"""KERNAL LOAD/SAVE hook must RTS (PC + SP) on error paths — else the CPU loop spins at $FFD5/$FFD8."""

from __future__ import annotations

import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from c64py.emulator import C64  # noqa: E402


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
