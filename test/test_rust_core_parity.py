"""Differential tests: Python ``step()`` vs optional Rust ``step_fast_batch``."""

from __future__ import annotations

import os
import sys

import pytest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from c64py import _core  # noqa: E402
from c64py.cpu import CPU6502  # noqa: E402
from c64py.memory import MemoryMap  # noqa: E402


@pytest.mark.skipif(not _core.is_available, reason="c64py_rust_core not built")
def test_parity_tight_loop() -> None:
    """Short synthetic program: LDA/INX/DEY/BNE — no KERNAL, no CHROUT, no interface."""
    mem_py = MemoryMap()
    mem_rs = MemoryMap()

    # $0800: LDX #10 -> loop: DEX -> BNE loop -> LDA #$42 -> NOP (no BRK / IRQ)
    prog = [
        0xA2,
        0x0A,  # LDX #10
        0xCA,  # DEX
        0xD0,
        0xFD,  # BNE -3
        0xA9,
        0x42,  # LDA #$42
        0xEA,  # NOP
    ]
    base = 0x0800
    for i, b in enumerate(prog):
        mem_py.ram[base + i] = b
        mem_rs.ram[base + i] = b

    cpu_py = CPU6502(mem_py, interface=None, accurate_vic=False)
    cpu_rs = CPU6502(mem_rs, interface=None, accurate_vic=False)
    cpu_py.state.pc = base
    cpu_rs.state.pc = base
    cpu_py.state.sp = 0xFD
    cpu_rs.state.sp = 0xFD

    steps = 80
    for _ in range(steps):
        cpu_py.step()

    ins, cyc = cpu_rs.step_fast_batch(steps)
    assert ins == steps

    assert mem_py.ram == mem_rs.ram
    assert cpu_py.state.pc == cpu_rs.state.pc
    assert cpu_py.state.a == cpu_rs.state.a
    assert cpu_py.state.x == cpu_rs.state.x
    assert cpu_py.state.y == cpu_rs.state.y
    assert cpu_py.state.sp == cpu_rs.state.sp
    assert cpu_py.state.p == cpu_rs.state.p
    assert cpu_py.state.stopped == cpu_rs.state.stopped
    # Cycle totals can differ if Python hit CHROUT/CINT shortcuts (not used here).
    assert cpu_py.state.cycles == cpu_rs.state.cycles
    assert mem_py.raster_line == mem_rs.raster_line
    assert mem_py.raster_cycles == mem_rs.raster_cycles
    assert bytes(mem_py._vic_regs) == bytes(mem_rs._vic_regs)


@pytest.mark.skipif(not _core.is_available, reason="c64py_rust_core not built")
def test_parity_6510_inc01_matches_step() -> None:
    """Same scenario as ``test_6510_rmw_port`` — Rust batch vs Python steps."""
    mem_py = MemoryMap()
    mem_rs = MemoryMap()
    mem_py.ram[0x00] = mem_rs.ram[0x00] = 0x2F
    mem_py.ram[0x01] = mem_rs.ram[0x01] = 0x37
    mem_py.ram[0x0200] = mem_rs.ram[0x0200] = 0xE6
    mem_py.ram[0x0201] = mem_rs.ram[0x0201] = 0x01
    mem_py.ram[0x0202] = mem_rs.ram[0x0202] = 0xEA

    cpu_py = CPU6502(mem_py, interface=None, accurate_vic=False)
    cpu_rs = CPU6502(mem_rs, interface=None, accurate_vic=False)
    cpu_py.state.pc = cpu_rs.state.pc = 0x0200
    cpu_py.state.sp = cpu_rs.state.sp = 0xFD

    cpu_py.step()
    cpu_rs.step_fast_batch(1)

    assert mem_py.ram == mem_rs.ram
    assert cpu_py.state.pc == cpu_rs.state.pc
    assert cpu_py.state.cycles == cpu_rs.state.cycles
