"""Tests for ``--inject-keys`` parsing and payload expansion."""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from c64py.keyboard_inject import (
    expand_inject_payload,
    parse_inject_key_entries,
    parse_inject_key_entry,
)


class TestKeyboardInject(unittest.TestCase):
    def test_parse_append_style(self) -> None:
        rules = parse_inject_key_entries(["3000000c:AB", "45s:{F1}"])
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0].when_cycles, 3000000)
        self.assertIsNone(rules[0].when_seconds)
        self.assertEqual(rules[0].payload_raw, "AB")
        self.assertEqual(rules[1].when_seconds, 45.0)
        self.assertIsNone(rules[1].when_cycles)
        self.assertEqual(rules[1].payload_raw, "{F1}")

    def test_payload_preserves_leading_space_and_embedded_trigger_like_text(self) -> None:
        rule = parse_inject_key_entry("99c: 1c:something")
        self.assertEqual(rule.when_cycles, 99)
        self.assertEqual(rule.payload_raw, " 1c:something")
        b, _, _, _ = expand_inject_payload(rule.payload_raw)
        self.assertTrue(b.startswith(b" "))

    def test_expand_fn_keys(self) -> None:
        b, j1, j2, hold = expand_inject_payload("{F1}")
        self.assertEqual(b, bytes([0x85]))
        self.assertEqual(j1, 0)
        self.assertEqual(j2, 0)
        self.assertEqual(hold, 0)

    def test_expand_joy_sets_hold(self) -> None:
        b, j1, j2, hold = expand_inject_payload("{joy1-fire}")
        self.assertEqual(b, b"")
        self.assertEqual(j1, 0x10)
        self.assertEqual(j2, 0)
        self.assertGreater(hold, 0)

    def test_expand_newline_escape(self) -> None:
        b, _, _, _ = expand_inject_payload("A\\nB")
        self.assertEqual(b, b"A\x0DB")

    def test_empty_payload_allowed(self) -> None:
        rule = parse_inject_key_entry("100c:")
        self.assertEqual(rule.payload_raw, "")

    def test_single_space_payload_not_stripped(self) -> None:
        rule = parse_inject_key_entry("4000000c: ")
        self.assertEqual(rule.payload_raw, " ")
        b, _, _, _ = expand_inject_payload(rule.payload_raw)
        self.assertEqual(b, b" ")


if __name__ == "__main__":
    unittest.main()
