"""Disk-drive integration tests.

Hermetic: every test builds its own D64 in a tmp_path via create_blank_d64
+ write_file, so there's no dependency on a checked-in fixture file.
"""
import shutil
import socket
import subprocess
import sys
import time

import pytest

from c64py.d64 import create_blank_d64, load_d64
from c64py.drives.drive import DiskDrive
from c64py.drives.tcp_drive_client import TcpDriveClient
from c64py.emulator import C64
from c64py.server import EmulatorServer


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


@pytest.fixture
def disk_path(tmp_path):
    """Build a fresh D64 with one tiny PRG and return the file path."""
    d = create_blank_d64(disk_name="TESTDISK", disk_id="01")
    # PRG: load address $C000 then RTS ($60). 3 bytes total.
    d.write_file("HELLO", b"\x00\xC0\x60")
    p = tmp_path / "test.d64"
    d.save_to_file(str(p))
    return str(p)


def test_d64_parsing(disk_path):
    d = load_d64(disk_path)
    name, did = d.read_bam()
    assert name.strip().upper() == "TESTDISK"
    assert did.strip().upper() == "01"
    entries = d.read_directory()
    assert any(e.filename.strip().upper() == "HELLO" for e in entries)
    listing = d.format_directory_listing()
    assert "HELLO" in listing.upper()


def test_disk_drive(disk_path):
    d = load_d64(disk_path)
    drive = DiskDrive(device_number=8)
    drive.attach_disk(d, disk_path)
    dir_data = drive.load_file("$")
    assert dir_data is not None and len(dir_data) > 2
    # BASIC start ($0801) is the canonical directory load address.
    load_addr = dir_data[0] | (dir_data[1] << 8)
    assert load_addr == 0x0801
    # First BASIC line (header): line number 0, text starts with RVS ON ($12).
    assert dir_data[4:6] == b"\x00\x00"
    assert dir_data[6] == 0x12
    assert dir_data[7] == ord('"')
    assert dir_data[24] == ord('"')
    first_line_text = dir_data[6 : dir_data.index(0, 6)]
    assert first_line_text.endswith(b" 2A")
    drive.detach_disk()
    assert not drive.has_disk()


def test_emulator_integration(disk_path):
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "c64py.drives.c1541_emulator",
         "--interface", "headless", "--emulation", "fast",
         "--disk", disk_path, "--device", "8", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        assert _wait_port(port, timeout=4.0), "drive did not open port"
        emu = C64()
        emu.interface = type("Iface", (), {"add_debug_log": lambda *a, **k: None})()
        emu.initialize_iec_bus(tcp_drives={8: f"localhost:{port}"})
        client = emu.get_drive(8)
        assert client is not None
        status = client.get_remote_status()
        assert status is not None and status.get("disk", "") != ""
        dir_data, err, _ft = client.fast_load("$", secondary=0)
        assert err is None and dir_data is not None and len(dir_data) > 2
        emu.detach_disks()
        status2 = client.get_remote_status()
        assert status2["disk"] == ""
    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_server_commands(disk_path):
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "c64py.drives.c1541_emulator",
         "--interface", "headless", "--emulation", "fast",
         "--disk", disk_path, "--device", "8", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        assert _wait_port(port, timeout=4.0), "drive did not open port"
        emu = C64()
        emu.interface = type("Iface", (), {"add_debug_log": lambda *a, **k: None})()
        emu.initialize_iec_bus(tcp_drives={8: f"localhost:{port}"})
        server = EmulatorServer(emu, tcp_port=None, udp_port=None)
        assert 8 in emu.iec_drives
        response = server._handle_command("DETACH-DISKS")
        assert "OK" in response.upper() or "DETACH" in response.upper()
        assert emu.iec_drives.get(8) is not None
    finally:
        proc.terminate()
        proc.wait(timeout=3)
