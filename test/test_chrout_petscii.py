"""CHROUT ($FFD2) PETSCII control and printable semantics (Python shortcut)."""

from __future__ import annotations

import pytest

from c64py.constants import COLOR_MEM, SCREEN_MEM
from c64py.cpu import CPU6502
from c64py.memory import MemoryMap


@pytest.fixture
def cpu_chrout() -> CPU6502:
    mem = MemoryMap()
    cpu = CPU6502(mem)
    mem.ram[SCREEN_MEM : SCREEN_MEM + 1000] = [0x20] * 1000
    mem.ram[COLOR_MEM : COLOR_MEM + 1000] = [0x0E] * 1000
    mem.write(0x0286, 0x0E)
    mem.write(0xD1, SCREEN_MEM & 0xFF)
    mem.write(0xD2, (SCREEN_MEM >> 8) & 0xFF)
    return cpu


def test_chrout_rvs_on_sets_screen_bit7(cpu_chrout: CPU6502) -> None:
    cpu = cpu_chrout
    cpu.apply_chrout_petscii(0x12)
    assert cpu.memory.read(SCREEN_MEM) == 0x20
    cpu.apply_chrout_petscii(ord("A"))
    assert cpu.memory.read(SCREEN_MEM) == 0x81
    cpu.apply_chrout_petscii(0x92)
    cpu.apply_chrout_petscii(ord("B"))
    assert cpu.memory.read(SCREEN_MEM + 1) == 0x02


def test_chrout_90_is_black_not_rvs(cpu_chrout: CPU6502) -> None:
    cpu = cpu_chrout
    cpu.apply_chrout_petscii(0x90)
    assert cpu.memory.read(0x0286) == 0
    assert not cpu._chrout_rvs_on
    cpu.apply_chrout_petscii(ord("X"))
    assert cpu.memory.read(SCREEN_MEM) == 0x18
    assert cpu.memory.read(COLOR_MEM) == 0


def test_chrout_color_green_and_red(cpu_chrout: CPU6502) -> None:
    cpu = cpu_chrout
    cpu.apply_chrout_petscii(0x1E)
    assert cpu.memory.read(0x0286) == 5
    cpu.apply_chrout_petscii(ord("G"))
    assert cpu.memory.read(COLOR_MEM) == 5
    cpu.apply_chrout_petscii(0x1C)
    assert cpu.memory.read(0x0286) == 2
    cpu.apply_chrout_petscii(ord("R"))
    assert cpu.memory.read(SCREEN_MEM + 1) == 0x12
    assert cpu.memory.read(COLOR_MEM + 1) == 2


def test_chrout_control_12_does_not_print_or_advance(cpu_chrout: CPU6502) -> None:
    cpu = cpu_chrout
    cpu.apply_chrout_petscii(0x12)
    assert cpu.memory.read(SCREEN_MEM) == 0x20
    assert cpu.memory.read(0xD1) == (SCREEN_MEM & 0xFF)


def test_chrout_cursor_right_does_not_print_bracket(cpu_chrout: CPU6502) -> None:
    cpu = cpu_chrout
    cpu.apply_chrout_petscii(ord("?"))
    assert cpu.memory.read(SCREEN_MEM) == 0x3F
    cpu.apply_chrout_petscii(0x1D)
    assert cpu.memory.read(SCREEN_MEM + 1) == 0x20
    assert cpu.memory.read(0xD3) == 2


def test_chrout_tab_does_not_print_glyph(cpu_chrout: CPU6502) -> None:
    cpu = cpu_chrout
    cpu.apply_chrout_petscii(ord("?"))
    cpu.apply_chrout_petscii(0x09)
    assert cpu.memory.read(SCREEN_MEM + 1) == 0x20
    col = cpu.memory.read(0xD3)
    assert col == 10


def test_chrout_rvs_via_kernal_c7_flag(cpu_chrout: CPU6502) -> None:
    cpu = cpu_chrout
    cpu.memory.write(0xC7, 1)
    cpu.apply_chrout_petscii(ord("Z"))
    assert cpu.memory.read(SCREEN_MEM) == 0x9A
    cpu.memory.write(0xC7, 0)
    cpu.apply_chrout_petscii(ord("Y"))
    assert cpu.memory.read(SCREEN_MEM + 1) == 0x19


def test_chrout_return_clears_rvs(cpu_chrout: CPU6502) -> None:
    cpu = cpu_chrout
    cpu.apply_chrout_petscii(0x12)
    cpu.apply_chrout_petscii(ord("H"))
    cpu.apply_chrout_petscii(0x0D)
    assert not cpu._chrout_rvs_on
    assert cpu.memory.read(0xC7) == 0
    cpu.apply_chrout_petscii(ord("Y"))
    assert cpu.memory.read(SCREEN_MEM + 40) == 0x19


def test_chrout_cyan_violet_yellow(cpu_chrout: CPU6502) -> None:
    cpu = cpu_chrout
    cpu.apply_chrout_petscii(0x9F)
    assert cpu.memory.read(0x0286) == 3
    cpu.apply_chrout_petscii(0x9C)
    assert cpu.memory.read(0x0286) == 4
    cpu.apply_chrout_petscii(0x9E)
    assert cpu.memory.read(0x0286) == 7
