"""Tests for D64 write-back support (D64Image.write_file and Drive.save_file)."""

from __future__ import annotations

import os
import tempfile

import pytest

from c64py.d64 import D64Image, D64_SIZE_STANDARD, create_blank_d64, load_d64
from c64py.drives.drive import DiskDrive


def _make_blank_disk():
    return create_blank_d64("TESTDISK", "01")


def test_write_file_then_read_back_same_bytes():
    """A small PRG written via write_file should round-trip via read_file."""
    disk = _make_blank_disk()
    payload = bytes([0x01, 0x08]) + bytes(range(100))  # load addr + 100 bytes

    assert disk.write_file("HELLO", payload) is True

    entries = {e.filename.rstrip(): e for e in disk.read_directory()}
    assert "HELLO" in entries
    entry = entries["HELLO"]
    assert entry.filetype == 2  # PRG
    assert entry.blocks == 1  # 102 bytes fits in one 254-byte data sector

    got = disk.read_file(entry)
    assert got == payload


def test_full_roundtrip_through_disk_file():
    """Persist via save_to_file, reload, list, load, verify bytes."""
    drive = DiskDrive()

    with tempfile.TemporaryDirectory() as td:
        d64_path = os.path.join(td, "scratch.d64")
        disk = _make_blank_disk()
        disk.save_to_file(d64_path)

        # Reopen via load_d64 to mirror real usage.
        loaded = load_d64(d64_path)
        drive.attach_disk(loaded, d64_path)

        # Multi-sector payload to exercise sector chaining.
        payload = bytes([0x01, 0x08]) + bytes((i * 7) & 0xFF for i in range(700))

        assert drive.save_file('"PROG"', payload) is True
        assert drive.last_error == (0, "OK", 0, 0)

        # Reload from the persisted file: changes must be on disk.
        reread = load_d64(d64_path)
        names = [e.filename.rstrip() for e in reread.read_directory()]
        assert "PROG" in names

        loaded_back = DiskDrive()
        loaded_back.attach_disk(reread, d64_path)
        got = loaded_back.load_file("PROG")
        assert got == payload


def test_file_exists_returns_false_and_sets_error():
    """Writing the same name twice must fail with FILE EXISTS (code 63)."""
    drive = DiskDrive()
    drive.attach_disk(_make_blank_disk(), "")

    payload = bytes([0x01, 0x08, 0x42, 0x42, 0x42])
    assert drive.save_file("DUPE", payload) is True
    assert drive.last_error == (0, "OK", 0, 0)

    assert drive.save_file("DUPE", payload) is False
    code, message, _, _ = drive.last_error
    assert code == 63
    assert "EXISTS" in message.upper()


def test_save_file_no_disk_sets_drive_not_ready():
    """save_file with no disk attached returns False and sets code 74."""
    drive = DiskDrive()
    assert drive.save_file("X", b"\x01\x08AB") is False
    code, message, _, _ = drive.last_error
    assert code == 74
    assert "NOT READY" in message.upper()


def test_disk_full_returns_false():
    """Filling the disk should eventually return False with DISK FULL (72)."""
    drive = DiskDrive()
    drive.attach_disk(_make_blank_disk(), "")

    # Each saved file uses ~3 sectors (one 254-byte sector + entry overhead).
    # A blank disk has 664 user-data blocks; saving ~700 single-sector files
    # of unique names is more than enough to hit "disk full".
    big = bytes([0x01, 0x08]) + b"X" * 250  # 1 data sector
    full_seen = False
    for i in range(700):
        name = f"F{i:04d}"
        if not drive.save_file(name, big):
            code, _, _, _ = drive.last_error
            assert code == 72, f"Expected DISK FULL (72), got {drive.last_error!r}"
            full_seen = True
            break
    assert full_seen, "Disk never reported full after 700 writes"
