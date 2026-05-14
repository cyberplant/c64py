"""Persistent configuration for c64py (TOML-backed).

Implements item D from ``docs/input_config_plan.md``:

* Single config file model. The first existing path in
  :data:`CONFIG_SEARCH_PATHS` wins; CLI flags override config; config
  overrides hardcoded defaults.
* Search order (resolved at call time, not import time):

  1. ``./c64py.toml`` (cwd)
  2. ``~/.c64py.toml``
  3. ``$XDG_CONFIG_HOME/c64py/c64py.toml``
     (default ``~/.config/c64py/c64py.toml`` if ``XDG_CONFIG_HOME`` unset)

The ``[input.joystick.*]`` schema is intentionally left empty — it is
filled by item E once the keymap data structures land.
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import-not-found]
else:  # pragma: no cover - exercised on 3.9/3.10 only
    import tomli as tomllib  # type: ignore[import-not-found]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# DEFAULT_CONFIG mirrors the hardcoded CLI defaults. Keep this in sync with
# the argparse setup in ``C64.py``. Keys here are the canonical config
# names; the mapping to CLI flags lives in ``C64.py`` (CONFIG_TO_CLI).
DEFAULT_CONFIG: Dict[str, Any] = {
    "video": {
        # "per-frame" | "per-raster" | "per-cycle" (per-cycle uses the Python VIC cycle path;
        # see docs/per_cycle_vic.md). Aliases "fast"/"accurate" are accepted on the CLI for
        # backwards compat but normalised before reaching this config.
        "rendering": "per-frame",
        "standard": "pal",
        "scale": 2,
        "fps": 30,
        "border": 32,
        "fullscreen": False,
    },
    "audio": {
        "emulation": "resid",
        "volume": 1.0,
    },
    "emulation": {
        "interface": "textual",
        "disk_emulation": "fast",
        "vic_emulation": "fast",
    },
    "c1541": {
        "file_logging_enabled": False,
        "file_logging_filename": "logs/c1541-{date}.log",
    },
    "debug": {
        "turbo": False,
        "udp_debug": False,
        "screen_update_interval": 0.1,
        # UDP port for debug events when udp_debug is true. Matches the
        # historical CLI default.
        "udp_port": 64738,
    },
    # Joystick mapping (item E): host keys → joystick port bits. Defaults
    # match docs/input_config_plan.md §3 — port 2 with arrows + RCtrl/Space.
    "input": {
        "joystick": {
            "port1": {},
            "port2": {
                "up": "Up",
                "down": "Down",
                "left": "Left",
                "right": "Right",
                "fire": ["RCtrl", "Space"],
            },
        },
        "gamepad": {
            # Shared default axis threshold; each port can override with the same key.
            "axis_threshold": 0.5,
            # Per C64 joystick port: separate SDL device index + mapping.
            "port1": {
                "enabled": False,
                "axis_threshold": 0.5,
                "mapping": {
                    "up": "axis1-",
                    "down": "axis1+",
                    "left": "axis0-",
                    "right": "axis0+",
                    "fire": "button0",
                },
            },
            "port2": {
                "enabled": False,
                "axis_threshold": 0.5,
                "mapping": {
                    "up": "axis1-",
                    "down": "axis1+",
                    "left": "axis0-",
                    "right": "axis0+",
                    "fire": "button0",
                },
            },
        },
    },
}


def _config_search_paths() -> List[Path]:
    """Resolve the search list at call time (env / cwd may have changed)."""
    paths: List[Path] = [Path.cwd() / "c64py.toml"]

    home = os.environ.get("HOME")
    if home:
        paths.append(Path(home) / ".c64py.toml")

    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        paths.append(Path(xdg) / "c64py" / "c64py.toml")
    elif home:
        paths.append(Path(home) / ".config" / "c64py" / "c64py.toml")

    return paths


# Backwards-compatible alias. ``CONFIG_SEARCH_PATHS`` is documented as
# "resolved at call time"; treat it as a callable-ish proxy by exposing
# the function and a property-like attribute via ``__getattr__`` so users
# importing the module-level name still get a fresh list.
def __getattr__(name: str) -> Any:  # pragma: no cover - trivial
    if name == "CONFIG_SEARCH_PATHS":
        return _config_search_paths()
    raise AttributeError(name)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Return a new dict where ``overlay`` keys win, recursing into nested dicts."""
    out: Dict[str, Any] = copy.deepcopy(base)
    for key, val in overlay.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(val, dict)
        ):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def normalize_sdl_guid(raw: Any) -> Optional[str]:
    """Normalize an SDL joystick GUID string for comparison (lowercase hex, no dashes)."""

    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        s = raw.hex()
    else:
        s = str(raw).strip().lower().replace("-", "")
    return s or None


def gamepad_joy_guid(joy: Any) -> Optional[str]:
    """Return normalized SDL GUID for a pygame Joystick, or None if unavailable."""

    if joy is None:
        return None
    get_g = getattr(joy, "get_guid", None)
    if get_g is None:
        return None
    try:
        raw = get_g()
    except Exception:
        return None
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        return normalize_sdl_guid(raw.hex())
    return normalize_sdl_guid(str(raw))


def default_sdl_joystick_index_for_c64_port(c64_port: int) -> int:
    """SDL index for legacy string bindings: C64 port 1 → 0, port 2 → 1."""

    return max(0, int(c64_port) - 1)


def parse_gamepad_mapping_entry(val: Any) -> Tuple[Optional[str], Optional[int], str]:
    """Parse a mapping value into ``(guid, host_index, token)``.

    * **String** (legacy): ``(None, None, token)`` — use SDL index ``port - 1`` for that C64 port.
    * **Table**: ``guid`` / ``device_guid`` / ``sdl_guid``, ``token`` / ``bind``, optional
      ``host_index`` / ``sdl_index`` when two devices share the same GUID (same model).
    """

    if val is None or val == "":
        return (None, None, "")
    if isinstance(val, str):
        return (None, None, val.strip().lower())
    if isinstance(val, dict):
        tok = val.get("token") if "token" in val else val.get("bind")
        if tok is None:
            return (None, None, "")
        tok_s = str(tok).strip().lower()
        raw_g = val.get("guid")
        if raw_g is None:
            raw_g = val.get("device_guid")
        if raw_g is None:
            raw_g = val.get("sdl_guid")
        guid = normalize_sdl_guid(raw_g) if raw_g not in (None, "") else None
        hi = val.get("host_index")
        if hi is None:
            hi = val.get("sdl_index")
        host_i: Optional[int]
        if hi is None or hi == "":
            host_i = None
        else:
            try:
                host_i = int(hi)
            except (TypeError, ValueError):
                host_i = None
        return (guid, host_i, tok_s)
    return (None, None, str(val).strip().lower())


def gamepad_binding_value_for_capture(joy: Optional[Any], sticks: List[Any], token: str) -> Union[str, Dict[str, Any]]:
    """Build a TOML value for a captured control: string if no GUID, else a small table."""

    tok = token.strip().lower()
    g = gamepad_joy_guid(joy) if joy is not None else None
    if not g:
        return tok
    out: Dict[str, Any] = {"guid": g, "token": tok}
    guids = [gamepad_joy_guid(j) for j in sticks]
    if guids.count(g) > 1:
        for i, j in enumerate(sticks):
            if j is joy:
                out["host_index"] = i
                break
    return out


def _joy_from_instance_id(sticks: List[Any], instance_id: Optional[int]) -> Optional[Any]:
    if instance_id is None:
        return None
    for j in sticks:
        get_i = getattr(j, "get_instance_id", None)
        if get_i is None:
            continue
        try:
            if int(get_i()) == int(instance_id):
                return j
        except (TypeError, ValueError, AttributeError):
            continue
    return None


