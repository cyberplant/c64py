"""Tests for joystick-from-keyboard (item C) and TOML keymap (item E).

Covers:

* :meth:`MemoryMap.set_joystick_dir` / :meth:`clear_joystick_dir` pulling the
  right CIA1 PRA (port 2) / PRB (port 1) bits low at scan time, layered on
  top of the matrix and the time-windowed ``--inject-keys`` masks.
* :func:`host_keymap.build_host_to_joystick` resolving TOML-shaped configs
  (single string or list of strings; aliases like ``"RCtrl"`` / ``"Up"``)
  into ``pygame.K_*`` codes with port + bit pairs.
"""

from __future__ import annotations

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from c64py.memory import MemoryMap


def _kernal_idle(mem: MemoryMap) -> None:
    """Configure CIA1 in the KERNAL-default direction with no keys driven."""
    mem._write_cia1(0x02, 0xFF)  # DDRA = output
    mem._write_cia1(0x03, 0x00)  # DDRB = input
    mem._write_cia1(0x00, 0xFF)
    mem._write_cia1(0x01, 0xFF)


class TestJoystickHeld(unittest.TestCase):
    def test_port2_held_pulls_pra_bits_low(self) -> None:
        mem = MemoryMap()
        _kernal_idle(mem)
        # Up = 0x01, Down = 0x02, Left = 0x04, Right = 0x08, Fire = 0x10.
        mem.set_joystick_dir(2, 0x01)  # up
        mem.set_joystick_dir(2, 0x10)  # fire
        self.assertEqual(mem._read_cia1(0x00), 0xFF & ~(0x01 | 0x10))
        # Port 1 (PRB) is untouched.
        self.assertEqual(mem._read_cia1(0x01), 0xFF)

    def test_port1_held_pulls_prb_bits_low(self) -> None:
        mem = MemoryMap()
        _kernal_idle(mem)
        mem.set_joystick_dir(1, 0x04)  # left
        self.assertEqual(mem._read_cia1(0x01), 0xFF & ~0x04)
        self.assertEqual(mem._read_cia1(0x00), 0xFF)

    def test_clear_joystick_dir(self) -> None:
        mem = MemoryMap()
        _kernal_idle(mem)
        mem.set_joystick_dir(2, 0x01 | 0x08)
        mem.clear_joystick_dir(2, 0x01)
        self.assertEqual(mem._read_cia1(0x00), 0xFF & ~0x08)
        mem.clear_joystick_dir(2, 0x08)
        self.assertEqual(mem._read_cia1(0x00), 0xFF)

    def test_release_all_joystick(self) -> None:
        mem = MemoryMap()
        _kernal_idle(mem)
        mem.set_joystick_dir(1, 0x10)
        mem.set_joystick_dir(2, 0x10)
        mem.release_all_joystick()
        self.assertEqual(mem._read_cia1(0x00), 0xFF)
        self.assertEqual(mem._read_cia1(0x01), 0xFF)

    def test_held_layers_with_inject_and_matrix(self) -> None:
        """Joystick held + --inject-keys + matrix all OR together (active-low)."""
        mem = MemoryMap()
        _kernal_idle(mem)
        # Press matrix 'A' = (1, 2) and drive PA bit 1 low → expect PB bit 2 low.
        mem.press_matrix_key(1, 2)
        mem._write_cia1(0x00, 0xFD)  # PA bit 1 driven
        # Held joystick port 1 bit 0 (up) → PB bit 0 also low.
        mem.set_joystick_dir(1, 0x01)
        # --inject-keys port 2 bit 4 (fire) → PA bit 4 low.
        mem.arm_joystick_inject(2, 0x10, until_cycle=10_000)
        prb = mem._read_cia1(0x01)
        # PA bit 1 is *output* (driven low). On read it's the latch (0xFD)
        # ANDed with ~inject2 (~0x10 = 0xEF) → 0xFD & 0xEF = 0xED.
        pra = mem._read_cia1(0x00)
        self.assertEqual(pra, 0xED)
        # PB: bit 2 (matrix), bit 0 (held joystick port 1).
        expected_prb = 0xFF & ~(1 << 2) & ~(1 << 0)
        self.assertEqual(prb, expected_prb)


