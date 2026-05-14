"""KERNAL LOAD/SAVE hook must RTS (PC + SP) on error paths — else the CPU loop spins at $FFD5/$FFD8."""

from __future__ import annotations

import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from c64py.drives.tcp_drive_client import TcpDriveClient  # noqa: E402
from c64py.emulator import C64  # noqa: E402


class _CaptureSaveClient(TcpDriveClient):
    """TcpDriveClient that records ``fast_save`` without a real socket."""

    def __init__(self) -> None:
        super().__init__(8, "localhost", 1)
        self.last_save: tuple[str, bytes] | None = None

    def connect(self) -> bool:  # type: ignore[override]
        return True

    def fast_save(self, filename: str, data: bytes) -> tuple:  # type: ignore[override]
        self.last_save = (filename, bytes(data))
        return (True, None)


def test_kernal_load_no_disk_pops_return_address() -> None:
    emu = C64(interface_factory=lambda _e: None)
    emu._initialize_c64()
    emu.memory.write(0xBA, 8)  # device 8, no drive attached
    emu.cpu.state.pc = 0xFFD5
    emu.cpu.state.sp = 0xFD
    emu.memory.write(0x01FE, 0x34)
    emu.memory.write(0x01FF, 0x12)
    assert emu._handle_kernal_load() is True
    assert emu.cpu.state.pc == 0x1235
    assert emu.cpu.state.sp == 0xFF
    assert emu.cpu.state.p & 0x01


def test_kernal_save_uses_zp_index_for_start_xy_for_end() -> None:
    """SAVE ($FFD8): A indexes ZP start pointer; X/Y are end (exclusive), per KERNAL $F5DD."""
    emu = C64(interface_factory=lambda _e: None)
    emu._initialize_c64()
    emu.kernal_load_shortcut_enabled = True
    cap = _CaptureSaveClient()
    emu.iec_drives[8] = cap

    # SETNAM-style filename "P" at $1000
    emu.memory.write(0xB7, 1)
    emu.memory.write(0xBB, 0x00)
    emu.memory.write(0xBC, 0x10)
    emu.memory.write(0x1000, ord("P"))
    emu.memory.write(0xBA, 8)

    # BASIC-style: start pointer at $2B/$2C -> $0801
    emu.memory.write(0x2B, 0x01)
    emu.memory.write(0x2C, 0x08)
    for a in range(0x0801, 0x0810):
        emu.memory.write(a, (a ^ 0x55) & 0xFF)

    emu.cpu.state.pc = 0xFFD8
    emu.cpu.state.a = 0x2B
    emu.cpu.state.x = 0x10
    emu.cpu.state.y = 0x08
    emu.cpu.state.sp = 0xFD
    emu.memory.write(0x01FE, 0x34)
    emu.memory.write(0x01FF, 0x12)

    assert emu._handle_kernal_save() is True
    assert emu.cpu.state.pc == 0x1235
    assert emu.cpu.state.sp == 0xFF
    assert (emu.cpu.state.p & 0x01) == 0
    assert cap.last_save is not None
    fn, payload = cap.last_save
    assert fn == "P"
    assert payload[0:2] == bytes([0x01, 0x08])
    assert len(payload) == 2 + (0x0810 - 0x0801)
    assert payload[2:] == bytes(((a ^ 0x55) & 0xFF) for a in range(0x0801, 0x0810))


def test_kernal_save_no_disk_pops_return_address() -> None:
    emu = C64(interface_factory=lambda _e: None)
    emu._initialize_c64()
    emu.memory.write(0xBA, 8)
    emu.cpu.state.pc = 0xFFD8
    emu.cpu.state.sp = 0xFD
    emu.memory.write(0x01FE, 0x78)
    emu.memory.write(0x01FF, 0x56)
    assert emu._handle_kernal_save() is True
    assert emu.cpu.state.pc == 0x5679
    assert emu.cpu.state.sp == 0xFF
    assert emu.cpu.state.p & 0x01
