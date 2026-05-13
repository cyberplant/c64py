#!/usr/bin/env python3
"""Dump CIA2-derived IEC tap transitions from a cycle-capped TCP-drive run.

This is a developer aid for the KERNAL IEC bridge decoder work. It starts a real
``c1541_emulator`` subprocess, runs ``C64.py`` with ``--tcp-drive`` and
``C64PY_IEC_TAP_JSONL`` set, then leaves a JSONL transition trace on disk.

The BASIC program intentionally enters ``OPEN``; until the logical IEC decoder
lands, the run is expected to stop by ``--max-cycles`` rather than by completing
the program.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = REPO_ROOT.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_tcp(port: int, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Optional[OSError] = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    raise RuntimeError(f"drive server did not accept TCP connections on port {port}: {last_error}")


def _merged_env(tap_path: Path, rom_dir: Optional[str]) -> Dict[str, str]:
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(PACKAGE_PARENT)
        if not existing_pythonpath
        else str(PACKAGE_PARENT) + os.pathsep + existing_pythonpath
    )
    env["C64PY_IEC_TAP_JSONL"] = str(tap_path)
    env.setdefault("C64PY_USE_RUST_FAST", "0")
    if rom_dir:
        env["C64PY_ROM_DIR"] = rom_dir
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="test_results/iec_tap_open.jsonl",
        help="JSONL output path (default: test_results/iec_tap_open.jsonl)",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=3_000_000,
        help="C64 cycle cap for the capture run (default: 3000000)",
    )
    parser.add_argument(
        "--rom-dir",
        default=os.environ.get("C64PY_ROM_DIR"),
        help="Directory containing C64 ROMs (default: C64PY_ROM_DIR or auto-detect)",
    )
    args = parser.parse_args()

    if shutil.which("petcat") is None:
        print("ERROR: dump_iec_tap.py needs VICE petcat on PATH for the temporary .bas file.", file=sys.stderr)
        return 2

    tap_path = (REPO_ROOT / args.out).resolve() if not os.path.isabs(args.out) else Path(args.out)
    tap_path.parent.mkdir(parents=True, exist_ok=True)
    if tap_path.exists():
        tap_path.unlink()

    port = _free_port()
    env = _merged_env(tap_path, args.rom_dir)

    with tempfile.TemporaryDirectory(prefix="c64py_iec_tap_") as tmpdir_s:
        tmpdir = Path(tmpdir_s)
        disk_path = tmpdir / "tap.d64"
        bas_path = tmpdir / "tap_open.bas"
        bas_path.write_text(
            '10 OPEN 1,8,2,"HOSTSEQ,S,W"\n'
            '20 PRINT#1,"HELLO FROM C64PY"\n'
            "30 CLOSE 1\n"
            "40 END\n",
            encoding="ascii",
        )

        drive_cmd = [
            sys.executable,
            "-m",
            "c64py.drives.c1541_emulator",
            "--new-disk",
            str(disk_path),
            "--device",
            "8",
            "--port",
            str(port),
            "--interface",
            "headless",
            "--emulation",
            "fast",
        ]
        drive = subprocess.Popen(
            drive_cmd,
            cwd=str(PACKAGE_PARENT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_for_tcp(port)
            c64_cmd = [
                sys.executable,
                str(REPO_ROOT / "C64.py"),
                str(bas_path),
                "--tcp-drive",
                f"8:127.0.0.1:{port}",
                "--interface",
                "headless",
                "--no-colors",
                "--no-config",
                "--vic-emulation",
                "fast",
                "--max-cycles",
                str(args.max_cycles),
            ]
            if args.rom_dir:
                c64_cmd.extend(["--rom-dir", args.rom_dir])
            result = subprocess.run(c64_cmd, cwd=str(REPO_ROOT), env=env, text=True)
        finally:
            drive.terminate()
            try:
                drive.wait(timeout=5)
            except subprocess.TimeoutExpired:
                drive.kill()
                drive.wait(timeout=5)

        print(f"Wrote IEC tap JSONL to {tap_path}")
        return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