def _migrate_legacy_gamepad(cfg: Dict[str, Any]) -> None:
    """Normalize ``input.gamepad`` to per-port ``port1`` / ``port2`` blocks.

    Older configs used top-level ``enabled`` + ``port`` + ``mapping`` (often
    merged alongside default ``port1`` / ``port2`` keys). Detect that and
    fold values into the target port, then strip legacy keys.
    """
    inp = cfg.get("input")
    if not isinstance(inp, dict):
        return
    gp = inp.get("gamepad")
    if not isinstance(gp, dict):
        return
    defaults_gp = DEFAULT_CONFIG["input"]["gamepad"]

    # v1 flat schema: top-level ``enabled`` is a bool (not a port table).
    if isinstance(gp.get("enabled"), bool):
        port = max(1, min(2, int(gp.get("port", 2))))
        key = f"port{port}"
        for k in ("port1", "port2"):
            base = copy.deepcopy(defaults_gp.get(k, {}))
            cur = gp.get(k)
            if isinstance(cur, dict):
                gp[k] = _deep_merge(base, cur)
            else:
                gp[k] = base
        gp[key]["enabled"] = bool(gp["enabled"])
        if isinstance(gp.get("mapping"), dict):
            gp[key]["mapping"] = _deep_merge(
                copy.deepcopy(gp[key]["mapping"]), gp["mapping"]
            )
        if isinstance(gp.get("axis_threshold"), (int, float)):
            gp[key]["axis_threshold"] = float(gp["axis_threshold"])
        for legacy in ("enabled", "device_index", "port", "mapping"):
            gp.pop(legacy, None)
        if "axis_threshold" not in gp:
            gp["axis_threshold"] = defaults_gp["axis_threshold"]
        for pk in ("port1", "port2"):
            blk = gp.get(pk)
            if isinstance(blk, dict):
                blk.pop("device_index", None)
        return

    for k in ("port1", "port2"):
        base = copy.deepcopy(defaults_gp.get(k, {}))
        cur = gp.get(k)
        if isinstance(cur, dict):
            gp[k] = _deep_merge(base, cur)
        else:
            gp[k] = base
    if "axis_threshold" not in gp:
        gp["axis_threshold"] = defaults_gp["axis_threshold"]
    for legacy in ("enabled", "device_index", "port", "mapping"):
        gp.pop(legacy, None)
    for pk in ("port1", "port2"):
        blk = gp.get(pk)
        if isinstance(blk, dict):
            blk.pop("device_index", None)


def _parse_toml_file(path: Path) -> Dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_config(
    path: Optional[Path] = None,
    *,
    skip_search: bool = False,
) -> Dict[str, Any]:
    """Load and return the effective config (defaults + first found file).

    * ``path`` — if given, parse only that file. ``FileNotFoundError`` is
      raised if it does not exist.
    * ``skip_search`` — if True (and ``path`` is None), return defaults
      only without consulting any file.
    * Otherwise walk :func:`_config_search_paths`; first existing file
      wins, deep-merged over :data:`DEFAULT_CONFIG`.
    """
    if path is not None:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"Config file not found: {path}")
        merged = _deep_merge(DEFAULT_CONFIG, _parse_toml_file(path))
        _migrate_legacy_gamepad(merged)
        return merged

    if skip_search:
        out = copy.deepcopy(DEFAULT_CONFIG)
        _migrate_legacy_gamepad(out)
        return out

    for candidate in _config_search_paths():
        if candidate.is_file():
            merged = _deep_merge(DEFAULT_CONFIG, _parse_toml_file(candidate))
            _migrate_legacy_gamepad(merged)
            return merged

    out = copy.deepcopy(DEFAULT_CONFIG)
    _migrate_legacy_gamepad(out)
    return out


def find_config_file() -> Optional[Path]:
    """Return the first existing config file in the search order, or None."""
    for candidate in _config_search_paths():
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

# Inline comments emitted alongside select keys when calling
# :func:`write_config`. Maps "section.key" -> comment text.
_COMMENTS: Dict[str, str] = {
    "video.standard": 'Video timing standard: "pal" or "ntsc"',
    "video.rendering": 'Pygame sampling tier: "per-frame", "per-raster", or "per-cycle"',
    "video.scale": "Graphics window scale factor (integer)",
    "video.fps": "Graphics target FPS / max present rate",
    "video.border": "Graphics border size in pixels",
    "video.fullscreen": "Hide debug panel / status bar (text mode) when true",
    "audio.emulation": 'Audio backend: "resid", "python-sid", or "disabled"',
    "audio.volume": "Master volume (0.0 = muted, 1.0 = full)",
    "emulation.interface": 'Interface mode: "textual", "headless", or "graphics"',
    "emulation.disk_emulation": 'Disk emulation tier: "fast", "accurate-python", "accurate-rust"',
    "emulation.vic_emulation": 'VIC timing tier: "fast", "accurate-python", "accurate-rust"',
    "c1541.file_logging_enabled": "Append c1541_emulator logs to a file (standalone TCP drive)",
    "c1541.file_logging_filename": 'Log path; "{date}" → ISO date, "{device}" → drive number',
    "debug.turbo": "Run at maximum speed (no throttle)",
    "debug.udp_debug": "Emit debug events over UDP",
    "debug.udp_port": "UDP port for debug events when udp_debug = true",
    "debug.screen_update_interval": "Status/screen update interval in seconds",
    "input.gamepad.axis_threshold": "Default axis threshold (ports may override)",
    "input.gamepad.port1.enabled": "Gamepad → emulated C64 joystick port 1",
    "input.gamepad.port1.axis_threshold": "Axis threshold for port 1",
    "input.gamepad.port2.enabled": "Gamepad → emulated C64 joystick port 2",
    "input.gamepad.port2.axis_threshold": "Axis threshold for port 2",
}


def _toml_quote(s: str) -> str:
    # Basic strings; escape backslash and double-quote, control chars unlikely.
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return _toml_quote(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"Unsupported TOML value type: {type(v).__name__}")


def _emit_section(
    lines: List[str],
    section_path: List[str],
    table: Dict[str, Any],
) -> None:
    header = ".".join(section_path)
    if section_path:
        lines.append(f"[{header}]")

    # Scalar/leaf keys first, then nested tables.
    scalar_items = [(k, v) for k, v in table.items() if not isinstance(v, dict)]
    nested_items = [(k, v) for k, v in table.items() if isinstance(v, dict)]

    for key, val in scalar_items:
        comment = _COMMENTS.get(f"{header}.{key}" if header else key)
        rendered = f"{key} = {_toml_value(val)}"
        if comment:
            rendered += f"  # {comment}"
        lines.append(rendered)

    if scalar_items and nested_items:
        lines.append("")

    for i, (key, val) in enumerate(nested_items):
        if i > 0 or scalar_items:
            lines.append("")
        _emit_section(lines, section_path + [key], val)


def dumps_config(config: Dict[str, Any]) -> str:
    """Serialise a config dict to a deterministic TOML string."""
    lines: List[str] = [
        "# c64py configuration file.",
        "# Generated by `c64py --write-config`. CLI flags always override these values.",
        "# See docs/config.md for the full schema and search order.",
        "",
    ]
    # Top-level table comes first (no header), then each section.
    top_scalars = {k: v for k, v in config.items() if not isinstance(v, dict)}
    if top_scalars:
        _emit_section(lines, [], top_scalars)
        lines.append("")

    sections = [(k, v) for k, v in config.items() if isinstance(v, dict)]
    for i, (key, val) in enumerate(sections):
        if i > 0:
            lines.append("")
        _emit_section(lines, [key], val)

    # Trailing newline.
    return "\n".join(lines) + "\n"