class TestHostToJoystick(unittest.TestCase):
    def setUp(self) -> None:
        # Headless pygame init for K_* constants (no display required).
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        try:
            import pygame  # noqa: F401
        except ImportError:
            self.skipTest("pygame not installed")

    def test_default_config_maps_arrows_and_fire(self) -> None:
        import pygame
        from c64py.host_keymap import (
            DEFAULT_JOYSTICK_CONFIG,
            JOY_BIT_DOWN,
            JOY_BIT_FIRE,
            JOY_BIT_LEFT,
            JOY_BIT_RIGHT,
            JOY_BIT_UP,
            build_host_to_joystick,
        )

        m = build_host_to_joystick(DEFAULT_JOYSTICK_CONFIG)
        self.assertEqual(m[pygame.K_UP], [(2, JOY_BIT_UP)])
        self.assertEqual(m[pygame.K_DOWN], [(2, JOY_BIT_DOWN)])
        self.assertEqual(m[pygame.K_LEFT], [(2, JOY_BIT_LEFT)])
        self.assertEqual(m[pygame.K_RIGHT], [(2, JOY_BIT_RIGHT)])
        # Fire is multi-key (RCtrl + Space).
        self.assertEqual(m[pygame.K_RCTRL], [(2, JOY_BIT_FIRE)])
        self.assertEqual(m[pygame.K_SPACE], [(2, JOY_BIT_FIRE)])

    def test_none_config_uses_defaults(self) -> None:
        import pygame
        from c64py.host_keymap import build_host_to_joystick

        m = build_host_to_joystick(None)
        self.assertIn(pygame.K_UP, m)

    def test_custom_port1_wasd(self) -> None:
        import pygame
        from c64py.host_keymap import (
            JOY_BIT_DOWN,
            JOY_BIT_FIRE,
            JOY_BIT_LEFT,
            JOY_BIT_RIGHT,
            JOY_BIT_UP,
            build_host_to_joystick,
        )

        cfg = {
            "port1": {
                "up": "W",
                "down": "S",
                "left": "A",
                "right": "D",
                "fire": "LShift",
            },
            "port2": {},
        }
        m = build_host_to_joystick(cfg)
        self.assertEqual(m[pygame.K_w], [(1, JOY_BIT_UP)])
        self.assertEqual(m[pygame.K_s], [(1, JOY_BIT_DOWN)])
        self.assertEqual(m[pygame.K_a], [(1, JOY_BIT_LEFT)])
        self.assertEqual(m[pygame.K_d], [(1, JOY_BIT_RIGHT)])
        self.assertEqual(m[pygame.K_LSHIFT], [(1, JOY_BIT_FIRE)])
        # Port 2 absent → no entries for it.
        self.assertNotIn(pygame.K_UP, m)

    def test_unknown_key_silently_dropped(self) -> None:
        import pygame  # noqa: F401
        from c64py.host_keymap import build_host_to_joystick

        cfg = {"port2": {"up": "ThisKeyDoesNotExist"}}
        m = build_host_to_joystick(cfg)
        # No exception, no entries for that bogus name.
        self.assertEqual(m, {})

    def test_one_key_two_ports(self) -> None:
        import pygame
        from c64py.host_keymap import JOY_BIT_FIRE, build_host_to_joystick

        cfg = {
            "port1": {"fire": "Space"},
            "port2": {"fire": "Space"},
        }
        m = build_host_to_joystick(cfg)
        self.assertEqual(
            sorted(m[pygame.K_SPACE]),
            [(1, JOY_BIT_FIRE), (2, JOY_BIT_FIRE)],
        )

    def test_quote_key_same_matrix_as_at(self) -> None:
        import pygame
        from c64py.host_keymap import ShiftReq, build_host_to_matrix

        m = build_host_to_matrix()
        self.assertEqual(m[pygame.K_QUOTE][:2], m[pygame.K_AT][:2])
        self.assertEqual(m[pygame.K_QUOTE][2], ShiftReq.NONE)


if __name__ == "__main__":
    unittest.main()
