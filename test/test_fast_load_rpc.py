"""End-to-end tests for fast_load / fast_save / status RPC.

Spawns a real ``c1541_emulator`` subprocess with a **fresh blank D64** (no
external disk fixtures) and talks JSON over ``TcpDriveClient``.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import pytest

from c64py.d64 import create_blank_d64, load_d64
from c64py.drives.tcp_drive_client import TcpDriveClient


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


@pytest.fixture()
def drive_tcp_server(tmp_path):
    """Headless TCP drive on an ephemeral port; backing image is a blank D64."""
    d64_path = tmp_path / "server.d64"
    create_blank_d64("PYTEST", "01").save_to_file(str(d64_path))
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "c64py.drives.c1541_emulator",
         "--interface", "headless", "--emulation", "fast",
         "--disk", str(d64_path),
         "--device", "8", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    assert _wait_port(port, timeout=4.0), "drive subprocess did not open port"
    client = TcpDriveClient(device_number=8, host="127.0.0.1", port=port)
    assert client.connect(), "TcpDriveClient could not connect"
    yield client, port, str(d64_path)
    client.disconnect()
    proc.terminate()
    proc.wait(timeout=3)


def _entry_by_stem(img, stem: str):
    want = stem.upper().strip()
    for e in img.read_directory():
        if e.filename.upper().strip() == want:
            return e
    return None


def test_fast_save_prg_persists_to_d64_on_host(drive_tcp_server):
    """After fast_save, the backing .d64 on disk must contain the new file."""
    client, _port, d64_path = drive_tcp_server
    payload = bytes([0x01, 0x08, 0x00, 0x20, 0xEA])  # $0801 + small PRG body
    ok, err = client.fast_save("PRGONE", payload)
    assert ok, f"fast_save failed: {err}"

    img = load_d64(d64_path)
    ent = _entry_by_stem(img, "PRGONE")
    assert ent is not None, "directory entry missing after save"
    assert ent.filetype == 2  # PRG nibble
    assert img.read_file(ent) == payload


def test_fast_save_seq_persists_and_roundtrips(drive_tcp_server):
    raw = b"hello seq world"
    client, _port, d64_path = drive_tcp_server
    ok, err = client.fast_save("MYSEQ,S", raw)
    assert ok, f"fast_save SEQ failed: {err}"

    img = load_d64(d64_path)
    ent = _entry_by_stem(img, "MYSEQ")
    assert ent is not None
    assert ent.filetype == 1
    assert img.read_file(ent) == raw

    data2, err2, ft2 = client.fast_load("MYSEQ,S", secondary=1)
    assert err2 is None, err2
    assert ft2 == 1
    assert data2 == bytes([0x01, 0x08]) + raw


def test_fast_load_directory(drive_tcp_server):
    client, _port, _ = drive_tcp_server
    data, err, _ft = client.fast_load("$", secondary=0)
    assert err is None, f"expected success, got error {err}"
    assert data is not None
    assert len(data) >= 4
    assert data[0] == 0x01
    assert data[1] == 0x08
    assert data[4:6] == b"\x00\x00"
    assert data[6] == 0x12


def test_fast_load_wildcard_blank_has_no_prg(drive_tcp_server):
    """Blank disk has no PRG yet; ``*`` should not load a program."""
    client, _port, _ = drive_tcp_server
    _data, err, _ft = client.fast_load("*", secondary=1)
    assert err is not None
    assert err[0] in (62, 74)


def test_fast_load_missing_file(drive_tcp_server):
    client, _port, _ = drive_tcp_server
    data, err, _ft = client.fast_load("DOESNOTEXIST", secondary=1)
    assert data is None
    assert err is not None
    assert err[0] in (62, 74)


def test_status_rpc(drive_tcp_server):
    client, _port, d64_path = drive_tcp_server
    result = client.get_remote_status()
    assert result is not None
    assert result.get("type") == "status_reply"
    assert "led_on" in result
    assert "disk" in result
    assert "status" in result
    assert os.path.basename(d64_path) in result["disk"] or result["disk"].endswith(".d64")


def test_detach_and_reattach(drive_tcp_server):
    client, _port, d64_path = drive_tcp_server
    assert client.detach_disk_remote()
    st = client.get_remote_status()
    assert st["disk"] == ""

    assert client.attach_disk_remote(d64_path)
    st2 = client.get_remote_status()
    assert st2["disk"] != ""


def test_fast_save_roundtrip(drive_tcp_server):
    """Save a small PRG then load it back by name."""
    client, _port, _ = drive_tcp_server
    filename = "TESTPROG"
    payload = bytes([0x01, 0x08, 0xEA, 0xEA, 0xEA])
    ok, err = client.fast_save(filename, payload)
    assert ok, f"fast_save failed: {err}"

    data2, err2, ft2 = client.fast_load(filename, secondary=1)
    assert err2 is None, f"fast_load after save failed: {err2}"
    assert data2 == payload
    assert ft2 == 2
