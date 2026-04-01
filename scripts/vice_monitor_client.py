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


def parse_last_trace_cycle(block: str) -> int | None:
    """
    Parse the last trailing cycle value from a VICE trace line in a monitor block.

    Example line:
      .C:010f  D0 02 ... - A:D6 X:DA Y:00 ...   90487723
    """
    cyc: int | None = None
    for line in block.splitlines():
        # Keep this permissive; monitor formatting varies by VICE build/options.
        m = re.search(r"\.C:[0-9a-fA-F]{4}.*\s(-?\d+)\s*$", line)
        if not m:
            # Register dump format includes STOPWATCH as the last field, e.g.:
            # .;00fc 20 e7 00 fd 2f 10 10100100 131 057   90819030
            m2 = re.search(r"^\.;[0-9a-fA-F]{4}\s+.*\s(-?\d+)\s*$", line)
            if not m2:
                continue
            try:
                cyc = int(m2.group(1))
            except ValueError:
                continue
            continue
        try:
            cyc = int(m.group(1))
        except ValueError:
            continue
    return cyc


def parse_last_trace_pc(block: str) -> str | None:
    """Parse PC from the last .C:xxxx or .;xxxx monitor trace/register line."""
    last_pc: str | None = None
    for line in block.splitlines():
        m = re.search(r"\.C:([0-9a-fA-F]{4})\b", line)
        if m:
            last_pc = m.group(1).lower()
            continue
        m2 = re.search(r"^\.;([0-9a-fA-F]{4})\b", line)
        if m2:
            last_pc = m2.group(1).lower()
    return last_pc


def _vice_ignore_decimal_to_arg(n: int) -> str:
    """Format decimal hit-skip count as hex digits for the monitor (no 0x prefix)."""
    n = max(0, int(n))
    return f"{n:x}"