def write_config(path: Path, *, force: bool = False) -> None:
    """Write a fully-populated TOML config to *path*.

    Refuses to overwrite an existing file unless ``force=True``.
    Parent directories are created as needed.
    """
    path = Path(path).expanduser()
    if path.exists() and not force:
        raise FileExistsError(
            f"Refusing to overwrite existing config: {path} "
            "(pass force=True / --force-overwrite-config)"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps_config(DEFAULT_CONFIG), encoding="utf-8")


def _config_get(config: Dict[str, Any], dotted: str) -> Any:
    cur: Any = config
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _config_set(config: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur: Dict[str, Any] = config
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _config_delete_leaf(config: Dict[str, Any], dotted: str) -> None:
    """Remove a leaf key if present (used to clear bindings to match empty defaults)."""

    parts = dotted.split(".")
    cur: Dict[str, Any] = config
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            return
        cur = nxt
    cur.pop(parts[-1], None)


def _config_restore_default_leaf(cfg: Dict[str, Any], dotted: str) -> None:
    """Set *dotted* to the value from :data:`DEFAULT_CONFIG`, or delete if absent there."""

    dval = _config_get(DEFAULT_CONFIG, dotted)
    if dval is None:
        _config_delete_leaf(cfg, dotted)
    else:
        _config_set(cfg, dotted, copy.deepcopy(dval))


# C64-ish editor palette (VIC luminances, blue backdrop).
_EDITOR_BG = (0x30, 0x30, 0xC0)
_EDITOR_HDR = (0x6C, 0x6C, 0x6C)
_EDITOR_FG = (0xFC, 0xFC, 0xFC)
_EDITOR_SEL = (0xFC, 0xFC, 0x54)
_EDITOR_ACCENT = (0xA8, 0xA8, 0xA8)


def _try_read_chargen_4k() -> Optional[bytes]:
    try:
        from c64py.roms import iter_candidate_rom_dirs
    except ImportError:
        return None
    names = ("characters.901225-01.bin", "chargen-901225-01.bin")
    for d in iter_candidate_rom_dirs():
        for n in names:
            p = d / n
            try:
                if p.is_file() and p.stat().st_size >= 4096:
                    return p.read_bytes()[:4096]
            except OSError:
                pass
    return None


def _petscii_to_screen_code_chrout(petscii: int) -> int:
    """PETSCII byte → C64 screen code (KERNAL CHROUT / BSOUT $E716 rules).

    The 901225 chargen is indexed by *screen code*, not PETSCII/ASCII. Using raw
    ASCII (especially ``a``–``z``) as an index picks the wrong glyphs (graphics).
    """

    c = petscii & 0xFF
    if 0x40 <= c <= 0x5F:
        return c - 0x40
    if 0x60 <= c <= 0x7F:
        return c - 0x20
    if 0xA0 <= c <= 0xBF:
        return c - 0x40
    if 0xC0 <= c <= 0xFE:
        return c - 0x40
    if c == 0xFF:
        return 0x5E
    return c


# PETSCII substitutes for Unicode punctuation used in editor strings.
_EDITOR_CHAR_UNI_FALLBACK = {
    0x2014: 0x2D,  # —
    0x2013: 0x2D,  # –
    0x2018: 0x27,
    0x2019: 0x27,
    0x201C: 0x22,
    0x201D: 0x22,
}

# Longest first: strip this prefix from dotted keys in the config editor list.
_EDITOR_DOTTED_DISPLAY_PREFIXES: tuple[str, ...] = (
    "emulation.",
    "input.gamepad.port1.mapping.",
    "input.gamepad.port2.mapping.",
    "input.gamepad.port1.",
    "input.gamepad.port2.",
    "input.joystick.port1.",
    "input.joystick.port2.",
    "input.gamepad.",
    "video.",
    "audio.",
    "debug.",
)


def _editor_short_dotted_key(dotted: str) -> str:
    """Last path segment(s) after the section prefix, for compact list labels."""

    for pref in _EDITOR_DOTTED_DISPLAY_PREFIXES:
        if dotted.startswith(pref):
            return dotted[len(pref) :]
    parts = dotted.split(".")
    return parts[-1] if parts else dotted


def _editor_label_from_key(short_key: str) -> str:
    """Make key names chargen-safe (underscores → hyphens)."""

    return short_key.replace("_", "-")


def _editor_format_value_display(val: Any) -> str:
    """Format a value for on-screen display.

    Gamepad binding dicts use ``[ key=value, ... ]`` with commas (``[]`` — chargen
    draws ``{}`` as wrong glyphs). SDL GUIDs are shown in full (typically 32 hex chars).
    """

    if isinstance(val, dict):
        order = ("guid", "token", "host_index")
        seen: List[str] = []
        parts: List[str] = []
        for k in order:
            if k not in val:
                continue
            v = val[k]
            parts.append(f"{_editor_label_from_key(k)}={v}")
            seen.append(k)
        for k, v in val.items():
            if k in seen:
                continue
            parts.append(f"{_editor_label_from_key(str(k))}={v}")
        if not parts:
            return "[]"
        inner = ", ".join(parts)
        return f"[ {inner} ]"
    if isinstance(val, list):
        if not val:
            return "None"
        return ", ".join(
            repr(x) if isinstance(x, str) else str(x) for x in val
        )
    if val is None:
        return "None"
    if isinstance(val, bool):
        return "True" if val else "False"
    if isinstance(val, (int, float)):
        return str(val)
    return repr(val)


def _editor_char_to_screen_code(ch: str) -> int:
    """Map one character → screen code for the **lo/up** chargen half (offset 2048).

    That ROM half shows **uppercase** at screen codes ``0x41``–``0x5A`` and **lowercase**
    at ``1``–``26`` (see sta.c64.org screen-code table, column *lo/up*).

    KERNAL CHROUT instead targets the *up/gfx* half: PETSCII ``A``→1, ``a``→0x41.
    Feeding CHROUT codes into the lo/up half **swaps** Latin case — fine for a real C64
    that pairs CHROUT with the matching VIC charset bit, wrong for drawing ASCII strings
    with a fixed lo/up bitmap font.
    """

    o = ord(ch)
    if o > 0xFF:
        o = _EDITOR_CHAR_UNI_FALLBACK.get(o, ord("?"))
    # Underscore has no lo/up glyph; hyphen reads clearly in on-screen labels.
    if o == ord("_"):
        return ord("-")
    if ord("A") <= o <= ord("Z"):
        return o
    if ord("a") <= o <= ord("z"):
        return o - 0x60
    return _petscii_to_screen_code_chrout(o)


class _EditorFont:
    """8×8 C64 chargen (scaled) when a full 4 KiB ROM is found; else system font.

    The 901225 chargen file is two 256-glyph banks: bytes ``0..2047`` are the
    **uppercase + graphics** set (what you see when lowercase is “symbols” on a
    C64); bytes ``2048..4095`` are **uppercase + lowercase** text. We always
    draw from the second bank so normal ASCII labels render as letters. Latin
    letters use lo/up screen codes (uppercase at ``0x41``+, lowercase at ``1``–``26``);
    other bytes use PETSCII → screen code (CHROUT).
    """

    _CHAR_ROM_LOWER_CASESET_OFFSET = 2048

    def __init__(self, pygame_mod: Any, chargen: Optional[bytes]) -> None:
        self._pg = pygame_mod
        self._chargen = chargen if chargen and len(chargen) >= 4096 else None
        self.px = 2
        self._sys = pygame_mod.font.SysFont(
            "menlo,monaco,courier new,courier,consolas,lucidatypewriter",
            14,
            bold=True,
        )

    @property
    def line_skip(self) -> int:
        return 8 * self.px + 4

    def render(self, text: str, color: tuple[int, int, int], bg: tuple[int, int, int]) -> Any:
        if self._chargen:
            return self._render_chargen(text, color, bg)
        return self._sys.render(text, True, color, bg)

    def measure_text_width(self, text: str) -> int:
        """Horizontal pixel width of *text* as rendered by this font."""

        if self._chargen:
            return max(1, len(text)) * 8 * self.px
        return int(self._sys.size(text)[0])

    def _glyph(self, screen_code: int, fg: tuple[int, int, int], bg: tuple[int, int, int]) -> Any:
        surf = self._pg.Surface((8 * self.px, 8 * self.px))
        code = screen_code & 0xFF
        rom = self._chargen
        if not rom:
            return surf
        base = self._CHAR_ROM_LOWER_CASESET_OFFSET + code * 8
        if base + 8 > len(rom):
            base = code * 8
        for row in range(8):
            b = rom[base + row]
            for col in range(8):
                bit = (b >> (7 - col)) & 1
                c = fg if bit else bg
                self._pg.draw.rect(surf, c, (col * self.px, row * self.px, self.px, self.px))
        return surf

    def _render_chargen(self, text: str, fg: tuple[int, int, int], bg: tuple[int, int, int]) -> Any:
        w, h = max(1, len(text)) * 8 * self.px, 8 * self.px
        surf = self._pg.Surface((w, h))
        surf.fill(bg)
        for i, ch in enumerate(text):
            surf.blit(self._glyph(_editor_char_to_screen_code(ch), fg, bg), (i * 8 * self.px, 0))
        return surf


def _joy_fire_display(cfg: Dict[str, Any], port: int) -> str:
    v = _config_get(cfg, f"input.joystick.port{port}.fire")
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v) if v is not None else ""


