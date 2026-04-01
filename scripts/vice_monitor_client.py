#!/usr/bin/env python3
"""Small TCP client for VICE remote monitor automation.

Example:
  python scripts/vice_monitor_client.py \
    --host 127.0.0.1 --port 6510 \
    --setup "delete;break 011d;watch store 002f 0030;watch store c200 c3ff;g" \
    --iterations 40 --disasm 20 --output vice_monitor_capture.log

  Second store to $E5F0 from loader (skip STA $E500,Y at $0849, stop on STA ($2D),Y at $00FA):
  python scripts/vice_monitor_client.py --preset e5f0_second_00fa \\
    --json-output vice_capture_00fa_after0849.jsonl \\
    --output vice_capture_00fa_after0849.log --no-exit
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console


PROMPT_RE = re.compile(r"\(C:\$([0-9a-fA-F]{4})\)")


@dataclass
class ViceMonitorClient:
    host: str
    port: int
    timeout_s: float = 0.6
    idle_gap_s: float = 0.12
    max_wait_s: float = 2.5

    def __post_init__(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        self.sock.settimeout(self.timeout_s)

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass

    def _recv_idle(self) -> bytes:
        chunks: list[bytes] = []
        started = time.monotonic()
        last_data = started
        while True:
            try:
                data = self.sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
                last_data = time.monotonic()
                # Stop early if a monitor prompt is present.
                if b"(C:$" in b"".join(chunks[-2:]):
                    if time.monotonic() - last_data >= self.idle_gap_s:
                        break
                continue
            except TimeoutError:
                now = time.monotonic()
                if now - started >= self.max_wait_s:
                    break
                if time.monotonic() - last_data >= self.idle_gap_s:
                    break
        return b"".join(chunks)

    def send(self, command: str) -> str:
        payload = (command.rstrip("\n") + "\n").encode("ascii", errors="ignore")
        self.sock.sendall(payload)
        out = self._recv_idle()
        return out.decode("latin1", errors="replace")

    def send_batch(self, commands: list[str]) -> str:
        data = "".join(f"{c.rstrip()}\n" for c in commands)
        self.sock.sendall(data.encode("ascii", errors="ignore"))
        out = self._recv_idle()
        # VICE monitor can occasionally respond late; retry one passive read.
        if not out.strip():
            time.sleep(0.05)
            out = self._recv_idle()
        return out.decode("latin1", errors="replace")


def parse_pc(block: str) -> str:
    matches = PROMPT_RE.findall(block)
    return matches[-1].lower() if matches else "????"


def parse_stop_pcs(block: str) -> set[str]:
    pcs = set(m.lower() for m in re.findall(r"C:([0-9a-fA-F]{4})", block))
    pcs.update(m.lower() for m in re.findall(r"C:\$([0-9a-fA-F]{4})", block))
    return pcs


def build_setup_commands(setup: str, watch_mode: str) -> list[str]:
    presets = {
        "custom": [],
        # Focused: avoid 002f/0030 flood and track destination pointer only.
        "ptr2d": ["delete", "break 011d", "watch store 002d 002e"],
        # Even more focused: watch only the high byte to avoid INC $2D flood.
        "ptr2e": ["delete", "break 011d", "watch store 002e 002e"],
        # Pointer-source focused mode.
        "ptr2f": ["delete", "break 011d", "watch store 002f 0030"],
        # Broad mode for full pointer + destination data.
        "all": [
            "delete",
            "break 011d",
            "watch store 002d 002e",
            "watch store 002f 0030",
            "watch store c200 c3ff",
        ],
        # Bruce Lee–style: first $E5F0 hit is often $0849; second is $00FA (STA ($2D),Y).
        "e5f0_second": ["delete", "watch store e5f0 e5f0"],
    }
    commands = list(presets[watch_mode])
    commands.extend(c.strip() for c in setup.split(";") if c.strip())
    return commands


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _pick_text_monitor_port_for_pid(pid: int, host: str, timeout_s: float = 6.0) -> int:
    """
    Discover VICE *text* remote monitor port for a running x64sc PID.

    VICE often opens two listeners: a dynamic text monitor port and 6502 (binary monitor).
    We select a listening TCP port bound to `host` and not equal to 6502.
    """
    lsof = shutil.which("lsof")
    if not lsof:
        raise RuntimeError("lsof not found (needed to discover remote monitor port)")
    deadline = time.monotonic() + max(0.5, timeout_s)
    last_err = ""
    while time.monotonic() < deadline:
        try:
            out = subprocess.check_output(
                # NOTE: On macOS, combine filters with -a (AND). Without it, -p can behave like an OR,
                # returning unrelated processes and causing us to pick the wrong port.
                [lsof, "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
                stderr=subprocess.STDOUT,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            last_err = (e.output or "").strip()
            time.sleep(0.15)
            continue

        ports: list[int] = []
        for line in out.splitlines():
            # Example: x64sc 44271 ... TCP 127.0.0.1:54379 (LISTEN)
            m = re.search(r"\bTCP\s+([0-9.:]+):(\d+)\s+\(LISTEN\)\s*$", line)
            if not m:
                continue
            ip, port_s = m.group(1), m.group(2)
            if ip != host:
                continue
            try:
                port = int(port_s)
            except ValueError:
                continue
            if port == 6502:
                continue
            ports.append(port)

        if ports:
            return sorted(set(ports))[-1]
        time.sleep(0.15)

    msg = f"could not discover x64sc remote monitor port for pid={pid}"
    if last_err:
        msg += f": {last_err}"
    raise RuntimeError(msg)


def _launch_x64sc(
    *,
    console: Console,
    prg: str,
    cwd: str,
    host: str,
    x64sc_path: str,
    extra_args: list[str],
    show_progress: bool,
    log_path: Path | None,
) -> tuple[subprocess.Popen[str], int]:
    """Launch x64sc and return (proc, text_monitor_port)."""
    exe = shutil.which(x64sc_path) if os.path.sep not in x64sc_path else x64sc_path
    if not exe:
        raise RuntimeError(f"x64sc not found: {x64sc_path}")

    cwd_path = Path(cwd).resolve()
    prg_path = Path(prg)
    if not prg_path.is_absolute():
        prg_path = (cwd_path / prg_path).resolve()
    if not prg_path.exists():
        raise RuntimeError(f"x64sc autostart PRG not found: {prg_path}")

    args = [
        exe,
        "-remotemonitor",
        "-remotemonitoraddress",
        host,
        "-autostart",
        str(prg_path),
        "--warp",
        "--console",
        *extra_args,
    ]
    if show_progress:
        console.print(f"[{ts()}] [bold green]launch>[/bold green] {' '.join(args)}")

    # x64sc is GUI-based (GTK); we don't attach stdin. Optionally capture logs.
    stdout = subprocess.DEVNULL
    stderr = subprocess.DEVNULL
    log_fh = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("w", encoding="utf-8")
        stdout = log_fh
        stderr = subprocess.STDOUT

    try:
        proc = subprocess.Popen(
            args,
            cwd=str(cwd_path),
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
    except Exception:
        if log_fh:
            log_fh.close()
        raise

    try:
        port = _pick_text_monitor_port_for_pid(proc.pid, host=host)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
        if log_fh:
            log_fh.close()
        raise

    if show_progress:
        console.print(f"[{ts()}] [bold green]launch[/bold green]: x64sc pid={proc.pid} text_monitor_port={port}")
    # Give VICE a moment to finish monitor init before first client commands.
    time.sleep(0.4)
    return proc, port


def run_capture(
    client: ViceMonitorClient,
    setup_commands: list[str],
    iterations: int,
    disasm: int,
    output_path: Path,
    json_output_path: Path | None,
    show_progress: bool,
    console: Console,
    stop_on_pcs: set[str],
    dump_mem_on_stop: list[str],
    max_empty_stops: int,
    minimal_poll: bool,
    exit_on_finish: bool,
) -> None:
    jf = None
    empty_stop_streak = 0
    try:
        with output_path.open("w", encoding="utf-8", buffering=1) as f:
            f.write("# VICE monitor capture\n")
            f.write(f"# host={client.host} port={client.port}\n\n")
            f.flush()

            if json_output_path is not None:
                jf = json_output_path.open("w", encoding="utf-8", buffering=1)

            if show_progress:
                console.print(f"[{ts()}] [bold green]Logging to[/bold green] {output_path.resolve()}")
                if jf is not None:
                    console.print(f"[{ts()}] [bold green]JSONL to[/bold green] {json_output_path.resolve()}")

            banner = client._recv_idle().decode("latin1", errors="replace")
            if banner.strip():
                f.write("=== banner ===\n")
                f.write(banner)
                if not banner.endswith("\n"):
                    f.write("\n")
                f.flush()

            if setup_commands:
                f.write("=== setup ===\n")
                for cmd in setup_commands:
                    if show_progress:
                        console.print(f"[{ts()}] [cyan]setup>[/cyan] {cmd}")
                    out = client.send(cmd)
                    f.write(f">>> {cmd}\n{out}\n")
                    f.flush()
                    if jf is not None:
                        jf.write(json.dumps({
                            "timestamp": ts(),
                            "type": "setup",
                            "command": cmd,
                            "response": out,
                            "pc": parse_pc(out),
                        }) + "\n")
                        jf.flush()

            for i in range(1, iterations + 1):
                batch = ["g"] if minimal_poll else ["g", "r", "m 002d 0030"]
                if (not minimal_poll) and disasm > 0:
                    batch.append(f"z {disasm}")
                if show_progress:
                    console.print(f"[{ts()}] [cyan]stop {i:04d}[/cyan] send batch: {' ; '.join(batch)}")
                batch_out = client.send_batch(batch)
                block = f">>> {' ; '.join(batch)}\n{batch_out}"
                pc = parse_pc(batch_out)
                stop_pcs = parse_stop_pcs(batch_out)
                # Guard against monitor desync/no-output loops (pc=???? forever).
                if (pc == "????") and not stop_pcs:
                    empty_stop_streak += 1
                else:
                    empty_stop_streak = 0
                f.write(f"=== stop {i:04d} pc=${pc} ===\n")
                f.write(block)
                if not block.endswith("\n"):
                    f.write("\n")
                f.flush()

                if jf is not None:
                    jf.write(json.dumps({
                        "timestamp": ts(),
                        "type": "stop",
                        "index": i,
                        "pc": pc,
                        "commands": {
                            "batch": batch,
                        },
                        "raw_block": block,
                    }) + "\n")
                    jf.flush()

                console.print(f"[{ts()}] [bold]stop {i:04d}[/bold]: pc=${pc} hits={sorted(stop_pcs)}")
                if empty_stop_streak >= max_empty_stops:
                    console.print(
                        f"[{ts()}] [bold red]Stopping after {empty_stop_streak} consecutive empty/unknown stops.[/bold red]"
                    )
                    if jf is not None:
                        jf.write(json.dumps({
                            "timestamp": ts(),
                            "type": "warning",
                            "index": i,
                            "reason": "consecutive_empty_stops",
                            "count": empty_stop_streak,
                        }) + "\n")
                        jf.flush()
                    break
                hit = (pc in stop_on_pcs) or bool(stop_pcs.intersection(stop_on_pcs))
                if hit:
                    matched = sorted(({pc} | stop_pcs).intersection(stop_on_pcs))
                    console.print(
                        f"[{ts()}] [bold yellow]Reached stop-on PC(s): {', '.join(matched)}. Stopping early.[/bold yellow]"
                    )
                    # In minimal polling mode, pull detailed state only when we hit.
                    if minimal_poll:
                        detail_batch = ["r", "m 002d 0030"]
                        detail_out = client.send_batch(detail_batch)
                        detail_block = f">>> {' ; '.join(detail_batch)}\n{detail_out}"
                        f.write(detail_block)
                        if not detail_block.endswith("\n"):
                            f.write("\n")
                        f.flush()
                        if jf is not None:
                            jf.write(json.dumps({
                                "timestamp": ts(),
                                "type": "detail_on_stop",
                                "pc": pc,
                                "commands": {
                                    "batch": detail_batch,
                                },
                                "raw_block": detail_block,
                            }) + "\n")
                            jf.flush()
                    for dump_range in dump_mem_on_stop:
                        mem_cmd = f"m {dump_range}"
                        if show_progress:
                            console.print(f"[{ts()}] [cyan]dump>[/cyan] {mem_cmd}")
                        mem_out = client.send(mem_cmd)
                        f.write(f">>> {mem_cmd}\n{mem_out}\n")
                        f.flush()
                        if jf is not None:
                            jf.write(json.dumps({
                                "timestamp": ts(),
                                "type": "mem_dump",
                                "pc": pc,
                                "command": mem_cmd,
                                "response": mem_out,
                            }) + "\n")
                            jf.flush()
                    break
    finally:
        if jf is not None:
            jf.close()
        if exit_on_finish:
            try:
                if show_progress:
                    console.print(f"[{ts()}] [cyan]exit>[/cyan] q")
                client.send("q")
            except Exception:
                # Best-effort; monitor might already be closed.
                pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Automate VICE TCP monitor capture.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6510)
    ap.add_argument(
        "--launch-x64sc",
        action="store_true",
        help="Launch x64sc automatically and discover the text monitor port (requires lsof).",
    )
    ap.add_argument(
        "--x64sc-path",
        default="x64sc",
        help="Path to x64sc executable (default: x64sc in PATH).",
    )
    ap.add_argument(
        "--x64sc-cwd",
        default=str(Path(__file__).resolve().parents[1]),
        help="Working directory for x64sc (default: repo root inferred from this script).",
    )
    ap.add_argument(
        "--x64sc-prg",
        default="programs/BruceLee.prg",
        help="PRG passed to x64sc -autostart (default: programs/BruceLee.prg).",
    )
    ap.add_argument(
        "--x64sc-extra-args",
        default="",
        help="Extra args appended to x64sc command line (single string, split on spaces).",
    )
    ap.add_argument(
        "--x64sc-log",
        default="",
        help="Optional path to capture x64sc stdout/stderr (useful for debugging).",
    )
    ap.add_argument(
        "--kill-x64sc",
        action="store_true",
        help="With --launch-x64sc: terminate x64sc after capture (in addition to sending 'q').",
    )
    ap.add_argument(
        "--setup",
        default="",
        help="Semicolon-separated commands sent once before capture.",
    )
    ap.add_argument(
        "--watch-mode",
        choices=["custom", "ptr2d", "ptr2e", "ptr2f", "all", "e5f0_second"],
        default="custom",
        help="Optional setup preset (custom leaves setup untouched).",
    )
    ap.add_argument(
        "--preset",
        choices=["", "e5f0_second_00fa"],
        default="",
        help="e5f0_second_00fa: watch $E5F0, --minimal-poll, stop on $00FA/$00FC, "
        "default memory dumps (for loader table vs c64py).",
    )
    ap.add_argument(
        "--stop-on-pc",
        default="011d",
        help="Comma-separated PCs to stop early on (e.g. 011d,0841,c200). Empty disables.",
    )
    ap.add_argument(
        "--dump-mem-on-stop",
        default="",
        help="Optional semicolon-separated ranges for m command when stop-on-pc hits "
             "(e.g. 'e5f0 e610;c200 c3ff').",
    )
    ap.add_argument(
        "--max-empty-stops",
        type=int,
        default=50,
        help="Stop after this many consecutive empty/unknown stop blocks.",
    )
    ap.add_argument(
        "--minimal-poll",
        action="store_true",
        help="Send only 'g' each iteration; fetch regs/memory only when stop-on-pc hits.",
    )
    ap.add_argument("--iterations", type=int, default=50)
    ap.add_argument("--disasm", type=int, default=20, help="z N each stop (0 disables).")
    ap.add_argument("--output", default="vice_monitor_capture.log")
    ap.add_argument(
        "--json-output",
        nargs="?",
        const="vice_monitor_capture.jsonl",
        default="",
        help="Enable JSONL output; optionally provide output path.",
    )
    ap.add_argument("--quiet", action="store_true", help="Reduce terminal progress output.")
    ap.add_argument(
        "--no-exit",
        action="store_true",
        help="Do not send 'q' to emulator monitor when capture finishes.",
    )
    args = ap.parse_args()

    console = Console()
    x64sc_proc: subprocess.Popen[str] | None = None
    try:
        if args.launch_x64sc:
            x64sc_log = Path(args.x64sc_log) if args.x64sc_log else None
            extra = [a for a in args.x64sc_extra_args.split(" ") if a.strip()] if args.x64sc_extra_args else []
            x64sc_proc, port = _launch_x64sc(
                console=console,
                prg=args.x64sc_prg,
                cwd=args.x64sc_cwd,
                host=args.host,
                x64sc_path=args.x64sc_path,
                extra_args=extra,
                show_progress=not args.quiet,
                log_path=x64sc_log,
            )
            args.port = int(port)

        if args.preset == "e5f0_second_00fa":
            args.watch_mode = "e5f0_second"
            args.stop_on_pc = "00fa,00fc"
            args.dump_mem_on_stop = (
                "0000 0001;e5f0 e610;e750 e770;00f8 0100;0110 0118"
            )
            args.minimal_poll = True
            args.iterations = max(args.iterations, 200)

        setup_commands = build_setup_commands(args.setup, args.watch_mode)
        stop_on_pcs = {pc.strip().lower() for pc in args.stop_on_pc.split(",") if pc.strip()}
        dump_mem_on_stop = [r.strip() for r in args.dump_mem_on_stop.split(";") if r.strip()]
        json_output_path = Path(args.json_output) if args.json_output else None

        # Connect with retries (x64sc can open the port before the monitor is ready).
        client: ViceMonitorClient | None = None
        last_err: Exception | None = None
        for attempt in range(1, 41):
            try:
                client = ViceMonitorClient(host=args.host, port=args.port)
                break
            except Exception as e:
                last_err = e
                time.sleep(0.1 + min(0.4, attempt * 0.01))
        if client is None:
            raise RuntimeError(f"failed to connect to VICE monitor at {args.host}:{args.port}: {last_err}")
        # Let the monitor settle after accept.
        time.sleep(0.2)

        try:
            run_capture(
                client=client,
                setup_commands=setup_commands,
                iterations=args.iterations,
                disasm=args.disasm,
                output_path=Path(args.output),
                json_output_path=json_output_path,
                show_progress=not args.quiet,
                console=console,
                stop_on_pcs=stop_on_pcs,
                dump_mem_on_stop=dump_mem_on_stop,
                max_empty_stops=args.max_empty_stops,
                minimal_poll=args.minimal_poll,
                exit_on_finish=not args.no_exit,
            )
        finally:
            client.close()
    finally:
        if x64sc_proc is not None and args.kill_x64sc:
            try:
                if not args.quiet:
                    console.print(f"[{ts()}] [cyan]kill>[/cyan] x64sc pid={x64sc_proc.pid}")
                x64sc_proc.terminate()
                try:
                    x64sc_proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    x64sc_proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    main()
