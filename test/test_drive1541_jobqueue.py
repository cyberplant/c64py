"""Stage-4 tests: 1541 job-queue trap services D64 reads/writes."""
import pytest

from c64py.drives.c1541_emulator import Drive1541
from c64py.iec_bus import IECBus
from c64py.d64 import create_blank_d64


def make_drive_with_disk(name: str = "TEST") -> Drive1541:
    bus = IECBus()
    drive = Drive1541(device_number=8)
    drive.load_rom(b"\x00" * 16384)
    bus.attach_device(drive)
    drive.notify_bus_change()
    drive.attach_disk(create_blank_d64(disk_name=name, disk_id="01"))
    return drive


def _set_job(drive: Drive1541, buf: int, code: int, track: int, sector: int):
    """Lay down T/S then write the job code (matches what real DOS does)."""
    drive.memory.write(0x06 + buf * 2, track)
    drive.memory.write(0x07 + buf * 2, sector)
    drive.memory.write(buf, code)


def test_read_job_loads_sector_into_buffer():
    drive = make_drive_with_disk()
    # Write a known pattern into the underlying D64 at track 18, sector 0
    # (BAM/header sector — we'll replace it with a marker).
    pattern = bytes([(i * 7 + 3) & 0xFF for i in range(256)])
    drive.disk.write_sector(18, 0, pattern)

    _set_job(drive, buf=0, code=0x80, track=18, sector=0)

    # After the job-code write, the trap should have completed:
    # status byte at $00 = 0x01 (OK), and buffer 0 ($0300-$03FF) holds
    # the sector data.
    assert drive.memory.ram[0] == 0x01
    buf0 = bytes(drive.memory.ram[0x0300:0x0400])
    assert buf0 == pattern


def test_write_job_persists_to_disk():
    drive = make_drive_with_disk()
    payload = bytes(range(256))
    # Place payload in buffer 1 ($0400-$04FF)
    for i, b in enumerate(payload):
        drive.memory.ram[0x0400 + i] = b
    _set_job(drive, buf=1, code=0x90, track=20, sector=5)

    assert drive.memory.ram[1] == 0x01
    assert drive.disk.read_sector(20, 5) == payload


def test_invalid_track_returns_header_error():
    drive = make_drive_with_disk()
    _set_job(drive, buf=0, code=0x80, track=99, sector=0)
    # Should not crash, status indicates header-not-found.
    assert drive.memory.ram[0] == 0x02


def test_seek_bump_verify_succeed_as_noop():
    drive = make_drive_with_disk()
    for code in (0xA0, 0xB0, 0xC0):
        _set_job(drive, buf=0, code=code, track=18, sector=0)
        assert drive.memory.ram[0] == 0x01, f"job {code:#04x} should succeed"


def test_no_disk_attached_returns_drive_not_ready():
    drive = Drive1541(device_number=8)
    drive.load_rom(b"\x00" * 16384)
    _set_job(drive, buf=0, code=0x80, track=18, sector=0)
    assert drive.memory.ram[0] == 0x0F


def test_inactive_job_code_is_ignored():
    drive = make_drive_with_disk()
    # High bit clear -> not an active job. Should not modify the slot.
    drive.memory.write(0, 0x01)
    assert drive.memory.ram[0] == 0x01
    drive.memory.write(0, 0x02)
    assert drive.memory.ram[0] == 0x02


def test_write_job_does_not_corrupt_other_sectors():
    drive = make_drive_with_disk()
    original_bam = drive.disk.read_sector(18, 0)
    payload = bytes([0xFF] * 256)
    for i in range(256):
        drive.memory.ram[0x0500 + i] = payload[i]
    _set_job(drive, buf=2, code=0x90, track=1, sector=0)
    assert drive.memory.ram[2] == 0x01
    assert drive.disk.read_sector(1, 0) == payload
    assert drive.disk.read_sector(18, 0) == original_bam
