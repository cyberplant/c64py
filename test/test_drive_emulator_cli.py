"""Smoke tests for c1541_emulator CLI flags added in M1."""
from __future__ import annotations

import subprocess
import sys
import time
import socket


def _start(args: list[str], timeout: float = 2.0) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "c64py.drives.c1541_emulator"] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_port(port: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def test_graphics_stub_exits_2():
    proc = _start(["--interface", "graphics"])
    proc.wait(timeout=5)
    assert proc.returncode == 2


def test_headless_fast_listens():
    port = 16440
    proc = _start(["--interface", "headless", "--emulation", "fast", "--port", str(port)])
    try:
        assert _wait_port(port, timeout=3.0), "drive did not open port in time"
    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_headless_accurate_python_listens():
    port = 16441
    proc = _start(["--interface", "headless", "--emulation", "accurate-python", "--port", str(port)])
    try:
        assert _wait_port(port, timeout=3.0), "drive did not open port in time"
    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_headless_accurate_rust_listens():
    port = 16442
    proc = _start(["--interface", "headless", "--emulation", "accurate-rust", "--port", str(port)])
    try:
        assert _wait_port(port, timeout=3.0), "drive did not open port in time"
    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_unknown_interface_rejected():
    proc = _start(["--interface", "vr"])
    proc.wait(timeout=5)
    assert proc.returncode != 0


def test_unknown_emulation_rejected():
    proc = _start(["--emulation", "ultra"])
    proc.wait(timeout=5)
    assert proc.returncode != 0
