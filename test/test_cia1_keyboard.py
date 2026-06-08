"""Tests for CIA1 keyboard-matrix scanning (item A in input_config_plan.md).

Synthesizes the standard KERNAL scan loop — write a row-select mask to PRA
($DC00), read PRB ($DC01) — and verifies that the right column bit gets
pulled low for every (row, col) slot in the 8x8 matrix. Convention: ``row``
is the $DC00 bit the KERNAL drives low; ``col`` is the $DC01 bit it reads
back. Also exercises SHIFT coexistence, the inverse scan direction, and
the joy_inject masking layer that ``--inject-keys`` relies on.
"""

from __future__ import annotations

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from c64py.memory import MemoryMap


def _kernal_scan_state(mem: MemoryMap) -> None:
    """Configure CIA1 the way the KERNAL ISR sets it up: PRA out, PRB in."""
    mem._write_cia1(0x02, 0xFF)  # DDRA = output
    mem._write_cia1(0x03, 0x00)  # DDRB = input
    mem._write_cia1(0x00, 0xFF)  # all rows idle (high)
    mem._write_cia1(0x01, 0xFF)


def _drive_row_low(mem: MemoryMap, row: int) -> int:
    """Standard scan: drive a single row low on PRA, return PRB read."""
    mem._write_cia1(0x00, (~(1 << row)) & 0xFF)
    return mem._read_cia1(0x01)


def _drive_column_low(mem: MemoryMap, col: int) -> int:
    """Inverse scan: drive a single column low on PRB, return PRA read."""
    mem._write_cia1(0x02, 0x00)  # DDRA = input
    mem._write_cia1(0x03, 0xFF)  # DDRB = output
    mem._write_cia1(0x00, 0xFF)
    mem._write_cia1(0x01, (~(1 << col)) & 0xFF)
    return mem._read_cia1(0x00)


class TestCia1Keyboard(unittest.TestCase):
    def test_idle_reads_all_high(self) -> None:
        mem = MemoryMap()
        _kernal_scan_state(mem)
        self.assertEqual(mem._read_cia1(0x00), 0xFF)
        self.assertEqual(mem._read_cia1(0x01), 0xFF)

    def test_every_matrix_slot_pulls_correct_column(self) -> None:
        """Press one key at a time, drive its row, expect its column low on PRB."""
        for row in range(8):
            for col in range(8):
                mem = MemoryMap()
                _kernal_scan_state(mem)
                mem.press_matrix_key(row, col)
                prb = _drive_row_low(mem, row)
                # Only the pressed column should be 0; others remain high.
                expected = (~(1 << col)) & 0xFF
                self.assertEqual(
                    prb,
                    expected,
                    f"row={row} col={col}: PRB=0x{prb:02X} expected=0x{expected:02X}",
                )
                # Driving a different (idle) row should leave PRB high.
                idle_row = (row + 1) & 0x07
                if idle_row != row:
                    prb_idle = _drive_row_low(mem, idle_row)
                    self.assertEqual(
                        prb_idle,
                        0xFF,
                        f"unexpected pulldown when driving idle row {idle_row}",
                    )

    def test_inverse_scan_pulls_row(self) -> None:
        """Drive a column low on PRB and read PRA — row of the pressed key drops."""
        mem = MemoryMap()
        # Press F1 (row=0, col=4).
        mem.press_matrix_key(0, 4)
        pra = _drive_column_low(mem, 4)
        self.assertEqual(pra, (~(1 << 0)) & 0xFF)

    def test_shift_coexists_with_other_key(self) -> None:
        """SHIFT (row 1 col 7) + F1 (row 0 col 4): driving row 0 pulls col 4;
        driving row 1 pulls col 7. KERNAL uses this to detect F2."""
        mem = MemoryMap()
        _kernal_scan_state(mem)
        mem.press_matrix_key(1, 7)  # LSHIFT
        mem.press_matrix_key(0, 4)  # F1
        self.assertEqual(_drive_row_low(mem, 0), (~(1 << 4)) & 0xFF)
        self.assertEqual(_drive_row_low(mem, 1), (~(1 << 7)) & 0xFF)
        # Driving both rows at once should pull both cols.
        mem._write_cia1(0x00, (~((1 << 0) | (1 << 1))) & 0xFF)
        self.assertEqual(mem._read_cia1(0x01), (~((1 << 4) | (1 << 7))) & 0xFF)

    def test_release_matrix_key(self) -> None:
        mem = MemoryMap()
        _kernal_scan_state(mem)
        mem.press_matrix_key(0, 4)
        self.assertEqual(_drive_row_low(mem, 0), (~(1 << 4)) & 0xFF)
        mem.release_matrix_key(0, 4)
        self.assertEqual(_drive_row_low(mem, 0), 0xFF)

    def test_release_all_keys(self) -> None:
        mem = MemoryMap()
        for row in range(8):
            for col in range(8):
                mem.press_matrix_key(row, col)
        self.assertTrue(any(b != 0 for b in mem.keyboard_matrix))
        mem.release_all_keys()
        self.assertTrue(all(b == 0 for b in mem.keyboard_matrix))
        _kernal_scan_state(mem)
        self.assertEqual(_drive_row_low(mem, 0), 0xFF)

    def test_letter_a_kernal_decode(self) -> None:
        """Regression for the row/col transpose bug: pressing 'A' (1, 2) must
        leave PB bit 2 low when KERNAL drives PA bit 1 low — i.e. the exact
        signal pattern the real KERNAL ISR matches against 'A' in its keytab."""
        mem = MemoryMap()
        _kernal_scan_state(mem)
        mem.press_matrix_key(1, 2)  # 'A' per the C64 matrix
        mem._write_cia1(0x00, 0xFD)  # KERNAL: drive PA bit 1 low
        self.assertEqual(mem._read_cia1(0x01), 0xFB)  # expect PB bit 2 low

    def test_joy_inject_combines_with_matrix(self) -> None:
        """joy_inject2_clear must still pull PRA bits low (active-low OR)."""
        mem = MemoryMap()
        _kernal_scan_state(mem)
        # Joystick 2 fire (bit 4, port 2 → PRA).
        mem.arm_joystick_inject(2, 0x10, until_cycle=10_000)
        pra = mem._read_cia1(0x00)
        # Since we are driving all PRA cols high (idle), PRA reads are
        # output-bit latches → 0xFF, then masked with ~0x10 = 0xEF.
        self.assertEqual(pra, 0xEF)
        # Joystick 1 up (bit 0, port 1 → PRB).
        mem.arm_joystick_inject(1, 0x01, until_cycle=10_000)
        prb = mem._read_cia1(0x01)
        self.assertEqual(prb, 0xFE)

    def test_default_idle_matches_pre_matrix_behavior(self) -> None:
        """Before the matrix existed, _read_cia1 returned 0xFF (no joy inject).
        With KERNAL-default DDRs and no keys pressed, the new scan must agree."""
        mem = MemoryMap()
        _kernal_scan_state(mem)
        self.assertEqual(mem._read_cia1(0x00), 0xFF)
        self.assertEqual(mem._read_cia1(0x01), 0xFF)
        # DDRA/DDRB readback.
        self.assertEqual(mem._read_cia1(0x02), 0xFF)
        self.assertEqual(mem._read_cia1(0x03), 0x00)


if __name__ == "__main__":
    unittest.main()
