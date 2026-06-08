"""Byte-level IEC TALK/LISTEN protocol exercised against a Drive1541 + D64.

Drives the bus exactly as a real KERNAL would: LISTEN dev, OPEN secondary,
filename bytes, UNLISTEN, TALK dev, DATA secondary, drain with receive_byte
until EOI; then UNTALK to close.
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from c64py.d64 import load_d64
from c64py.drives.drive import DiskDrive
from c64py.drives.c1541_emulator import Drive1541
from c64py.iec_bus import IECBus


D64_PATH = os.path.join(os.path.dirname(__file__), "ark.d64")


def _make_bus_with_drive(device: int = 8):
    disk = load_d64(D64_PATH)
    drive = Drive1541(device_number=device)
    drive.attach_disk(disk, D64_PATH)
    bus = IECBus()
    bus.attach_device(drive)
    return bus, drive, disk


def _drain_talker(bus: IECBus, max_bytes: int = 200_000) -> bytes:
    out = bytearray()
    for _ in range(max_bytes):
        result = bus.receive_byte()
        if result is None:
            break
        assert isinstance(result, tuple), "Drive1541 should return (byte, eoi) tuples"
        byte, eoi = result
        out.append(byte)
        if eoi:
            break
    else:
        pytest.fail("talker produced more bytes than max_bytes; missing EOI")
    return bytes(out)


def _open_read(bus: IECBus, device: int, channel: int, filename: bytes) -> bytes:
    """Run the full LISTEN/OPEN/UNLISTEN + TALK/DATA/UNTALK sequence."""
    assert bus.send_command(0x20 | device)        # LISTEN dev
    assert bus.send_command(0xF0 | channel)        # OPEN channel
    for i, b in enumerate(filename):
        bus.send_byte(b, eoi=(i == len(filename) - 1))
    assert bus.send_command(0x3F)                  # UNLISTEN
    assert bus.send_command(0x40 | device)         # TALK dev
    assert bus.send_command(0x60 | channel)        # DATA channel
    data = _drain_talker(bus)
    assert bus.send_command(0x5F)                  # UNTALK
    return data


def test_load_directory_via_iec():
    bus, drive, _ = _make_bus_with_drive(8)
    helper = DiskDrive(8)
    helper.attach_disk(drive.disk, "")
    expected = helper.load_file("$")
    assert expected is not None and len(expected) > 2

    got = _open_read(bus, device=8, channel=0, filename=b"$")
    assert got == expected


def test_load_prg_via_iec():
    bus, drive, _ = _make_bus_with_drive(8)
    helper = DiskDrive(8)
    helper.attach_disk(drive.disk, "")
    # ark.d64 is known to contain TEST1.PRG.
    expected = helper.load_file("TEST1.PRG")
    assert expected is not None and len(expected) > 2

    got = _open_read(bus, device=8, channel=0, filename=b"TEST1.PRG")
    assert got == expected


def test_load_missing_file_signals_eoi_quickly():
    bus, _, _ = _make_bus_with_drive(8)

    got = _open_read(bus, device=8, channel=0, filename=b"NOSUCHFILE")
    # File-not-found contract: a single sentinel byte with EOI set, no payload.
    assert got == b"\x00", f"expected single-byte EOI sentinel, got {got!r}"


def test_unlisten_untalk_clear_state():
    bus, _, _ = _make_bus_with_drive(8)

    bus.send_command(0x28)
    bus.send_command(0xF0)
    bus.send_byte(ord("$"), eoi=True)
    bus.send_command(0x3F)  # UNLISTEN
    assert bus.current_listener is None
    assert bus.listener is None
    assert bus.eoi_pending is False

    bus.send_command(0x48)
    bus.send_command(0x60)
    # Pull a byte to set eoi_pending depending on stream length.
    bus.receive_byte()
    bus.send_command(0x5F)  # UNTALK
    assert bus.current_talker is None
    assert bus.talker is None
    assert bus.eoi_pending is False
    assert bus.current_secondary is None
    assert bus.secondary_phase == "idle"


def test_multiple_sequential_loads_no_state_leak():
    bus, drive, _ = _make_bus_with_drive(8)
    helper = DiskDrive(8)
    helper.attach_disk(drive.disk, "")

    # Load directory.
    dir_data = _open_read(bus, 8, 0, b"$")
    assert dir_data == helper.load_file("$")

    # Close channel 0 between loads.
    bus.send_command(0x28)        # LISTEN 8
    bus.send_command(0xE0 | 0)    # CLOSE channel 0
    bus.send_command(0x3F)        # UNLISTEN
    assert 0 not in drive.channels

    # Load a PRG via channel 1 to also exercise multi-channel.
    prg_data = _open_read(bus, 8, 1, b"TEST1.PRG")
    assert prg_data == helper.load_file("TEST1.PRG")

    # And a third sequence reusing channel 0 again.
    dir_data2 = _open_read(bus, 8, 0, b"$")
    assert dir_data2 == dir_data
