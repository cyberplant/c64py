"""6510 processor port: INC/DEC must perform the 6502 dummy write on $00/$01."""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from c64py.cpu import CPU6502
from c64py.memory import MemoryMap


class Test6510RmwPort(unittest.TestCase):
    def test_inc_01_dummy_write_then_increment(self) -> None:
        mem = MemoryMap()
        mem.ram[0x00] = 0x2F
        mem.ram[0x01] = 0x37
        # Program: INC $01 at $0200
        mem.ram[0x0200] = 0xE6
        mem.ram[0x0201] = 0x01
        mem.ram[0x0202] = 0x00  # BRK

        cpu = CPU6502(mem, interface=None, accurate_vic=False)
        cpu.state.pc = 0x0200
        cpu.state.sp = 0xFD

        cycles = cpu.step()
        self.assertEqual(cycles, 5)
        self.assertEqual(mem.ram[0x01], 0x38)
        self.assertEqual(cpu.state.pc, 0x0202)

    def test_dec_01_dummy_write_then_decrement(self) -> None:
        mem = MemoryMap()
        mem.ram[0x00] = 0x2F
        mem.ram[0x01] = 0x37
        mem.ram[0x0200] = 0xC6  # DEC zp
        mem.ram[0x0201] = 0x01
        mem.ram[0x0202] = 0x00

        cpu = CPU6502(mem, interface=None, accurate_vic=False)
        cpu.state.pc = 0x0200
        cpu.state.sp = 0xFD

        cpu.step()
        self.assertEqual(mem.ram[0x01], 0x36)


if __name__ == "__main__":
    unittest.main()
