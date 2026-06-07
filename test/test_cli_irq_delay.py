"""CLI / SEI / PLP one-instruction IRQ delay (canonical 6502 semantics).

Real 6502 hardware — and VICE, our reference emulator — polls the IRQ line
**during** each instruction's penultimate cycle, using the I flag value that
is effective *at that moment*. The flag manipulation by CLI / SEI / PLP only
becomes visible to IRQ polling on the **following** instruction.

Concretely, for a pending IRQ latched before ``CLI``:

* ``CLI`` clears ``I`` by the end of its execution, but the poll inside
  ``CLI`` still sees the *old* ``I=1`` → no IRQ dispatched after ``CLI``.
* The **next** instruction runs normally (its opcode fetch and body are
  unaffected by the latched IRQ). At the end of *that* instruction the poll
  sees the now-cleared ``I=0`` → IRQ is dispatched.

This is the bug that historically broke a number of demos and games in
our emulator: a raster IRQ is latched, the game executes ``CLI``, and we
dispatch the handler immediately — one instruction too early —
corrupting the expected PC / register state and sending execution to a
phantom address.

The regression test below dials this down to the minimum reproducer: no
KERNAL, no VIC cycle machine, a hand-rolled pending CIA IRQ, then
``SEI`` / ``CLI`` / ``NOP`` and a byte-accurate PC check after each step.
"""

from __future__ import annotations

import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from c64py.cpu import CPU6502  # noqa: E402
from c64py.memory import MemoryMap  # noqa: E402


def _bare_cpu() -> tuple[CPU6502, MemoryMap]:
    """CPU6502 + MemoryMap with no KERNAL/BASIC ROMs (Python-only, fast VIC)."""
    mem = MemoryMap()
    cpu = CPU6502(mem, interface=None, accurate_vic=False)
    cpu.state.pc = 0x0800
    cpu.state.sp = 0xFD
    cpu.state.p = 0x20
    cpu.state.a = 0x00
    cpu.state.x = 0x00
    cpu.state.y = 0x00
    cpu.state.cycles = 0
    return cpu, mem


def _install_irq_vector(mem: MemoryMap, handler: int = 0x2000) -> None:
    """Point IRQ/BRK vector at $FFFE/$FFFF to ``handler`` and plant an RTI there.

    The handler itself is trivial (``RTI`` at ``handler``) — tests only care
    whether PC jumped to the handler, not what it does.
    """
    mem.ram[0xFFFE] = handler & 0xFF
    mem.ram[0xFFFF] = (handler >> 8) & 0xFF
    mem.ram[handler] = 0x40  # RTI


def _raise_pending_cia_irq(mem: MemoryMap) -> None:
    """Simulate a latched CIA1 IRQ (as if Timer A just underflowed)."""
    mem.cia1_icr = 0x81  # bit7=IRQ asserted, bit0=Timer-A source
    mem.recompute_pending_irq()
    assert mem.pending_irq is True


def test_cli_delays_irq_by_one_instruction() -> None:
    """Canonical: CLI with a pending IRQ must let ONE instruction run first.

    Program::

        0800: 78        SEI        ; I := 1  (mask IRQs while we arm one)
        0801: 58        CLI        ; I := 0  — but IRQ must NOT dispatch yet
        0802: EA        NOP        ; must execute — IRQ dispatches at its END
        0803: EA        NOP        ; never reached before handler
    """
    cpu, mem = _bare_cpu()
    _install_irq_vector(mem)

    mem.ram[0x0800] = 0x78  # SEI
    mem.ram[0x0801] = 0x58  # CLI
    mem.ram[0x0802] = 0xEA  # NOP
    mem.ram[0x0803] = 0xEA  # NOP

    cpu.step()
    assert cpu.state.pc == 0x0801, f"after SEI, PC={cpu.state.pc:04X}"
    assert (cpu.state.p & 0x04) != 0, "I flag must be set after SEI"

    _raise_pending_cia_irq(mem)

    cpu.step()
    assert (cpu.state.p & 0x04) == 0, "I flag must be clear after CLI"
    assert cpu.state.pc == 0x0802, (
        f"CLI-delay bug: PC={cpu.state.pc:04X} after CLI "
        f"(expected 0x0802 — the NOP). IRQ was dispatched one instruction "
        f"early; real 6502/VICE delays the CLI effect by one instruction."
    )

    cpu.step()
    assert cpu.state.pc == 0x2000, (
        f"after the post-CLI NOP, IRQ should dispatch: PC={cpu.state.pc:04X}"
    )


def test_cli_without_pending_irq_is_a_noop() -> None:
    """Sanity: CLI with no IRQ pending must simply fall through to the next op."""
    cpu, mem = _bare_cpu()
    _install_irq_vector(mem)

    mem.ram[0x0800] = 0x78  # SEI
    mem.ram[0x0801] = 0x58  # CLI
    mem.ram[0x0802] = 0xEA  # NOP

    for expected_pc in (0x0801, 0x0802, 0x0803):
        cpu.step()
        assert cpu.state.pc == expected_pc, (
            f"expected PC={expected_pc:04X}, got {cpu.state.pc:04X}"
        )


def test_plp_clearing_i_delays_irq_by_one_instruction() -> None:
    """``PLP`` restoring I=0 also has a one-instruction delay (same mechanism)."""
    cpu, mem = _bare_cpu()
    _install_irq_vector(mem)

    mem.ram[0x0800] = 0x78  # SEI      (I := 1)
    mem.ram[0x0801] = 0xA9  # LDA #$20 (P with I clear, unused bit set)
    mem.ram[0x0802] = 0x20
    mem.ram[0x0803] = 0x48  # PHA      (push desired P onto stack)
    mem.ram[0x0804] = 0x28  # PLP      (pull P → I := 0 — delayed)
    mem.ram[0x0805] = 0xEA  # NOP      (must run before IRQ dispatch)
    mem.ram[0x0806] = 0xEA  # NOP

    cpu.step()
    cpu.step()
    cpu.step()
    assert cpu.state.pc == 0x0804

    _raise_pending_cia_irq(mem)

    cpu.step()
    assert (cpu.state.p & 0x04) == 0, "I flag must be clear after PLP"
    assert cpu.state.pc == 0x0805, (
        f"PLP-delay bug: PC={cpu.state.pc:04X} (expected 0x0805)"
    )

    cpu.step()
    assert cpu.state.pc == 0x2000, (
        f"IRQ should dispatch after the post-PLP NOP; PC={cpu.state.pc:04X}"
    )
