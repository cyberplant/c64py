"""Color RAM ($D800+) is 4-bit hardware; CPU still uses 8-bit loads/stores (VICE-compatible)."""

import unittest

from c64py.memory import MemoryMap


class TestColorRamCpuByte(unittest.TestCase):
    def test_roundtrip_full_byte(self) -> None:
        m = MemoryMap()
        m.write(0xDA89, 0xD6)
        self.assertEqual(m.read(0xDA89), 0xD6)
        self.assertEqual(m.ram[0xDA89], 0xD6)

    def test_low_nibble_still_available_for_vic(self) -> None:
        m = MemoryMap()
        m.write(0xD800, 0xD6)
        self.assertEqual(m.read(0xD800) & 0x0F, 0x06)


if __name__ == "__main__":
    unittest.main()
