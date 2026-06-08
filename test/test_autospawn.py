"""Tests for C64._spawn_local_drive auto-spawn helper (M3)."""
from __future__ import annotations

import socket
import time

import pytest

from c64py.emulator import C64


def _make_minimal_c64() -> C64:
    """Create a C64 instance with no ROMs / graphics (enough for IEC)."""
    emu = C64(
        interface_factory=None,
        enable_sid=False,
        enable_resid=False,
    )
    emu.initialize_iec_bus()
    return emu


def _wait_port(port: int, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def test_spawn_creates_tcp_client():
    emu = _make_minimal_c64()
    addr = emu._spawn_local_drive(disk_path=None, device=8, tier="fast")
    try:
        assert "localhost:" in addr
        assert 8 in emu.iec_drives
        client = emu.iec_drives[8]
        assert client.is_connected()
    finally:
        emu._terminate_spawned_drives()


def test_spawn_with_disk_attaches_d64(tmp_path):
    import shutil
    import os
    d64_src = os.path.join(os.path.dirname(__file__), "ark.d64")
    d64_copy = str(tmp_path / "test.d64")
    shutil.copy(d64_src, d64_copy)

    emu = _make_minimal_c64()
    addr = emu._spawn_local_drive(disk_path=d64_copy, device=8, tier="fast")
    try:
        client = emu.iec_drives[8]
        status = client.get_remote_status()
        assert status is not None
        assert status.get("disk", "").endswith(".d64")
    finally:
        emu._terminate_spawned_drives()


def test_terminate_kills_children():
    emu = _make_minimal_c64()
    addr = emu._spawn_local_drive(disk_path=None, device=8, tier="fast")
    procs = list(emu._spawned_drives)
    assert all(p.poll() is None for p in procs)
    emu._terminate_spawned_drives()
    time.sleep(0.2)
    assert all(p.poll() is not None for p in procs)


def test_spawn_invalid_disk_raises():
    emu = _make_minimal_c64()
    with pytest.raises(Exception):
        emu._spawn_local_drive(disk_path="/nonexistent/file.d64", device=8, tier="fast")
    emu._terminate_spawned_drives()
