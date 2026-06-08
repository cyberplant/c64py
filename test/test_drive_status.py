"""Tests for the 1541 status / command channel implementation in :mod:`c64py.drive`."""

from __future__ import annotations

import os
import sys

import pytest

# Allow running both as a package test and as a standalone script.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from c64py.d64 import D64Image, D64_SIZE_STANDARD, load_d64
from c64py.drives.drive import DiskDrive


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

ARK_D64 = os.path.join(os.path.dirname(__file__), "ark.d64")


def _blank_disk() -> D64Image:
    """Return a minimal valid (but empty) D64 image."""
    return D64Image(bytes(D64_SIZE_STANDARD))


def _make_drive_with_disk(d64: D64Image) -> DiskDrive:
    drive = DiskDrive(device_number=8)
    drive.attach_disk(d64, "test.d64")
    return drive


# ---------------------------------------------------------------------------
# Status / set_error / clear_error
# ---------------------------------------------------------------------------


def test_default_status_no_disk():
    drive = DiskDrive()
    # No disk attached → DRIVE NOT READY (independent of last_error).
    assert drive.get_status() == "74,DRIVE NOT READY,00,00"


def test_default_status_with_disk():
    drive = _make_drive_with_disk(_blank_disk())
    assert drive.last_error == (0, "OK", 0, 0)
    assert drive.get_status() == "00, OK,00,00"


def test_set_error_file_not_found():
    drive = _make_drive_with_disk(_blank_disk())
    drive.set_error(62)
    assert drive.get_status() == "62,FILE NOT FOUND,00,00"


def test_set_error_with_track_sector():
    drive = _make_drive_with_disk(_blank_disk())
    drive.set_error(20, track=18, sector=5)
    assert drive.get_status() == "20,READ ERROR,18,05"


def test_clear_error_returns_to_ok():
    drive = _make_drive_with_disk(_blank_disk())
    drive.set_error(62)
    drive.clear_error()
    assert drive.get_status() == "00, OK,00,00"


# ---------------------------------------------------------------------------
# Command channel
# ---------------------------------------------------------------------------


def test_command_channel_initialize_clears_status():
    drive = _make_drive_with_disk(_blank_disk())
    drive.set_error(62)
    drive.command_channel_write("I")
    assert drive.get_status() == "00, OK,00,00"
    drive.set_error(62)
    drive.command_channel_write("I0")
    assert drive.get_status() == "00, OK,00,00"


def test_command_channel_validate_clears_status():
    drive = _make_drive_with_disk(_blank_disk())
    drive.set_error(62)
    drive.command_channel_write("V0")
    assert drive.get_status() == "00, OK,00,00"


def test_command_channel_unknown_sets_syntax_error():
    drive = _make_drive_with_disk(_blank_disk())
    drive.command_channel_write("ZZ:WAT")
    code, message, _, _ = drive.last_error
    assert code == 31
    assert message == "SYNTAX ERROR"


def test_scratch_missing_file_sets_62():
    drive = _make_drive_with_disk(_blank_disk())
    drive.command_channel_write("S0:NOSUCH")
    assert drive.get_status() == "62,FILE NOT FOUND,00,00"


# ---------------------------------------------------------------------------
# Real fixture — ark.d64 — exercises scratch + rename + BAM bookkeeping.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.path.exists(ARK_D64), reason="ark.d64 fixture missing")
def test_scratch_real_file_removes_dir_entry_and_frees_bam():
    disk = load_d64(ARK_D64)
    # Snapshot BAM free counts so we can assert sectors really were freed.
    bam_before = bytes(disk.data[disk._track_sector_to_offset(18, 0):
                                   disk._track_sector_to_offset(18, 0) + 0x90])
    free_before = sum(bam_before[4 + (t - 1) * 4] for t in range(1, 36))

    entries_before = disk.read_directory()
    assert entries_before, "Fixture must have at least one file"
    target = entries_before[0].filename  # e.g. 'TEST1.PRG'

    drive = _make_drive_with_disk(disk)
    drive.command_channel_write(f"S0:{target}")

    code, message, count, _ = drive.last_error
    assert code == 1, f"Expected FILES SCRATCHED, got {drive.last_error!r}"
    assert message == "FILES SCRATCHED"
    assert count == 1
    assert drive.get_status() == "01,FILES SCRATCHED,01,00"

    # Directory should no longer list the file.
    entries_after = disk.read_directory()
    assert all(e.filename != target for e in entries_after)

    # BAM free count should have grown by exactly the file's block count.
    bam_after = bytes(disk.data[disk._track_sector_to_offset(18, 0):
                                  disk._track_sector_to_offset(18, 0) + 0x90])
    free_after = sum(bam_after[4 + (t - 1) * 4] for t in range(1, 36))
    assert free_after - free_before == entries_before[0].blocks


