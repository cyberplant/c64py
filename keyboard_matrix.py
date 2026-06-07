"""C64 keyboard-matrix mapping + key/joystick payload parsing.

Shared by the live emulator (TCP ``INJECT`` command, ``--inject-keys``) and the
offline capture timeline. The matrix coordinates ``(row, col)`` are the standard
C64 hardware table (same values as ``host_keymap.py``); ``(1, 7)`` is LSHIFT.

The payload surface mirrors ``keyboard_inject`` / ``--inject-keys`` so users have
one syntax everywhere:

* literal characters (letters, digits, space, common punctuation),
* ``\\n`` / ``\\r`` → RETURN, ``\\t`` is not a matrix key (skipped),
* ``{token}`` names: ``{space}``, ``{return}``/``{enter}``, ``{f1}``..``{f8}``,
  ``{up}``/``{down}``/``{left}``/``{right}``, ``{home}``, ``{clr}``, ``{del}``,
  ``{inst}``, ``{runstop}``/``{stop}``, ``{ctrl}``, ``{cbm}``, ``{shift}``.

Joystick tokens (``{joy1-fire}`` etc.) are recognized and **skipped** by the
matrix parser — they are handled separately via :func:`parse_joy_mask`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

# LSHIFT cell; ShiftReq entries also press this.
SHIFT_CELL: Tuple[int, int] = (1, 7)

_LETTERS = {
    "a": (1, 2), "b": (3, 4), "c": (2, 4), "d": (2, 2), "e": (1, 6), "f": (2, 5),
    "g": (3, 2), "h": (3, 5), "i": (4, 1), "j": (4, 2), "k": (4, 5), "l": (5, 2),
    "m": (4, 4), "n": (4, 7), "o": (4, 6), "p": (5, 1), "q": (7, 6), "r": (2, 1),
    "s": (1, 5), "t": (2, 6), "u": (3, 6), "v": (3, 7), "w": (1, 1), "x": (2, 7),
    "y": (3, 1), "z": (1, 4),
}
_DIGITS = {
    "1": (7, 0), "2": (7, 3), "3": (1, 0), "4": (1, 3), "5": (2, 0),
    "6": (2, 3), "7": (3, 0), "8": (3, 3), "9": (4, 0), "0": (4, 3),
}
_PUNCT = {
    "+": (5, 0), "-": (5, 3), "*": (6, 1), "/": (6, 7), "@": (5, 6),
    ":": (5, 5), ";": (6, 2), "=": (6, 5), ",": (5, 7), ".": (5, 4),
}

# char -> (row, col, needs_shift)
CHAR_TO_MATRIX: Dict[str, Tuple[int, int, bool]] = {}
for _ch, _rc in _LETTERS.items():
    CHAR_TO_MATRIX[_ch] = (_rc[0], _rc[1], False)
    CHAR_TO_MATRIX[_ch.upper()] = (_rc[0], _rc[1], False)
for _ch, _rc in {**_DIGITS, **_PUNCT}.items():
    CHAR_TO_MATRIX[_ch] = (_rc[0], _rc[1], False)
CHAR_TO_MATRIX[" "] = (7, 4, False)
CHAR_TO_MATRIX["\n"] = (0, 1, False)
CHAR_TO_MATRIX["\r"] = (0, 1, False)

# {token} -> (row, col, needs_shift)
TOKEN_TO_MATRIX: Dict[str, Tuple[int, int, bool]] = {
    "space": (7, 4, False),
    "return": (0, 1, False), "enter": (0, 1, False),
    "f1": (0, 4, False), "f2": (0, 4, True),
    "f3": (0, 5, False), "f4": (0, 5, True),
    "f5": (0, 6, False), "f6": (0, 6, True),
    "f7": (0, 3, False), "f8": (0, 3, True),
    "down": (0, 7, False), "up": (0, 7, True),
    "right": (0, 2, False), "left": (0, 2, True),
    "home": (6, 3, False), "clr": (6, 3, True),
    "del": (0, 0, False), "delete": (0, 0, False), "inst": (0, 0, True),
    "runstop": (7, 7, False), "stop": (7, 7, False), "run": (7, 7, False),
    "ctrl": (7, 2, False),
    "cbm": (7, 5, False), "commodore": (7, 5, False),
    "shift": (1, 7, False),
}

# Joystick direction/button bit masks (active-low; OR-combined).
JOY_BITS: Dict[str, int] = {
    "up": 0x01, "down": 0x02, "left": 0x04, "right": 0x08,
    "fire": 0x10, "button": 0x10,
}

_JOY_TOKEN_RE = re.compile(r"^joy([12])-(up|down|left|right|fire|button)$")


@dataclass
class Stroke:
    """One keystroke: matrix cells to hold together (key + optional shift)."""

    cells: List[Tuple[int, int]]
    label: str


def _stroke(mapped: Tuple[int, int, bool], label: str) -> Stroke:
    row, col, shift = mapped
    cells = [(row, col)]
    if shift:
        cells.append(SHIFT_CELL)
    return Stroke(cells=cells, label=label)


def parse_matrix_strokes(payload: str) -> Tuple[List[Stroke], List[str]]:
    """Expand a payload into serial matrix keystrokes.

    Returns ``(strokes, unknown)`` where ``unknown`` lists characters/tokens with
    no matrix mapping (skipped). ``{joyN-...}`` tokens are recognized and skipped
    (not reported as unknown) since joystick is handled separately.
    """
    strokes: List[Stroke] = []
    unknown: List[str] = []
    i = 0
    n = len(payload)
    while i < n:
        ch = payload[i]
        if ch == "\\" and i + 1 < n:
            nxt = payload[i + 1]
            mapped = CHAR_TO_MATRIX.get("\n") if nxt in ("n", "r") else CHAR_TO_MATRIX.get(nxt)
            if mapped is not None:
                strokes.append(_stroke(mapped, "\\" + nxt))
            elif nxt != "t":  # \t has no matrix cell; ignore silently
                unknown.append("\\" + nxt)
            i += 2
            continue
        if ch == "{":
            j = payload.find("}", i + 1)
            if j < 0:
                raise ValueError(f"unclosed '{{' in payload: {payload!r}")
            token = payload[i + 1 : j].strip().lower().replace(" ", "")
            i = j + 1
            if _JOY_TOKEN_RE.match(token):
                continue  # joystick token; handled by parse_joy_mask
            mapped = TOKEN_TO_MATRIX.get(token)
            if mapped is not None:
                strokes.append(_stroke(mapped, "{" + token + "}"))
            else:
                unknown.append("{" + token + "}")
            continue
        mapped = CHAR_TO_MATRIX.get(ch)
        if mapped is not None:
            strokes.append(_stroke(mapped, ch))
        else:
            unknown.append(ch)
        i += 1
    return strokes, unknown


def parse_joy_mask(spec: str) -> Tuple[int, str]:
    """Parse ``up+fire`` style direction list into ``(mask, normalized_label)``."""
    parts = [p.strip().lower() for p in spec.replace("|", "+").split("+") if p.strip()]
    mask = 0
    names: List[str] = []
    for p in parts:
        bit = JOY_BITS.get(p)
        if bit is None:
            raise ValueError(f"unknown joystick direction/button {p!r}")
        mask |= bit
        names.append(p)
    if mask == 0:
        raise ValueError("no joystick directions/buttons given")
    return mask, "+".join(names)
