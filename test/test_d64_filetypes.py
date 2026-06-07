"""SEQ / USR / REL filename parsing and D64 read paths."""
from __future__ import annotations

from c64py.d64 import (
    D64DirEntry,
    create_blank_d64,
    dos_filetype_byte_closed,
    load_d64,
    parse_commodore_filename_mode,
)
from c64py.drives.drive import DiskDrive


def test_parse_commodore_filename_mode_strip_open():
    assert parse_commodore_filename_mode("DATA,S,W") == ("DATA", 1)
    assert parse_commodore_filename_mode("FOO,P,R") == ("FOO", 2)
    assert parse_commodore_filename_mode("BAR") == ("BAR", None)


def test_dos_filetype_byte_closed():
    assert dos_filetype_byte_closed(2) == 0x82
    assert dos_filetype_byte_closed(1) == 0x81


def test_disk_drive_seq_roundtrip(tmp_path):
    img = create_blank_d64("SEQDISK", "01")
    p = tmp_path / "s.d64"
    img.save_to_file(str(p))
    img2 = load_d64(str(p))
    drv = DiskDrive(8)
    drv.attach_disk(img2, str(p))
    body = b"hello sequential file"
    assert drv.save_file("NOTE,S,W", body)
    got = drv.load_file("NOTE,S,R")
    assert got is not None
    assert got[:2] == b"\x01\x08"
    assert got[2:] == body


def test_disk_drive_type_mismatch(tmp_path):
    img = create_blank_d64("T", "01")
    img.write_file("ONLYPRG", b"\x01\x08\xEA", filetype=0x82)
    p = tmp_path / "m.d64"
    img.save_to_file(str(p))
    d = load_d64(str(p))
    drv = DiskDrive(8)
    drv.attach_disk(d, str(p))
    assert drv.load_file("ONLYPRG,S,R") is None
    assert drv.last_error[0] == 64


def test_read_rel_file_one_pointer():
    img = create_blank_d64("REL", "01")
    ts_data = img._alloc_sector()
    ts_side = img._alloc_sector()
    assert ts_data and ts_side
    t_d, s_d = ts_data
    t_ss, s_ss = ts_side
    pl = bytearray(256)
    pl[0] = 0
    pl[1] = 3
    pl[2:5] = b"XYZ"
    img.write_sector(t_d, s_d, bytes(pl))
    ss = bytearray(256)
    ss[0] = 0
    ss[1] = 0xFF
    ss[4] = t_d
    ss[5] = s_d
    img.write_sector(t_ss, s_ss, bytes(ss))
    ent = D64DirEntry(
        filetype=4, filename="R", track=t_ss, sector=s_ss, blocks=1
    )
    assert img.read_rel_file(ent) == b"XYZ"


def test_kernal_verify_match_and_mismatch():
    from c64py.emulator import C64

    class _Stub:
        def fast_load(self, filename: str, secondary: int = 0):
            return bytes([0x00, 0x04, 0xAA, 0x55]), None, 2

    emu = C64(interface_factory=lambda _e: None)
    emu.interface = type("I", (), {"add_debug_log": lambda *a, **k: None})()
    emu._initialize_c64()
    emu.kernal_load_shortcut_enabled = True
    emu.get_drive = lambda d: _Stub() if d == 8 else None  # type: ignore[method-assign]
    emu.memory.write(0xBA, 8)
    emu.memory.write(0xB9, 0)
    emu.memory.write(0xB7, 4)
    emu.memory.write(0xBB, 0x00)
    emu.memory.write(0xBC, 0x10)
    for i, c in enumerate("TEST"):
        emu.memory.write(0x1000 + i, ord(c))
    emu.memory.write(0x0400, 0xAA)
    emu.memory.write(0x0401, 0x55)
    emu.cpu.state.pc = 0xFFD5
    emu.cpu.state.a = 1  # VERIFY
    emu.cpu.state.sp = 0xFD
    emu.memory.write(0x01FE, 0x99)
    emu.memory.write(0x01FF, 0x99)
    assert emu._handle_kernal_load() is True
    assert not (emu.cpu.state.p & 0x01)

    emu.memory.write(0x0400, 0xEE)
    emu.cpu.state.pc = 0xFFD5
    emu.cpu.state.a = 1
    assert emu._handle_kernal_load() is True
    assert emu.cpu.state.p & 0x01
    assert emu.cpu.state.a == 28
    emu.memory.write(0xB9, 0)
    emu.memory.write(0xB7, 4)
    emu.memory.write(0xBB, 0x00)
    emu.memory.write(0xBC, 0x10)
    for i, c in enumerate("TEST"):
        emu.memory.write(0x1000 + i, ord(c))
    emu.memory.write(0x0400, 0xAA)
    emu.memory.write(0x0401, 0x55)
    emu.cpu.state.pc = 0xFFD5
    emu.cpu.state.a = 1  # VERIFY
    emu.cpu.state.sp = 0xFD
    emu.memory.write(0x01FE, 0x99)
    emu.memory.write(0x01FF, 0x99)
    assert emu._handle_kernal_load() is True
    assert not (emu.cpu.state.p & 0x01)

    emu.memory.write(0x0400, 0xEE)
    emu.cpu.state.pc = 0xFFD5
    emu.cpu.state.a = 1
    assert emu._handle_kernal_load() is True
    assert emu.cpu.state.p & 0x01
    assert emu.cpu.state.a == 28