@pytest.mark.skipif(not os.path.exists(ARK_D64), reason="ark.d64 fixture missing")
def test_rename_real_file_updates_directory():
    disk = load_d64(ARK_D64)
    entries_before = disk.read_directory()
    assert entries_before
    old_name = entries_before[0].filename
    new_name = "RENAMED"

    drive = _make_drive_with_disk(disk)
    drive.command_channel_write(f"R0:{new_name}={old_name}")

    assert drive.get_status() == "00, OK,00,00"

    entries_after = disk.read_directory()
    names = [e.filename for e in entries_after]
    assert new_name in names
    assert old_name not in names


def test_rename_missing_file_sets_62():
    drive = _make_drive_with_disk(_blank_disk())
    drive.command_channel_write("R0:NEW=NOPE")
    assert drive.get_status() == "62,FILE NOT FOUND,00,00"


# ---------------------------------------------------------------------------
# Emulator integration: KERNAL LOAD failure / success via TCP fast_load RPC.
# ---------------------------------------------------------------------------

import shutil
import socket
import subprocess
import sys
import time


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _make_emulator_with_drive_server(disk_path: str):
    """Spawn a headless drive server and return (emu, client, proc)."""
    from c64py.emulator import C64
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "c64py.drives.c1541_emulator",
         "--interface", "headless", "--emulation", "fast",
         "--disk", disk_path, "--device", "8", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    assert _wait_port(port, timeout=4.0), "drive subprocess did not open port"
    emu = C64(interface_factory=lambda e: None)
    emu.interface = type("FakeUI", (), {"add_debug_log": lambda *a, **k: None})()
    emu._initialize_c64()
    emu.initialize_iec_bus(tcp_drives={8: f"localhost:{port}"})
    return emu, emu.get_drive(8), proc


def _set_load_filename(emu, name: str) -> None:
    emu.memory.write(0xBA, 8)
    emu.memory.write(0xB9, 1)
    emu.memory.write(0xB7, len(name))
    emu.memory.write(0xBB, 0x00)
    emu.memory.write(0xBC, 0x10)
    for i, ch in enumerate(name):
        emu.memory.write(0x1000 + i, ord(ch))
    emu.cpu.state.pc = 0xFFD5
    emu.cpu.state.a = 0
    emu.cpu.state.sp = 0xFD
    emu.memory.write(0x01FE, 0x99)
    emu.memory.write(0x01FF, 0x99)


@pytest.mark.skipif(not os.path.exists(ARK_D64), reason="ark.d64 fixture missing")
def test_kernal_load_missing_file_sets_carry():
    """KERNAL LOAD hook sets carry flag (error) when file not found via TCP."""
    emu, client, proc = _make_emulator_with_drive_server(ARK_D64)
    try:
        _set_load_filename(emu, "DOESNOTEXIST")
        handled = emu._handle_kernal_load()
        assert handled is True
        assert emu.cpu.state.p & 0x01, "carry not set on missing file"
        # $90 (ST) must be 0x00 — error is signalled via carry + A (KERNAL error code)
        assert emu.memory.read(0x90) == 0x00, "ST should be cleared; error in A/carry"
        # A should be a valid KERNAL error code (4=file not found, 5=device not present)
        assert emu.cpu.state.a in (4, 5), f"unexpected KERNAL error code: {emu.cpu.state.a}"
    finally:
        client.disconnect()
        proc.terminate()
        proc.wait(timeout=3)


@pytest.mark.skipif(not os.path.exists(ARK_D64), reason="ark.d64 fixture missing")
def test_kernal_load_success_clears_carry():
    """KERNAL LOAD hook clears carry (success) when file found via TCP."""
    from c64py.d64 import load_d64
    emu, client, proc = _make_emulator_with_drive_server(ARK_D64)
    try:
        disk = load_d64(ARK_D64)
        entries = disk.read_directory()
        assert entries, "ark.d64 has no directory entries"
        name = entries[0].filename.strip()
        _set_load_filename(emu, name)
        handled = emu._handle_kernal_load()
        assert handled is True
        assert not (emu.cpu.state.p & 0x01), "carry set on successful load"
        assert emu.memory.read(0x90) == 0x00
    finally:
        client.disconnect()
        proc.terminate()
        proc.wait(timeout=3)


def test_tcp_status_reply_includes_implementation(tmp_path):
    """``status`` RPC returns optional implementation/media keys (hardware bridge)."""
    from c64py.d64 import create_blank_d64

    p = tmp_path / "blank.d64"
    create_blank_d64("S", "01").save_to_file(str(p))
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "c64py.drives.c1541_emulator",
         "--interface", "headless", "--emulation", "fast",
         "--disk", str(p), "--device", "8", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        assert _wait_port(port, timeout=4.0)
        from c64py.drives.tcp_drive_client import TcpDriveClient

        c = TcpDriveClient(8, "127.0.0.1", port)
        assert c.connect()
        st = c.get_remote_status()
        assert st.get("implementation") == "c64py-c1541-emulator"
        assert st.get("media") == "d64"
    finally:
        proc.terminate()
        proc.wait(timeout=3)