def _joy_fire_set(cfg: Dict[str, Any], port: int, name: str) -> None:
    _config_set(cfg, f"input.joystick.port{port}.fire", name)


def _editor_clear_input_binding(cfg: Dict[str, Any], row: Any) -> None:
    """Clear keyboard direction, gamepad token, or fire list (unbound; persists via TOML)."""

    kind = row[0]
    if kind == "fire":
        _config_set(cfg, f"input.joystick.port{int(row[1])}.fire", [])
        return
    if kind == "fld" and row[2] in ("key", "pad"):
        _config_set(cfg, row[1], "")


def _friendly_keyboard_name(pygame: Any, key: int) -> str:
    n = pygame.key.name(int(key)).lower()
    aliases = {
        "up": "Up",
        "down": "Down",
        "left": "Left",
        "right": "Right",
        "space": "Space",
        "return": "Return",
        "escape": "Escape",
        "tab": "Tab",
        "left shift": "LShift",
        "right shift": "RShift",
        "left ctrl": "LCTRL",
        "right ctrl": "RCtrl",
        "left alt": "LALT",
        "right alt": "RALT",
        "left meta": "LMeta",
        "right meta": "RMeta",
    }
    if n in aliases:
        return aliases[n]
    if len(n) == 1:
        return n.upper()
    return n.replace(" ", "").title()


def _editor_build_rows() -> List[Any]:
    rows: List[Any] = []
    rows.append(("hdr", "--- VIDEO ---"))
    rows += [
        ("fld", "video.standard", "choice", ("pal", "ntsc")),
        ("fld", "video.rendering", "choice", ("per-frame", "per-raster", "per-cycle")),
        ("fld", "video.scale", "int", None),
        ("fld", "video.fps", "int", None),
        ("fld", "video.border", "int", None),
        ("fld", "video.fullscreen", "bool", None),
    ]
    rows.append(("hdr", "--- AUDIO ---"))
    rows += [
        ("fld", "audio.emulation", "choice", ("resid", "python-sid", "disabled")),
        ("fld", "audio.volume", "float", None),
    ]
    rows.append(("hdr", "--- EMULATION ---"))
    rows += [
        ("fld", "emulation.interface", "choice", ("textual", "headless", "graphics")),
        ("fld", "emulation.disk_emulation", "choice", ("fast", "accurate-python", "accurate-rust")),
        ("fld", "emulation.vic_emulation", "choice", ("fast", "accurate-python", "accurate-rust")),
    ]
    rows.append(("hdr", "--- DEBUG ---"))
    rows += [
        ("fld", "debug.turbo", "bool", None),
        ("fld", "debug.udp_debug", "bool", None),
        ("fld", "debug.udp_port", "int", None),
        ("fld", "debug.screen_update_interval", "float", None),
    ]
    rows.append(("hdr", "--- JOYSTICK PORT 1 — KEYBOARD ---"))
    for d in ("up", "down", "left", "right"):
        rows.append(("fld", f"input.joystick.port1.{d}", "key", None))
    rows.append(("fire", 1))
    rows.append(("hdr", "--- JOYSTICK PORT 1 — GAMEPAD ---"))
    rows += [
        ("fld", "input.gamepad.port1.enabled", "bool", None),
        ("fld", "input.gamepad.port1.axis_threshold", "float", None),
    ]
    for d in ("up", "down", "left", "right", "fire"):
        rows.append(("fld", f"input.gamepad.port1.mapping.{d}", "pad", 1))
    rows.append(("hdr", "--- JOYSTICK PORT 2 — KEYBOARD ---"))
    for d in ("up", "down", "left", "right"):
        rows.append(("fld", f"input.joystick.port2.{d}", "key", None))
    rows.append(("fire", 2))
    rows.append(("hdr", "--- JOYSTICK PORT 2 — GAMEPAD ---"))
    rows += [
        ("fld", "input.gamepad.port2.enabled", "bool", None),
        ("fld", "input.gamepad.port2.axis_threshold", "float", None),
    ]
    for d in ("up", "down", "left", "right", "fire"):
        rows.append(("fld", f"input.gamepad.port2.mapping.{d}", "pad", 2))
    rows.append(("hdr", "--- GLOBAL GAMEPAD ---"))
    rows.append(("fld", "input.gamepad.axis_threshold", "float", None))
    return rows


def _editor_row_text(cfg: Dict[str, Any], row: Any) -> str:
    kind = row[0]
    if kind == "hdr":
        return row[1]
    if kind == "fire":
        p = row[1]
        dotted_f = f"input.joystick.port{p}.fire"
        v = _config_get(cfg, dotted_f)
        label = _editor_label_from_key(_editor_short_dotted_key(dotted_f))
        if v is None or v == [] or v == "":
            return f"{label} = None"
        return f"{label} = {_editor_format_value_display(v)}"
    dotted, typ = row[1], row[2]
    val = _config_get(cfg, dotted)
    label = _editor_label_from_key(_editor_short_dotted_key(dotted))
    if typ in ("key", "pad") and (val is None or val == ""):
        return f"{label} = None"
    return f"{label} = {_editor_format_value_display(val)}"


def _editor_next_field(rows: List[Any], idx: int, delta: int) -> int:
    n = len(rows)
    for _ in range(n + 1):
        idx = (idx + delta) % n
        if rows[idx][0] != "hdr":
            return idx
    return idx


