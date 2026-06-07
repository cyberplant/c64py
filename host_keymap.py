"""Host (pygame) key → C64 keyboard-matrix mapping.

The matrix layout (row, col) is the standard C64 hardware table; see
``docs/input_config_plan.md`` section 2 for the full grid. ``ShiftReq`` lets
each entry declare whether the host press should also synthesize a SHIFT
press on the matrix (e.g. ``F2`` = SHIFT + F1).

Default punctuation follows a US ANSI host keyboard aligned to a C64 photo
(e.g. host ``'`` → C64 ``;``, ``[`` → ``@``, ``]`` → ``*``, ``-``/``+``,
host ``=`` → C64 ``-``, host ``\\`` → C64 ``=``). C64 RESTORE (NMI via
``$FFFA``) is bound in ``graphics.py`` to Scroll Lock and Pause — not in this
table.

Pygame is imported lazily so importing ``c64py`` headlessly never forces a
pygame init. Call :func:`build_host_to_matrix` once at startup; the returned
dict maps ``pygame.K_*`` constants to ``(row, col, ShiftReq)`` triples.

Joystick mapping (item C/E): :func:`build_host_to_joystick` returns
``dict[pygame.K_*, list[(port, mask)]]`` resolved from a TOML config block.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


class ShiftReq(Enum):
    """Whether to synthesize a SHIFT press alongside the mapped key."""
    NONE = 0
    SHIFT = 1


# C64 SHIFT lives at row 1, col 7 (LSHIFT). Used by ShiftReq.SHIFT entries.
LSHIFT_ROW_COL: Tuple[int, int] = (1, 7)


# Joystick mask bits (active-low at scan time; we OR into joy_held*_clear).
JOY_BIT_UP: int = 0x01
JOY_BIT_DOWN: int = 0x02
JOY_BIT_LEFT: int = 0x04
JOY_BIT_RIGHT: int = 0x08
JOY_BIT_FIRE: int = 0x10

_JOY_DIR_BITS: Dict[str, int] = {
    "up": JOY_BIT_UP,
    "down": JOY_BIT_DOWN,
    "left": JOY_BIT_LEFT,
    "right": JOY_BIT_RIGHT,
    "fire": JOY_BIT_FIRE,
}


# Defaults used when no [input.joystick.*] section is present. Port 2 is the
# C64 default for most games. Port 1 starts empty.
DEFAULT_JOYSTICK_CONFIG: Dict[str, Dict[str, Any]] = {
    "port1": {},
    "port2": {
        "up": "Up",
        "down": "Down",
        "left": "Left",
        "right": "Right",
        # RCtrl is mapped to C= on the matrix; use Space (and optional extras in TOML).
        "fire": ["Space"],
    },
}


def build_host_to_matrix() -> Dict[int, Tuple[int, int, ShiftReq]]:
    """Build the host→matrix table. Requires pygame to be importable."""
    import pygame  # local import: pygame may not be installed in headless tests

    K = pygame
    table: Dict[int, Tuple[int, int, ShiftReq]] = {
        # --- Function keys ---
        K.K_F1: (0, 4, ShiftReq.NONE),
        K.K_F3: (0, 5, ShiftReq.NONE),
        K.K_F5: (0, 6, ShiftReq.NONE),
        K.K_F7: (0, 3, ShiftReq.NONE),
        K.K_F2: (0, 4, ShiftReq.SHIFT),
        K.K_F4: (0, 5, ShiftReq.SHIFT),
        K.K_F6: (0, 6, ShiftReq.SHIFT),
        K.K_F8: (0, 3, ShiftReq.SHIFT),

        # --- Control / editing keys ---
        K.K_RETURN: (0, 1, ShiftReq.NONE),
        K.K_KP_ENTER: (0, 1, ShiftReq.NONE),
        K.K_BACKSPACE: (0, 0, ShiftReq.NONE),       # INST/DEL
        K.K_DELETE: (0, 0, ShiftReq.SHIFT),         # SHIFT+INST/DEL = INST
        K.K_SPACE: (7, 4, ShiftReq.NONE),
        K.K_ESCAPE: (7, 7, ShiftReq.NONE),          # RUN/STOP
        K.K_HOME: (6, 3, ShiftReq.NONE),            # HOME / CLR (SHIFT)
        K.K_TAB: (7, 2, ShiftReq.NONE),             # CTRL (C64 bottom row)
        # Host Ctrl → C= (Commodore key). Joystick default fire uses Space, not RCtrl.
        K.K_LCTRL: (7, 5, ShiftReq.NONE),
        K.K_RCTRL: (7, 5, ShiftReq.NONE),
        K.K_LSHIFT: (1, 7, ShiftReq.NONE),
        K.K_RSHIFT: (6, 4, ShiftReq.NONE),
        # Extra C= for keyboards where Alt is easier than Ctrl.
        K.K_LALT: (7, 5, ShiftReq.NONE),
        K.K_RALT: (7, 5, ShiftReq.NONE),

        # --- Cursor keys ---
        # On the C64, only down + right exist; up = SHIFT+down, left = SHIFT+right.
        K.K_DOWN: (0, 7, ShiftReq.NONE),
        K.K_UP: (0, 7, ShiftReq.SHIFT),
        K.K_RIGHT: (0, 2, ShiftReq.NONE),
        K.K_LEFT: (0, 2, ShiftReq.SHIFT),

        # --- Digits ---
        K.K_1: (7, 0, ShiftReq.NONE),
        K.K_2: (7, 3, ShiftReq.NONE),
        K.K_3: (1, 0, ShiftReq.NONE),
        K.K_4: (1, 3, ShiftReq.NONE),
        K.K_5: (2, 0, ShiftReq.NONE),
        K.K_6: (2, 3, ShiftReq.NONE),
        K.K_7: (3, 0, ShiftReq.NONE),
        K.K_8: (3, 3, ShiftReq.NONE),
        K.K_9: (4, 0, ShiftReq.NONE),
        K.K_0: (4, 3, ShiftReq.NONE),

        # --- Letters (a-z) ---
        K.K_a: (1, 2, ShiftReq.NONE),
        K.K_b: (3, 4, ShiftReq.NONE),
        K.K_c: (2, 4, ShiftReq.NONE),
        K.K_d: (2, 2, ShiftReq.NONE),
        K.K_e: (1, 6, ShiftReq.NONE),
        K.K_f: (2, 5, ShiftReq.NONE),
        K.K_g: (3, 2, ShiftReq.NONE),
        K.K_h: (3, 5, ShiftReq.NONE),
        K.K_i: (4, 1, ShiftReq.NONE),
        K.K_j: (4, 2, ShiftReq.NONE),
        K.K_k: (4, 5, ShiftReq.NONE),
        K.K_l: (5, 2, ShiftReq.NONE),
        K.K_m: (4, 4, ShiftReq.NONE),
        K.K_n: (4, 7, ShiftReq.NONE),
        K.K_o: (4, 6, ShiftReq.NONE),
        K.K_p: (5, 1, ShiftReq.NONE),
        K.K_q: (7, 6, ShiftReq.NONE),
        K.K_r: (2, 1, ShiftReq.NONE),
        K.K_s: (1, 5, ShiftReq.NONE),
        K.K_t: (2, 6, ShiftReq.NONE),
        K.K_u: (3, 6, ShiftReq.NONE),
        K.K_v: (3, 7, ShiftReq.NONE),
        K.K_w: (1, 1, ShiftReq.NONE),
        K.K_x: (2, 7, ShiftReq.NONE),
        K.K_y: (3, 1, ShiftReq.NONE),
        K.K_z: (1, 4, ShiftReq.NONE),

        # --- Punctuation (ANSI host layout aligned to C64 photo: '→; [→@ ]→* …) ---
        K.K_PLUS: (5, 0, ShiftReq.NONE),
        K.K_MINUS: (5, 0, ShiftReq.NONE),          # host - → C64 +
        K.K_KP_PLUS: (5, 0, ShiftReq.NONE),
        K.K_KP_MINUS: (5, 0, ShiftReq.NONE),
        K.K_PERIOD: (5, 4, ShiftReq.NONE),
        K.K_COMMA: (5, 7, ShiftReq.NONE),
        K.K_SLASH: (6, 7, ShiftReq.NONE),
        K.K_ASTERISK: (6, 1, ShiftReq.NONE),
        K.K_KP_MULTIPLY: (6, 1, ShiftReq.NONE),
        K.K_LEFTBRACKET: (5, 6, ShiftReq.NONE),   # host [ → C64 @
        K.K_RIGHTBRACKET: (6, 1, ShiftReq.NONE),  # host ] → C64 *
        K.K_SEMICOLON: (5, 5, ShiftReq.NONE),      # host ; → C64 :
        K.K_QUOTE: (6, 2, ShiftReq.NONE),          # host ' → C64 ;
        K.K_COLON: (5, 5, ShiftReq.NONE),
        K.K_AT: (5, 6, ShiftReq.NONE),
        K.K_EQUALS: (5, 3, ShiftReq.NONE),        # host = → C64 -
        K.K_KP_EQUALS: (5, 3, ShiftReq.NONE),
        K.K_BACKSLASH: (6, 5, ShiftReq.NONE),    # host \ → C64 = (PETSCII BASIC)
        K.K_BACKQUOTE: (7, 1, ShiftReq.NONE),       # ← (left-arrow) on C64
        K.K_CARET: (6, 6, ShiftReq.NONE),           # ↑ (up-arrow) on C64
    }
    return table


# ---------------------------------------------------------------------------
# Joystick name resolution
# ---------------------------------------------------------------------------

# Canonical alias table for friendly names users put in TOML. Keys are
# lower-cased; value is the matching ``pygame.K_*`` attribute name suffix.
_KEY_ALIASES: Dict[str, str] = {
    "up": "UP",
    "down": "DOWN",
    "left": "LEFT",
    "right": "RIGHT",
    "space": "SPACE",
    "spacebar": "SPACE",
    "return": "RETURN",
    "enter": "RETURN",
    "esc": "ESCAPE",
    "escape": "ESCAPE",
    "lshift": "LSHIFT",
    "rshift": "RSHIFT",
    "lctrl": "LCTRL",
    "rctrl": "RCTRL",
    "ctrl": "LCTRL",
    "lalt": "LALT",
    "ralt": "RALT",
    "alt": "LALT",
    "tab": "TAB",
    "home": "HOME",
    "backspace": "BACKSPACE",
    "delete": "DELETE",
    "del": "DELETE",
}


def _resolve_pygame_key(name: str) -> Optional[int]:
    """Resolve a friendly name (``"Up"``, ``"RCtrl"``, ``"a"``, ``"K_F1"``)
    to a ``pygame.K_*`` integer. Returns ``None`` if unknown."""
    import pygame

    raw = name.strip()
    if not raw:
        return None
    # Strip optional K_ prefix.
    if raw.upper().startswith("K_"):
        attr = raw.upper()
        return getattr(pygame, attr, None)
    lower = raw.lower()
    suffix = _KEY_ALIASES.get(lower)
    if suffix is None:
        # Single-character letter/digit fallback (case-sensitive: pygame K_a, K_1).
        if len(raw) == 1:
            return getattr(pygame, f"K_{raw.lower()}", None)
        suffix = raw.upper()
    return getattr(pygame, f"K_{suffix}", None)


def _coerce_keys(value: Any) -> List[str]:
    """Accept either ``"Up"`` or ``["Up", "W"]`` from TOML; return a list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(v) for v in value]
    raise TypeError(
        f"joystick key entry must be a string or list of strings, got {type(value).__name__}"
    )


def build_host_to_joystick(
    config: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[int, List[Tuple[int, int]]]:
    """Resolve a TOML ``[input.joystick]`` block into a host-key → list of
    ``(port, bit_mask)`` tuples.

    ``config`` shape mirrors :data:`DEFAULT_JOYSTICK_CONFIG`::

        {"port1": {"up": "W", ...}, "port2": {"fire": ["Space"]}}

    Multiple ports/directions can target the same host key (returned as a
    list so the runtime ORs all matching bits on press). Unknown key names
    are silently dropped — log warnings live at the caller (graphics.py).
    """
    if config is None:
        config = DEFAULT_JOYSTICK_CONFIG
    out: Dict[int, List[Tuple[int, int]]] = {}
    for port_key, port_num in (("port1", 1), ("port2", 2)):
        port_block = config.get(port_key) if isinstance(config, Mapping) else None
        if not port_block:
            continue
        for dir_name, dir_bit in _JOY_DIR_BITS.items():
            keys = _coerce_keys(port_block.get(dir_name))
            for k in keys:
                code = _resolve_pygame_key(k)
                if code is None:
                    continue
                out.setdefault(code, []).append((port_num, dir_bit))
    return out