def parse_last_checkpoint_id(block: str) -> int | None:
    """
    Parse latest VICE checkpoint id from monitor output.
    Examples:
      BREAK: 2  C:$0881 ...
      WATCH: 1  C:$e5f0 ...
    """
    cid: int | None = None
    for line in block.splitlines():
        m = re.search(r"^(?:BREAK|WATCH):\s+(\d+)\b", line.strip())
        if not m:
            continue
        try:
            cid = int(m.group(1))
        except ValueError:
            continue
    return cid


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
    stop_on_cycle: int | None,
    stop_on_cycle_pc: str,
    phase2_stop_on_cycle: int | None,
    phase2_stop_on_cycle_pc: str,
    phase2_setup_commands: list[str] | None,
    auto_ignore_break_count: int,
    poll_mem_each_stop: bool,
    dynamic_ignore_phase1: bool,
    phase1_ceiling_cycle: int | None,
    phase1_margin_cycles: int,
    phase1_max_ignore: int,
    phase1_guess_cyc_per_hit: int,
    phase1_bootstrap_ignore: int,
) -> None:
    jf = None
    empty_stop_streak = 0
    phase2_armed = False
    coarse_break_id: int | None = None
    phase1_next_ignore_dec: int | None = None
    phase1_prev_stop_cycle: int | None = None
    phase1_last_applied_ignore_dec: int = 0
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
                    if coarse_break_id is None and cmd.strip().lower().startswith("break"):
                        coarse_break_id = parse_last_checkpoint_id(out)
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
                if (
                    auto_ignore_break_count > 0
                    and coarse_break_id is not None
                    and not dynamic_ignore_phase1
                ):
                    ign_cmd = f"ignore {coarse_break_id} {auto_ignore_break_count}"
                    if show_progress:
                        console.print(f"[{ts()}] [cyan]setup>[/cyan] {ign_cmd}")
                    ign_out = client.send(ign_cmd)
                    f.write(f">>> {ign_cmd}\n{ign_out}\n")
                    f.flush()
                    if jf is not None:
                        jf.write(json.dumps({
                            "timestamp": ts(),
                            "type": "setup",
                            "command": ign_cmd,
                            "response": ign_out,
                            "pc": parse_pc(ign_out),
                        }) + "\n")
                        jf.flush()

            for i in range(1, iterations + 1):
                if (
                    dynamic_ignore_phase1
                    and (not phase2_armed)
                    and coarse_break_id is not None
                    and phase1_ceiling_cycle is not None
                ):
                    if phase1_next_ignore_dec is None:
                        phase1_next_ignore_dec = min(
                            phase1_max_ignore,
                            max(0, int(phase1_bootstrap_ignore)),
                        )
                    ign_arg = _vice_ignore_decimal_to_arg(phase1_next_ignore_dec)
                    ign_cmd = f"ignore {coarse_break_id} {ign_arg}"
                    if show_progress:
                        console.print(
                            f"[{ts()}] [cyan]dynamic-ignore>[/cyan] {ign_cmd} "
                            f"(dec_skips={phase1_next_ignore_dec})"
                        )
                    ign_out = client.send(ign_cmd)
                    f.write(f">>> {ign_cmd}\n{ign_out}\n")
                    f.flush()
                    phase1_last_applied_ignore_dec = phase1_next_ignore_dec
                    if jf is not None:
                        jf.write(json.dumps({
                            "timestamp": ts(),
                            "type": "dynamic_ignore",
                            "index": i,
                            "command": ign_cmd,
                            "ignore_decimal": phase1_next_ignore_dec,
                            "response": ign_out,
                            "pc": parse_pc(ign_out),
                        }) + "\n")
                        jf.flush()

                batch = ["g"] if minimal_poll else (["g", "r", "m 002d 0030"] if poll_mem_each_stop else ["g", "r"])
                if (not minimal_poll) and disasm > 0:
                    batch.append(f"z {disasm}")
                if show_progress:
                    console.print(f"[{ts()}] [cyan]stop {i:04d}[/cyan] send batch: {' ; '.join(batch)}")
                batch_out = client.send_batch(batch)
                block = f">>> {' ; '.join(batch)}\n{batch_out}"
                pc = parse_pc(batch_out)
                stop_pcs = parse_stop_pcs(batch_out)
                last_trace_cycle = parse_last_trace_cycle(batch_out)
                trace_pc = parse_last_trace_pc(batch_out)
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
                        "trace_cycle": last_trace_cycle,
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
                cycle_hit = False
                if stop_on_cycle is not None and last_trace_cycle is not None:
                    pc_for_cycle = trace_pc if trace_pc is not None else pc
                    if (not stop_on_cycle_pc) or (pc_for_cycle == stop_on_cycle_pc):
                        cycle_hit = last_trace_cycle >= stop_on_cycle
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
                if cycle_hit:
                    if (not phase2_armed) and (phase2_stop_on_cycle is not None):
                        if show_progress:
                            console.print(
                                f"[{ts()}] [bold yellow]Phase1 reached cycle {last_trace_cycle}; switching to "
                                f"phase2 stop cycle {phase2_stop_on_cycle} at pc=${phase2_stop_on_cycle_pc}.[/bold yellow]"
                            )
                        if phase2_setup_commands:
                            f.write("=== phase2 setup ===\n")
                            for cmd in phase2_setup_commands:
                                if show_progress:
                                    console.print(f"[{ts()}] [cyan]phase2>[/cyan] {cmd}")
                                out = client.send(cmd)
                                f.write(f">>> {cmd}\n{out}\n")
                                f.flush()
                                if jf is not None:
                                    jf.write(json.dumps({
                                        "timestamp": ts(),
                                        "type": "phase2_setup",
                                        "command": cmd,
                                        "response": out,
                                        "pc": parse_pc(out),
                                    }) + "\n")
                                    jf.flush()
                        stop_on_cycle = phase2_stop_on_cycle
                        stop_on_cycle_pc = phase2_stop_on_cycle_pc
                        phase2_armed = True
                        continue

                    console.print(
                        f"[{ts()}] [bold yellow]Reached stop-on cycle: {last_trace_cycle} >= {stop_on_cycle} "
                        f"(pc=${pc}). Stopping early.[/bold yellow]"
                    )
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
                            "type": "detail_on_cycle",
                            "pc": pc,
                            "trace_cycle": last_trace_cycle,
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
                                "trace_cycle": last_trace_cycle,
                                "command": mem_cmd,
                                "response": mem_out,
                            }) + "\n")
                            jf.flush()
                    break

                # Retune coarse breakpoint ignore for the next continue (phase1 only).
                if (
                    dynamic_ignore_phase1
                    and (not phase2_armed)
                    and phase1_ceiling_cycle is not None
                    and coarse_break_id is not None
                    and last_trace_cycle is not None
                ):
                    pc_tune = trace_pc if trace_pc is not None else pc
                    if pc_tune == stop_on_cycle_pc:
                        ceiling = int(phase1_ceiling_cycle) - int(phase1_margin_cycles)
                        room = ceiling - int(last_trace_cycle)
                        if room <= 0:
                            phase1_next_ignore_dec = 0
                            phase1_prev_stop_cycle = int(last_trace_cycle)
                        elif phase1_prev_stop_cycle is None:
                            rate = max(int(phase1_guess_cyc_per_hit), 1)
                            phase1_next_ignore_dec = min(
                                phase1_max_ignore,
                                max(0, room // rate - 1),
                            )
                            phase1_prev_stop_cycle = int(last_trace_cycle)
                        else:
                            delta_cyc = int(last_trace_cycle) - int(phase1_prev_stop_cycle)
                            hits = int(phase1_last_applied_ignore_dec) + 1
                            rate = max(delta_cyc // max(hits, 1), 1)
                            want_total = min(
                                phase1_max_ignore + 1,
                                max(1, room // rate),
                            )
                            phase1_next_ignore_dec = min(
                                phase1_max_ignore,
                                max(0, int(want_total) - 1),
                            )
                            phase1_prev_stop_cycle = int(last_trace_cycle)
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
        choices=["", "e5f0_second_00fa", "mismatch_90487723"],
        default="",
        help="e5f0_second_00fa: watch $E5F0, --minimal-poll, stop on $00FA/$00FC, "
        "default memory dumps (for loader table vs c64py). "
        "mismatch_90487723: two-phase break $0881 then $010F; dynamic ignore on $0881; "
        "stop at vice_cyc>=90487723 at $010F.",
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
    ap.add_argument(
        "--stop-on-cycle",
        type=int,
        default=None,
        help="Optional: stop when parsed VICE trace cycle reaches/exceeds this value.",
    )
    ap.add_argument(
        "--stop-on-cycle-pc",
        default="",
        help="Optional: require current monitor PC to equal this hex PC for --stop-on-cycle.",
    )
    ap.add_argument(
        "--phase1-ceiling-cycle",
        type=int,
        default=None,
        help="With --preset mismatch_90487723: STOPWATCH ceiling (default 90487723) so dynamic "
        "ignore does not overshoot before fine phase.",
    )
    ap.add_argument(
        "--phase1-margin-cycles",
        type=int,
        default=None,
        help="With mismatch preset: cycles kept below ceiling (default 32768).",
    )
    ap.add_argument(
        "--phase1-max-ignore",
        type=int,
        default=None,
        help="With mismatch preset: max breakpoint skips per continue, decimal (default 32768).",
    )
    ap.add_argument(
        "--phase1-guess-cyc-per-hit",
        type=int,
        default=None,
        help="With mismatch preset: initial cyc/hit guess before first measurement (default 2800).",
    )
    ap.add_argument(
        "--phase1-bootstrap-ignore",
        type=int,
        default=None,
        help="With mismatch preset: first-iteration skip count, decimal (default 8192).",
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

        phase2_stop_on_cycle: int | None = None
        phase2_stop_on_cycle_pc = ""
        phase2_setup_commands: list[str] | None = None
        auto_ignore_break_count = 0
        dynamic_ignore_phase1 = False
        phase1_ceiling_cycle: int | None = None
        phase1_margin_cycles = 4096
        phase1_max_ignore = 0x8000
        phase1_guess_cyc_per_hit = 2800
        phase1_bootstrap_ignore = 8192
        poll_mem_each_stop = True

        if args.preset == "e5f0_second_00fa":
            args.watch_mode = "e5f0_second"
            args.stop_on_pc = "00fa,00fc"
            args.dump_mem_on_stop = (
                "0000 0001;e5f0 e610;e750 e770;00f8 0100;0110 0118"
            )
            args.minimal_poll = True
            args.iterations = max(args.iterations, 200)
        elif args.preset == "mismatch_90487723":
            args.watch_mode = "custom"
            # Two-phase search:
            # 1) Coarse: break at 0881 (outer driver, fewer hits) up to near target cycle.
            # 2) Fine: switch to break 010f and stop at exact mismatch cycle.
            phase1 = "delete;break 0881"
            extra = (";" + args.setup) if args.setup else ""
            args.setup = f"{phase1}{extra}"
            args.stop_on_pc = ""
            args.stop_on_cycle = 90487000
            args.stop_on_cycle_pc = "0881"
            phase2_stop_on_cycle = 90487723
            phase2_stop_on_cycle_pc = "010f"
            phase2_setup_commands = ["delete", "break 010f"]
            # Dynamic ignore: scale skips from measured c/stop; cap below phase2 target.
            auto_ignore_break_count = 0
            dynamic_ignore_phase1 = True
            phase1_ceiling_cycle = 90487723
            phase1_margin_cycles = 32768
            phase1_max_ignore = 0x8000
            phase1_guess_cyc_per_hit = 2800
            phase1_bootstrap_ignore = 8192
            poll_mem_each_stop = False
            args.dump_mem_on_stop = (
                "0000 0001;002d 0030;00f8 0118;0100 01ff;e5f0 e610;e750 e770"
            )
            # Keep phase1 cheap: parse cycle from register dump (.;) and avoid disasm stepping flood.
            args.minimal_poll = False
            args.disasm = 0
            args.iterations = max(args.iterations, 5000)

        if args.phase1_ceiling_cycle is not None:
            phase1_ceiling_cycle = args.phase1_ceiling_cycle
        if args.phase1_margin_cycles is not None:
            phase1_margin_cycles = max(0, args.phase1_margin_cycles)
        if args.phase1_max_ignore is not None:
            phase1_max_ignore = max(0, args.phase1_max_ignore)
        if args.phase1_guess_cyc_per_hit is not None:
            phase1_guess_cyc_per_hit = max(1, args.phase1_guess_cyc_per_hit)
        if args.phase1_bootstrap_ignore is not None:
            phase1_bootstrap_ignore = max(0, args.phase1_bootstrap_ignore)

        setup_commands = build_setup_commands(args.setup, args.watch_mode)
        stop_on_pcs = {pc.strip().lower() for pc in args.stop_on_pc.split(",") if pc.strip()}
        dump_mem_on_stop = [r.strip() for r in args.dump_mem_on_stop.split(";") if r.strip()]
        stop_on_cycle_pc = args.stop_on_cycle_pc.strip().lower()
        json_output_path = Path(args.json_output) if args.json_output else None

        # Connect with retries (x64sc can open the port before the monitor is ready).
        client: ViceMonitorClient | None = None
        last_err: Exception | None = None
        for attempt in range(1, 41):
            # When we launched x64sc ourselves, refresh the discovered port each retry.
            if x64sc_proc is not None:
                try:
                    args.port = _pick_text_monitor_port_for_pid(
                        x64sc_proc.pid, host=args.host, timeout_s=0.35
                    )
                except Exception:
                    # Keep prior port; retry connect anyway.
                    pass
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
                stop_on_cycle=args.stop_on_cycle,
                stop_on_cycle_pc=stop_on_cycle_pc,
                phase2_stop_on_cycle=phase2_stop_on_cycle,
                phase2_stop_on_cycle_pc=phase2_stop_on_cycle_pc,
                phase2_setup_commands=phase2_setup_commands,
                auto_ignore_break_count=auto_ignore_break_count,
                poll_mem_each_stop=poll_mem_each_stop,
                dynamic_ignore_phase1=dynamic_ignore_phase1,
                phase1_ceiling_cycle=phase1_ceiling_cycle,
                phase1_margin_cycles=phase1_margin_cycles,
                phase1_max_ignore=phase1_max_ignore,
                phase1_guess_cyc_per_hit=phase1_guess_cyc_per_hit,
                phase1_bootstrap_ignore=phase1_bootstrap_ignore,
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