def _editor_wrap_banner_to_width(font: _EditorFont, text: str, max_width_px: int) -> List[str]:
    """Word-wrap *text* so each line is at most *max_width_px* wide when rendered."""

    words = text.split()
    if not words:
        return [""]
    lines: List[str] = []
    cur = ""

    def split_oversized(word: str) -> str:
        """Emit full-width slices of *word* onto *lines*; return trailing fragment for *cur*."""

        frag = ""
        for ch in word:
            cand = frag + ch
            if font.measure_text_width(cand) <= max_width_px:
                frag = cand
            else:
                if frag:
                    lines.append(frag)
                frag = ch
        return frag

    for w in words:
        cand = f"{cur} {w}".strip()
        if font.measure_text_width(cand) <= max_width_px:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            if font.measure_text_width(w) <= max_width_px:
                cur = w
            else:
                cur = split_oversized(w)
    if cur:
        lines.append(cur)
    return lines


def _editor_layout_metrics(
    height: int,
    font: _EditorFont,
    title_font: _EditorFont,
    *,
    help_line_count: int,
) -> tuple[int, int, int, int]:
    """Return ``(content_top_y, line_skip, max_visible_rows, foot_y)``.

    *foot_y* is the Y coordinate for the footer block (FILE line); list rows must
    end above it. Reserve FILE + SAVED + gap + up to four wrapped joystick lines.
    """

    ty = 12 + title_font.line_skip + 8 + help_line_count * font.line_skip + 6
    top = ty
    line_h = font.line_skip
    footer_slots = 7
    footer_margin_bottom = 12
    foot_y = height - footer_slots * line_h - footer_margin_bottom
    vis_h = max(line_h, foot_y - top)
    max_lines = max(1, vis_h // line_h)
    return top, line_h, max_lines, foot_y


def _editor_clamp_first_visible(
    first: int, selected: int, n_rows: int, max_lines: int
) -> int:
    """Keep the viewport fixed until the selection crosses the top or bottom edge."""

    if n_rows <= max_lines:
        return 0
    last_first = n_rows - max_lines
    first = max(0, min(first, last_first))
    if selected < first:
        first = selected
    # When selection reaches the first visible row while moving up, reveal one
    # extra line so section headers (e.g. "--- VIDEO ---") become reachable again.
    if selected == first and first > 0:
        first -= 1
    if selected >= first + max_lines:
        first = selected - max_lines + 1
    return max(0, min(first, last_first))


def _editor_row_index_at_y(
    mouse_y: int, content_top: int, first: int, line_h: int, n_rows: int
) -> Optional[int]:
    if mouse_y < content_top:
        return None
    rel = (mouse_y - content_top) // line_h
    i = first + rel
    if i < 0 or i >= n_rows:
        return None
    return i


def _editor_int_nudge_floor(dotted: str) -> int:
    """Minimum value when nudging int fields with LEFT (never below this)."""

    if dotted == "video.scale":
        return 1
    return 0


def _coerce_numeric_input(dotted: str, typ: str, raw: float) -> Any:
    """Clamp parsed numeric to sensible ranges for known keys."""

    if typ == "int":
        iv = int(round(raw))
        if "udp_port" in dotted:
            return max(0, min(65535, iv))
        if dotted == "video.scale":
            return max(1, min(32, iv))
        return max(0, iv)
    fv = float(raw)
    if "volume" in dotted:
        return round(max(0.0, min(4.0, fv)), 4)
    if "axis_threshold" in dotted:
        return round(max(0.0, min(1.0, fv)), 4)
    return round(max(0.0, fv), 4)


def _capture_numeric_value(
    pygame: Any,
    screen: Any,
    font: Any,
    prompt: str,
    dotted: str,
    typ: str,
    current: Any,
) -> Optional[Any]:
    """Small text prompt for int/float; Enter accepts, Esc cancels."""

    if typ == "int":
        seed = str(int(current)) if isinstance(current, (int, float)) else "0"
    else:
        seed = (
            str(float(current))
            if isinstance(current, (int, float))
            else ("0.0" if typ == "float" else "0")
        )
    buf = seed
    pygame.key.set_repeat(200, 45)
    try:
        while True:
            screen.fill(_EDITOR_BG)
            y = 24
            for line in (
                prompt,
                "Digits + Enter = OK   Esc = cancel   Backspace",
                "",
                f"> {buf}_",
            ):
                screen.blit(font.render(line[:118], _EDITOR_FG, _EDITOR_BG), (24, y))
                y += font.line_skip + 2
            pygame.display.flip()
            pygame.event.pump()
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return None
                if event.type != pygame.KEYDOWN:
                    continue
                if event.key == pygame.K_ESCAPE:
                    return None
                if event.key == pygame.K_RETURN:
                    s = buf.strip().replace(",", ".")
                    if typ == "int":
                        if not s or s == "-":
                            s = "0"
                        try:
                            v = float(s)
                        except ValueError:
                            continue
                        return _coerce_numeric_input(dotted, typ, v)
                    if not s or s in (".", "-", "-."):
                        s = "0"
                    try:
                        v = float(s)
                    except ValueError:
                        continue
                    return _coerce_numeric_input(dotted, typ, v)
                if event.key == pygame.K_BACKSPACE:
                    buf = buf[:-1]
                    continue
                ch = event.unicode
                if typ == "int":
                    if ch in "0123456789" or (ch == "-" and not buf):
                        buf += ch
                elif ch in "0123456789":
                    buf += ch
                elif ch in ".," and "." not in buf.replace(",", "."):
                    buf += "."
            pygame.time.wait(10)
    finally:
        pygame.key.set_repeat(400, 85)


def _capture_keyboard_binding(pygame: Any, screen: Any, font: _EditorFont) -> Optional[str]:
    msg = "Press a key to bind (Esc cancel)"
    deadline = pygame.time.get_ticks() + 15_000
    pygame.key.set_repeat(0)
    try:
        while pygame.time.get_ticks() < deadline:
            screen.fill((0, 0, 0))
            screen.blit(font.render(msg, _EDITOR_FG, (0, 0, 0)), (24, 24))
            pygame.display.flip()
            pygame.event.pump()
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return None
                if event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_RETURN):
                        return None
                    return _friendly_keyboard_name(pygame, event.key)
            pygame.time.wait(10)
        return None
    finally:
        pygame.key.set_repeat(400, 85)


