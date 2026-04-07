"""
Parse ``--inject-keys`` and expand payloads into PETSCII bytes + joystick masks.

Use **one** ``--inject-keys WHEN:WHAT`` per scheduled inject (repeat the flag as needed).
Only the **first** ``Nc:`` / ``Ns:`` splits timing from payload; everything after that colon
is ``WHAT`` verbatim (leading/trailing spaces, embedded ``1c:`` text, extra colons, etc.).

    <when>  ::= <int>c   # emulated CPU cycle count (>= threshold fires once)
             |  <float>s # host wall seconds since ``run()`` started

``<what>`` length is capped (see ``_RAW_MAX_LEN``). Supported in ``WHAT``:

* Printable ASCII and escapes: ``\\``, ``\\n`` / ``\\r`` (RETURN 0x0D), ``\\t``.
* Lowercase letters map to PETSCII uppercase (same as pygame UI).
* ``{F1}`` … ``{F8}`` — PETSCII function-key codes (0x85–0x8C) for BASIC/demos.
* ``{joy1-up}``, ``{joy1-down}``, ``{joy1-left}``, ``{joy1-right}``,
  ``{joy1-fire}`` / ``{joy1-button}`` — same for ``joy2`` (best-effort CIA1 bit
  pull-downs on ``$DC00`` / ``$DC01`` reads; see MemoryMap).

Joystick directions accumulate OR-style within one payload; hold duration defaults
to ``C64PY_INJECT_JOY_CYCLES`` (default 200_000 CPU cycles) or the same env name.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# When reading CIA1: idle 0xFF; joystick active lines are low (bits cleared).
# Port 1 → $DC01 (PRB), port 2 → $DC00 (PRA). Bit layout matches common C64 docs.
_JOY_DIR_BITS = {"up": 0x01, "down": 0x02, "left": 0x04, "right": 0x08, "fire": 0x10, "button": 0x10}

_FN_KEY_PETSCII = {
    "f1": 0x85,
    "f2": 0x89,
    "f3": 0x86,
    "f4": 0x8A,
    "f5": 0x87,
    "f6": 0x8B,
    "f7": 0x88,
    "f8": 0x8C,
}

_ENTRY_RE = re.compile(r"^(\d+(?:\.\d+)?)([cs]):(.*)\Z", re.DOTALL)
_RAW_MAX_LEN = 96


def _joy_hold_cycles() -> int:
    raw = os.environ.get("C64PY_INJECT_JOY_CYCLES", "").strip()
    if not raw:
        return 200_000
    try:
        return max(1, int(raw, 10))
    except ValueError:
        return 200_000


def ascii_char_to_petscii_byte(ch: str) -> int:
    """Single-character ASCII → PETSCII (matches graphics UI)."""
    if not ch:
        return 0
    o = ord(ch)
    if 0x20 <= o <= 0x5F:
        return o
    if 0x61 <= o <= 0x7A:
        return o - 0x20
    return o & 0xFF


def expand_inject_payload(raw: str) -> Tuple[bytes, int, int, int]:
    """Return (keyboard_bytes, joy1_clear_mask, joy2_clear_mask, joy_hold_cycles)."""
    if raw == "":
        return b"", 0, 0, 0
    if len(raw) > _RAW_MAX_LEN:
        raise ValueError(f"payload too long (max {_RAW_MAX_LEN} chars): {raw[:20]!r}…")
    s = raw

    out = bytearray()
    joy1 = 0
    joy2 = 0
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "n":
                out.append(0x0D)
            elif nxt == "r":
                out.append(0x0D)
            elif nxt == "t":
                out.append(0x09)
            elif nxt == "\\":
                out.append(0x5C)
            else:
                out.append(ascii_char_to_petscii_byte(nxt))
            i += 2
            continue
        if c == "{":
            j = s.find("}", i + 1)
            if j < 0:
                raise ValueError(f"unclosed '{{' in payload: {s!r}")
            token = s[i + 1 : j].strip().lower().replace(" ", "")
            i = j + 1
            if token.startswith("joy"):
                m = re.match(r"^joy([12])-(up|down|left|right|fire|button)$", token)
                if not m:
                    raise ValueError(f"unknown joystick token {{{token}}}")
                port = int(m.group(1))
                bit = _JOY_DIR_BITS[m.group(2)]
                if port == 1:
                    joy1 |= bit
                else:
                    joy2 |= bit
                continue
            if token in _FN_KEY_PETSCII:
                out.append(_FN_KEY_PETSCII[token])
                continue
            raise ValueError(f"unknown braced token {{{token}}}")
        out.append(ascii_char_to_petscii_byte(c))
        i += 1

    hold = _joy_hold_cycles() if (joy1 or joy2) else 0
    return bytes(out), joy1, joy2, hold


@dataclass
class InjectKeyRule:
    """One scheduled inject: either cycle- or wall-time triggered."""

    when_cycles: Optional[int]
    when_seconds: Optional[float]
    payload_raw: str
    fired: bool = False

    def __post_init__(self) -> None:
        has_c = self.when_cycles is not None
        has_s = self.when_seconds is not None
        if has_c == has_s:
            raise ValueError("rule must set exactly one of when_cycles / when_seconds")


def parse_inject_key_entry(entry: str, *, index: Optional[int] = None) -> InjectKeyRule:
    """Parse one ``WHEN:WHAT`` string (entire payload is ``group`` after first colon).

    Only **leading** whitespace on the argv token is removed so a payload of a single
    trailing space (e.g. ``4000000c: ``) is preserved; ``str.strip()`` would erase it.
    """
    text = entry.lstrip()
    if not text:
        raise ValueError("empty --inject-keys entry")
    m = _ENTRY_RE.match(text)
    if not m:
        loc = f" (entry #{index})" if index is not None else ""
        raise ValueError(
            f"bad --inject-keys entry{loc}: expected <int>c:<what> or <float>s:<what>, "
            f"got {entry[:60]!r}"
        )
    num_s, unit, what = m.group(1), m.group(2), m.group(3)
    if unit == "c":
        try:
            cval = int(num_s, 10)
        except ValueError as exc:
            raise ValueError(f"bad cycle count {num_s!r}") from exc
        if cval < 0:
            raise ValueError("cycle count must be non-negative")
        rule = InjectKeyRule(when_cycles=cval, when_seconds=None, payload_raw=what)
    else:
        try:
            sval = float(num_s)
        except ValueError as exc:
            raise ValueError(f"bad seconds value {num_s!r}") from exc
        if sval < 0:
            raise ValueError("seconds must be non-negative")
        rule = InjectKeyRule(when_cycles=None, when_seconds=sval, payload_raw=what)
    expand_inject_payload(what)
    return rule


def parse_inject_key_entries(entries: List[str]) -> List[InjectKeyRule]:
    """Parse each CLI ``--inject-keys`` occurrence into a rule (order preserved)."""
    rules: List[InjectKeyRule] = []
    for i, raw in enumerate(entries):
        if not (raw or "").strip():
            continue
        rules.append(parse_inject_key_entry(raw, index=i + 1))
    return rules