def _run_pygame_config_editor(path: Path) -> int:
    """Pygame TOML editor with C64 styling, per-port joystick blocks, and safe quit."""
    try:
        import pygame
    except ImportError:
        print("ERROR: pygame is required for --edit")
        return 1

    try:
        cfg = load_config(path) if path.is_file() else _deep_merge(DEFAULT_CONFIG, {})
        _migrate_legacy_gamepad(cfg)
    except Exception as exc:
        print(f"ERROR: failed to load {path}: {exc}")
        return 1

    rows = _editor_build_rows()
    pygame.init()
    pygame.key.set_repeat(400, 85)
    width, height = 1100, 760
    screen = pygame.display.set_mode((width, height))
    pygame.display.set_caption("c64py — configuration")
    chargen = _try_read_chargen_4k()
    font = _EditorFont(pygame, chargen)
    title_font = _EditorFont(pygame, chargen)
    title_font.px = 3

    editor_help_lines: tuple[str, ...] = (
        "UP/DOWN / WHEEL: move   CLICK: row   DOUBLE-CLICK: edit",
        "DEL / X: clear binding   LEFT/RIGHT: nudge value",
        "ENTER: edit   F10: factory reset   S: save   Q / ESC: quit",
    )
    editor_help_n = len(editor_help_lines)

    _, joystick_banner = _editor_refresh_joysticks(pygame)

    selected = _editor_next_field(rows, 0, 1)
    _, _, _init_max, _ = _editor_layout_metrics(
        height, font, title_font, help_line_count=editor_help_n
    )
    first_visible = _editor_clamp_first_visible(0, selected, len(rows), _init_max)
    dirty = False
    running = True
    mode = "edit"  # "edit" | "quit_prompt" | "reset_prompt"
    quit_sel = 0
    reset_sel = 0

    def save_cfg() -> bool:
        nonlocal dirty
        try:
            txt = dumps_config(cfg)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(txt, encoding="utf-8")
            dirty = False
            return True
        except Exception as exc:
            print(f"ERROR: save failed: {exc}")
            return False

    def activate_field(key: int) -> None:
        """Apply LEFT / RIGHT / RETURN semantics for the current ``selected`` row."""
        nonlocal dirty, joystick_banner
        row = rows[selected]
        kind = row[0]
        if kind == "hdr":
            return
        if kind == "fire":
            port = row[1]
            if key == pygame.K_RETURN:
                name = _capture_keyboard_binding(pygame, screen, font)
                if name:
                    _joy_fire_set(cfg, port, name)
                    dirty = True
            return
        if kind != "fld":
            return
        dotted, typ, extra = row[1], row[2], row[3]
        cur = _config_get(cfg, dotted)
        if typ == "bool":
            _config_set(cfg, dotted, not bool(cur))
            dirty = True
        elif typ == "choice":
            opts = list(extra or ())
            if opts:
                idx = opts.index(cur) if cur in opts else 0
                if key == pygame.K_LEFT:
                    idx = (idx - 1) % len(opts)
                else:
                    idx = (idx + 1) % len(opts)
                _config_set(cfg, dotted, opts[idx])
                dirty = True
        elif typ in ("int", "float"):
            if key == pygame.K_RETURN:
                newv = _capture_numeric_value(
                    pygame,
                    screen,
                    font,
                    f"New value for {dotted}",
                    dotted,
                    typ,
                    cur,
                )
                if newv is not None:
                    _config_set(cfg, dotted, newv)
                    dirty = True
            else:
                delta = -1 if key == pygame.K_LEFT else 1
                if typ == "int":
                    base = int(cur) if isinstance(cur, (int, float)) else 0
                    lo = _editor_int_nudge_floor(dotted)
                    hi = 65535 if "udp_port" in dotted else 10_000_000
                    if dotted == "video.scale":
                        hi = 32
                    _config_set(cfg, dotted, max(lo, min(hi, base + delta)))
                else:
                    base_f = float(cur) if isinstance(cur, (int, float)) else 0.0
                    step = 0.05 if "axis_threshold" in dotted else 0.1
                    _config_set(cfg, dotted, round(max(0.0, base_f + delta * step), 3))
                dirty = True
        elif typ == "key" and key == pygame.K_RETURN:
            name = _capture_keyboard_binding(pygame, screen, font)
            if name:
                _config_set(cfg, dotted, name)
                dirty = True
        elif typ == "pad" and key == pygame.K_RETURN:
            pref_port = int(row[3])
            pref = default_sdl_joystick_index_for_c64_port(pref_port)
            token = _capture_gamepad_token(pygame, screen, font, preferred_index=pref)
            if token:
                _config_set(cfg, dotted, token)
                dirty = True
            _, joystick_banner = _editor_refresh_joysticks(pygame)

    dbl_click_ms = 450
    dbl_click_tick = 0
    dbl_click_row: Optional[int] = None

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                if dirty:
                    mode, quit_sel = "quit_prompt", 0
                else:
                    running = False
            elif mode == "edit" and hasattr(pygame, "MOUSEWHEEL") and event.type == pygame.MOUSEWHEEL:
                dy = int(getattr(event, "y", 0))
                if dy != 0:
                    delta = 1 if dy > 0 else -1
                    selected = _editor_next_field(rows, selected, delta)
                    _, _, max_lines, _ = _editor_layout_metrics(
                        height, font, title_font, help_line_count=editor_help_n
                    )
                    first_visible = _editor_clamp_first_visible(
                        first_visible, selected, len(rows), max_lines
                    )
            elif (
                mode == "edit"
                and event.type == pygame.MOUSEBUTTONDOWN
                and event.button == 1
            ):
                top, line_h, max_lines, _ = _editor_layout_metrics(
                    height, font, title_font, help_line_count=editor_help_n
                )
                row_i = _editor_row_index_at_y(
                    int(event.pos[1]), top, first_visible, line_h, len(rows)
                )
                now_t = pygame.time.get_ticks()
                if row_i is not None:
                    is_dbl = (
                        rows[row_i][0] != "hdr"
                        and row_i == dbl_click_row
                        and (now_t - dbl_click_tick) <= dbl_click_ms
                    )
                    if rows[row_i][0] == "hdr":
                        selected = _editor_next_field(rows, row_i, 1)
                    else:
                        selected = row_i
                    first_visible = _editor_clamp_first_visible(
                        first_visible, selected, len(rows), max_lines
                    )
                    if is_dbl:
                        activate_field(pygame.K_RETURN)
                    dbl_click_tick = now_t
                    dbl_click_row = row_i if rows[row_i][0] != "hdr" else None
                else:
                    dbl_click_row = None
            elif (
                mode == "reset_prompt"
                and event.type == pygame.MOUSEBUTTONDOWN
                and event.button == 1
            ):
                line_h = font.line_skip
                bx = 80
                by0 = height // 2 - 56
                opt_y0 = by0 + line_h + 8
                mx, my = int(event.pos[0]), int(event.pos[1])
                if bx <= mx < width - 40 and opt_y0 <= my < opt_y0 + 2 * line_h:
                    j = min(1, max(0, (my - opt_y0) // line_h))
                    reset_sel = j
                    if j == 0:
                        cfg.clear()
                        cfg.update(copy.deepcopy(DEFAULT_CONFIG))
                        save_cfg()
                    mode = "edit"
                elif my < by0 - 24 or my > opt_y0 + 2 * line_h + 28 or mx < 40 or mx > width - 40:
                    mode = "edit"
            elif (
                mode == "quit_prompt"
                and event.type == pygame.MOUSEBUTTONDOWN
                and event.button == 1
            ):
                line_h = font.line_skip
                bx = 80
                by0 = height // 2 - 60
                opt_y0 = by0 + line_h + 8
                mx, my = int(event.pos[0]), int(event.pos[1])
                if bx <= mx < width - 40 and opt_y0 <= my < opt_y0 + 3 * line_h:
                    j = min(2, max(0, (my - opt_y0) // line_h))
                    quit_sel = j
                    if j == 0:
                        save_cfg()
                        running = False
                    elif j == 1:
                        running = False
                    else:
                        mode = "edit"
                elif my < by0 - 24 or my > opt_y0 + 3 * line_h + 32 or mx < 40 or mx > width - 40:
                    mode = "edit"
            elif hasattr(pygame, "JOYDEVICEADDED") and event.type == pygame.JOYDEVICEADDED:
                _, joystick_banner = _editor_refresh_joysticks(pygame)
            elif event.type == pygame.KEYDOWN and mode == "reset_prompt":
                if event.key == pygame.K_UP:
                    reset_sel = (reset_sel - 1) % 2
                elif event.key == pygame.K_DOWN:
                    reset_sel = (reset_sel + 1) % 2
                elif event.key == pygame.K_ESCAPE:
                    mode = "edit"
                elif event.key == pygame.K_RETURN:
                    if reset_sel == 0:
                        cfg.clear()
                        cfg.update(copy.deepcopy(DEFAULT_CONFIG))
                        save_cfg()
                    mode = "edit"
            elif event.type == pygame.KEYDOWN and mode == "quit_prompt":
                if event.key == pygame.K_UP:
                    quit_sel = (quit_sel - 1) % 3
                elif event.key == pygame.K_DOWN:
                    quit_sel = (quit_sel + 1) % 3
                elif event.key == pygame.K_ESCAPE:
                    mode = "edit"
                elif event.key == pygame.K_RETURN:
                    if quit_sel == 0:
                        save_cfg()
                        running = False
                    elif quit_sel == 1:
                        running = False
                    else:
                        mode = "edit"
            elif event.type == pygame.KEYDOWN and mode == "edit":
                if event.key == pygame.K_q and not (event.mod & pygame.KMOD_CTRL):
                    if dirty:
                        mode, quit_sel = "quit_prompt", 0
                    else:
                        running = False
                elif event.key == pygame.K_ESCAPE:
                    if dirty:
                        mode, quit_sel = "quit_prompt", 0
                    else:
                        running = False
                elif event.key == pygame.K_UP:
                    selected = _editor_next_field(rows, selected, -1)
                    _, _, max_lines, _ = _editor_layout_metrics(
                        height, font, title_font, help_line_count=editor_help_n
                    )
                    first_visible = _editor_clamp_first_visible(
                        first_visible, selected, len(rows), max_lines
                    )
                elif event.key == pygame.K_DOWN:
                    selected = _editor_next_field(rows, selected, 1)
                    _, _, max_lines, _ = _editor_layout_metrics(
                        height, font, title_font, help_line_count=editor_help_n
                    )
                    first_visible = _editor_clamp_first_visible(
                        first_visible, selected, len(rows), max_lines
                    )
                elif event.key == pygame.K_F10:
                    mode, reset_sel = "reset_prompt", 0
                elif event.key == pygame.K_s:
                    save_cfg()
                elif event.key in (pygame.K_DELETE, pygame.K_x) and not (
                    event.mod & pygame.KMOD_CTRL
                ):
                    row = rows[selected]
                    rk = row[0]
                    if rk == "fld" and row[2] in ("key", "pad"):
                        _editor_clear_input_binding(cfg, row)
                        dirty = True
                    elif rk == "fire":
                        _editor_clear_input_binding(cfg, row)
                        dirty = True
                elif event.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_RETURN):
                    activate_field(int(event.key))

        screen.fill(_EDITOR_BG)
        ty = 12
        screen.blit(
            title_font.render("**** C64PY CONFIGURATION ****", _EDITOR_SEL, _EDITOR_BG),
            (24, ty),
        )
        ty += title_font.line_skip + 8
        for hl in editor_help_lines:
            screen.blit(font.render(hl, _EDITOR_ACCENT, _EDITOR_BG), (24, ty))
            ty += font.line_skip

        ty += 6
        top, line_h, max_lines, foot_y = _editor_layout_metrics(
            height, font, title_font, help_line_count=editor_help_n
        )
        n = len(rows)
        first_visible = _editor_clamp_first_visible(first_visible, selected, n, max_lines)
        for i in range(first_visible, min(n, first_visible + max_lines)):
            row = rows[i]
            text = _editor_row_text(cfg, row)
            is_hdr = row[0] == "hdr"
            if is_hdr:
                col = _EDITOR_HDR
            elif i == selected:
                col = _EDITOR_SEL
            else:
                col = _EDITOR_FG
            y = top + (i - first_visible) * line_h
            screen.blit(font.render(text, col, _EDITOR_BG), (28, y))

        yf = foot_y
        screen.blit(
            font.render(f"FILE: {path}", _EDITOR_ACCENT, _EDITOR_BG),
            (20, yf),
        )
        yf += line_h
        screen.blit(
            font.render(
                "* UNSAVED *" if dirty else "SAVED",
                _EDITOR_SEL if dirty else _EDITOR_ACCENT,
                _EDITOR_BG,
            ),
            (20, yf),
        )
        yf += line_h
        yf += line_h
        banner_w = max(160, width - 40)
        joy_lines = _editor_wrap_banner_to_width(font, joystick_banner, banner_w)[:4]
        for jl in joy_lines:
            screen.blit(
                font.render(jl, _EDITOR_ACCENT, _EDITOR_BG),
                (20, yf),
            )
            yf += line_h

        if mode == "reset_prompt":
            overlay = pygame.Surface((width, height))
            overlay.set_alpha(220)
            overlay.fill((0, 0, 0))
            screen.blit(overlay, (0, 0))
            opts = (
                "RESET TO SHIPPED DEFAULTS (save file)",
                "CANCEL",
            )
            bx, by = 80, height // 2 - 56
            screen.blit(
                font.render("REPLACE CONFIG WITH DEFAULTS?", _EDITOR_SEL, (0, 0, 0)),
                (bx, by),
            )
            by += line_h + 8
            for j, label in enumerate(opts):
                prefix = ">" if j == reset_sel else " "
                col = _EDITOR_SEL if j == reset_sel else _EDITOR_FG
                screen.blit(font.render(f"{prefix} {label}", col, (0, 0, 0)), (bx, by))
                by += line_h
            screen.blit(
                font.render(
                    "UP/DOWN  CLICK  ENTER CONFIRM  ESC CANCEL",
                    _EDITOR_ACCENT,
                    (0, 0, 0),
                ),
                (bx, by + 8),
            )
        elif mode == "quit_prompt":
            overlay = pygame.Surface((width, height))
            overlay.set_alpha(220)
            overlay.fill((0, 0, 0))
            screen.blit(overlay, (0, 0))
            opts = (
                "SAVE AND QUIT",
                "DISCARD CHANGES AND QUIT",
                "CANCEL",
            )
            bx, by = 80, height // 2 - 60
            screen.blit(font.render("UNSAVED CHANGES — CHOOSE:", _EDITOR_SEL, (0, 0, 0)), (bx, by))
            by += line_h + 8
            for j, label in enumerate(opts):
                prefix = ">" if j == quit_sel else " "
                col = _EDITOR_SEL if j == quit_sel else _EDITOR_FG
                screen.blit(font.render(f"{prefix} {label}", col, (0, 0, 0)), (bx, by))
                by += line_h
            screen.blit(
                font.render(
                    "UP/DOWN (hold repeats)  CLICK OPTION  ENTER CONFIRM  ESC CANCEL",
                    _EDITOR_ACCENT,
                    (0, 0, 0),
                ),
                (bx, by + 8),
            )

        pygame.display.flip()

    pygame.quit()
    return 0


def _editor_refresh_joysticks(pygame: Any) -> tuple[list[Any], str]:
    """Open every SDL joystick so pygame receives button/axis/hat events.

    ``pygame.joystick.init()`` plus ``Joystick(i)`` opens each device on SDL2;
    per-instance ``Joystick.init()`` is deprecated (pygame-ce ≥2.4) and must not
    be called.
    """
    pygame.joystick.init()
    pygame.event.pump()
    for _ in range(30):
        pygame.event.get()
        pygame.event.pump()
    sticks: list[Any] = []
    n = pygame.joystick.get_count()
    parts: list[str] = []
    for i in range(n):
        try:
            j = pygame.joystick.Joystick(i)
            sticks.append(j)
            parts.append(f"{i}:{j.get_name()!r}")
        except Exception as exc:
            parts.append(f"{i}:<{exc}>")
    if n == 0:
        banner = "Joysticks: none — pair BT, wake the gamepad, click this window, wait; or plug USB"
    else:
        banner = "Joysticks: " + " | ".join(parts)
    return sticks, banner


def _snap_joy_state(joy: Any) -> tuple[list[int], list[float], list[tuple[int, int]]]:
    nb = joy.get_numbuttons()
    na = joy.get_numaxes()
    nh = joy.get_numhats()
    buttons = [int(joy.get_button(i)) for i in range(nb)]
    axes = [float(joy.get_axis(i)) for i in range(na)]
    hats = [tuple(joy.get_hat(i)) for i in range(nh)]
    return buttons, axes, hats


def _capture_gamepad_token(
    pygame: Any,
    screen: Any,
    font: Any,
    *,
    preferred_index: int = 0,
    timeout_ms: int = 30_000,
) -> Optional[Union[str, Dict[str, Any]]]:
    """Capture one mapping value from a physical gamepad.

    Returns a legacy string token or a ``{guid, token, host_index?}`` table when
    SDL reports a GUID (adds ``host_index`` if two devices share that GUID).

    Uses SDL events *and* polling. Bluetooth controllers often need opened
    devices plus polling because they do not emit a steady JOYAXISMOTION stream.
    """
    axis_thresh = 0.5
    axis_release = 0.35
    sticks, banner = _editor_refresh_joysticks(pygame)
    prev: dict[int, tuple[list[int], list[float], list[tuple[int, int]]]] = {}
    settle_until = pygame.time.get_ticks() + 250
    deadline = pygame.time.get_ticks() + timeout_ms
    last_rescan = pygame.time.get_ticks()

    def stick_order() -> list[tuple[int, Any]]:
        if not sticks:
            return []
        order = list(range(len(sticks)))
        if 0 <= preferred_index < len(sticks) and preferred_index in order:
            order.remove(preferred_index)
            return [(preferred_index, sticks[preferred_index])] + [
                (i, sticks[i]) for i in order
            ]
        return [(i, sticks[i]) for i in range(len(sticks))]

    pygame.key.set_repeat(0)
    try:
        while pygame.time.get_ticks() < deadline:
            now = pygame.time.get_ticks()
            if not sticks and now - last_rescan > 1500:
                sticks, banner = _editor_refresh_joysticks(pygame)
                last_rescan = now

            cap_bg = (12, 14, 22)
            cap_fg = (230, 230, 230)
            screen.fill(cap_bg)
            y = 20
            for line in (
                "Capture control — press a button, move a stick, or push a D-pad",
                f"(Esc cancel, {timeout_ms // 1000}s timeout)",
                "",
                banner,
                "",
                "Tip: focus this window; wake the Bluetooth pad; try A/B or triggers.",
            ):
                screen.blit(font.render(line, cap_fg, cap_bg), (24, y))
                y += 26
            pygame.display.flip()

            pygame.event.pump()
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return None
                if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    return None
                if hasattr(pygame, "JOYDEVICEADDED") and event.type == pygame.JOYDEVICEADDED:
                    sticks, banner = _editor_refresh_joysticks(pygame)
                    prev.clear()
                    last_rescan = now
                if event.type == pygame.JOYBUTTONDOWN:
                    joy_e = _joy_from_instance_id(sticks, getattr(event, "instance_id", None))
                    return gamepad_binding_value_for_capture(
                        joy_e, sticks, f"button{int(event.button)}"
                    )
                if event.type == pygame.JOYHATMOTION:
                    hat = int(event.hat)
                    x, yv = event.value
                    tok: Optional[str] = None
                    if yv > 0:
                        tok = f"hat{hat}:up"
                    elif yv < 0:
                        tok = f"hat{hat}:down"
                    elif x < 0:
                        tok = f"hat{hat}:left"
                    elif x > 0:
                        tok = f"hat{hat}:right"
                    if tok:
                        joy_e = _joy_from_instance_id(sticks, getattr(event, "instance_id", None))
                        return gamepad_binding_value_for_capture(joy_e, sticks, tok)
                if event.type == pygame.JOYAXISMOTION and abs(float(event.value)) >= axis_thresh:
                    axis = int(event.axis)
                    sign = "+" if float(event.value) > 0 else "-"
                    joy_e = _joy_from_instance_id(sticks, getattr(event, "instance_id", None))
                    return gamepad_binding_value_for_capture(joy_e, sticks, f"axis{axis}{sign}")

            if now < settle_until:
                for idx, joy in stick_order():
                    prev[id(joy)] = _snap_joy_state(joy)
                pygame.time.wait(10)
                continue

            for idx, joy in stick_order():
                jid = id(joy)
                b_now, a_now, h_now = _snap_joy_state(joy)
                b_old, a_old, h_old = prev.get(jid, (b_now, a_now, h_now))

                for bi in range(min(len(b_old), len(b_now))):
                    was, cur = b_old[bi], b_now[bi]
                    if cur and not was:
                        return gamepad_binding_value_for_capture(joy, sticks, f"button{bi}")

                for ai in range(min(len(a_old), len(a_now))):
                    was, cur = a_old[ai], a_now[ai]
                    if was >= -axis_release and was <= axis_release:
                        if cur >= axis_thresh:
                            return gamepad_binding_value_for_capture(joy, sticks, f"axis{ai}+")
                        if cur <= -axis_thresh:
                            return gamepad_binding_value_for_capture(joy, sticks, f"axis{ai}-")

                for hi in range(min(len(h_old), len(h_now))):
                    was, cur = h_old[hi], h_now[hi]
                    if cur != (0, 0) and cur != was:
                        x, yv = cur
                        if yv > 0:
                            return gamepad_binding_value_for_capture(joy, sticks, f"hat{hi}:up")
                        if yv < 0:
                            return gamepad_binding_value_for_capture(joy, sticks, f"hat{hi}:down")
                        if x < 0:
                            return gamepad_binding_value_for_capture(joy, sticks, f"hat{hi}:left")
                        if x > 0:
                            return gamepad_binding_value_for_capture(joy, sticks, f"hat{hi}:right")

                prev[jid] = (b_now, a_now, h_now)

            pygame.time.wait(10)
        return None
    finally:
        pygame.key.set_repeat(400, 85)


def main(argv: Optional[List[str]] = None, *, _invoked_as_main_script: bool = False) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="c64py configuration utilities")
    ap.add_argument("--config", default=None, metavar="FILE", help="Path to config file (default: first search path)")
    ap.add_argument("--write-default", action="store_true", help="Write default config to --config target and exit")
    ap.add_argument("--force", action="store_true", help="Allow overwriting config with --write-default")
    ap.add_argument("--edit", action="store_true", help="Launch pygame config editor")
    ap.add_argument(
        "--dump",
        action="store_true",
        help="Print merged config TOML to stdout and exit (default for `python -m c64py.config`)",
    )
    ns = ap.parse_args(argv)

    target = Path(ns.config).expanduser() if ns.config else _config_search_paths()[0]
    if ns.write_default:
        write_config(target, force=ns.force)
        print(f"Wrote default config to {target}")
        return 0
    # Launch editor when this file is run as a script (`python .../config.py`), unless
    # `--dump` is passed. `python -m c64py.config` defaults to `--dump` unless `--edit`.
    want_editor = ns.edit or (_invoked_as_main_script and not ns.dump)
    if want_editor:
        return _run_pygame_config_editor(target)

    cfg = load_config(target) if target.is_file() else load_config()
    print(dumps_config(cfg))
    return 0


def _launched_as_path_to_this_file() -> bool:
    """True when user ran ``python /path/to/config.py`` (not ``python -m c64py.config``).

    Both forms set ``sys.argv[0]`` to this file, so we use ``sys.orig_argv`` when
    available (Python 3.10+) to detect ``-m``.
    """
    import sys
    from pathlib import Path

    try:
        argv0 = Path(sys.argv[0]).resolve()
        here = Path(__file__).resolve()
    except OSError:
        return False
    if argv0 != here:
        return False
    orig = getattr(sys, "orig_argv", None)
    if orig is not None and len(orig) >= 2 and orig[1] == "-m":
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main(_invoked_as_main_script=_launched_as_path_to_this_file()))
