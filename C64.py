#!/usr/bin/env python3
"""
C64 Emulator - Text mode Python implementation

A Commodore 64 emulator focused on text mode operation.
Can load and run PRG files, dump memory, and communicate via TCP/UDP.

Usage:
    python C64.py [media.prg|media.d64|media.bas]
    python C64.py --tcp-port 1234
    python C64.py program.prg --udp-port 1235
"""

from __future__ import annotations

import argparse
import atexit
import functools
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from argparse import Namespace
from pathlib import Path
from typing import Optional, Tuple

# Handle both direct execution and module import
try:
    from . import config as _config_mod
    from .debug import UdpDebugLogger, ViceTraceLogger
    from .emulator import C64
    from .server import EmulatorServer
    from .constants import (
        BLNCT,
        BLNSW,
        CURSOR_COL_ADDR,
        CURSOR_ROW_ADDR,
        INPUT_BUFFER_INDEX_ADDR,
        INPUT_BUFFER_LEN_ADDR,
        KEYBOARD_BUFFER_BASE,
        KEYBOARD_BUFFER_LEN_ADDR,
        SCREEN_MEM,
    )
except ImportError:
    # When run directly, add parent directory to path
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from c64py import config as _config_mod
    from c64py.debug import UdpDebugLogger, ViceTraceLogger
    from c64py.emulator import C64
    from c64py.server import EmulatorServer
    from c64py.constants import (
        BLNCT,
        BLNSW,
        CURSOR_COL_ADDR,
        CURSOR_ROW_ADDR,
        INPUT_BUFFER_INDEX_ADDR,
        INPUT_BUFFER_LEN_ADDR,
        KEYBOARD_BUFFER_BASE,
        KEYBOARD_BUFFER_LEN_ADDR,
        SCREEN_MEM,
    )


def _show_speed(
    emu: "C64",
    cycles: int,
    *,
    wall_start_fallback: float,
    target_hz: Optional[float] = None,
) -> None:
    """Display emulation speed statistics.

    Uses wall time from :meth:`C64.reset_speed_throttle` (CPU loop start) so ROM/UI setup
    before the emulator thread is not counted — that mismatch was skewing the summary ~4%
    low vs the per-second throttle logs.
    """
    import time

    t0 = getattr(emu, "_speed_throttle_run_wall_start", None)
    if t0 is not None and cycles > 0:
        elapsed = time.perf_counter() - t0
    else:
        elapsed = time.perf_counter() - wall_start_fallback
    if elapsed > 0 and cycles > 0:
        mhz = cycles / elapsed / 1e6
        print(f"\n=== Emulation Speed ===")
        print(f"Cycles: {cycles:,}")
        print(f"Time:   {elapsed:.2f}s  (wall since CPU thread start; excludes earlier ROM/UI init)")
        if target_hz and target_hz > 0:
            pct = (mhz * 1e6) / target_hz
            label = "PAL" if abs(target_hz - 985_248) < abs(target_hz - 1_022_727) else "NTSC"
            print(
                f"Speed:  {mhz:.2f} MHz ({pct:.0%} of {label} C64 CPU, {target_hz/1e6:.3f} MHz nominal)"
            )
        else:
            print(f"Speed:  {mhz:.2f} MHz")


def _unlink_if_exists(path: str) -> None:
    try:
        if path and os.path.isfile(path):
            os.unlink(path)
    except OSError:
        pass


def _resolve_media_cli_arg(media_path: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Classify startup media: ``(prg_path, d64_path, temp_prg_to_delete)``.

    ``temp_prg_to_delete`` is set when a ``.bas`` file was converted with VICE ``petcat``.
    """
    path = os.path.normpath(os.path.abspath(media_path))
    if not os.path.isfile(path):
        print(f"ERROR: file not found: {media_path}", file=sys.stderr)
        sys.exit(1)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".d64":
        return None, path, None
    if ext == ".bas":
        petcat = shutil.which("petcat")
        if not petcat:
            print(
                "ERROR: .bas files require VICE `petcat` on PATH (e.g. install the VICE package for your OS).",
                file=sys.stderr,
            )
            sys.exit(1)
        fd, tmp_path = tempfile.mkstemp(prefix="c64py_", suffix=".prg")
        os.close(fd)
        try:
            r = subprocess.run(
                [petcat, "-w2", "-o", tmp_path, path],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _unlink_if_exists(tmp_path)
            print(f"ERROR: petcat failed: {exc}", file=sys.stderr)
            sys.exit(1)
        if r.returncode != 0:
            _unlink_if_exists(tmp_path)
            err = (r.stderr or r.stdout or "").strip()
            msg = f"ERROR: petcat exited {r.returncode}"
            if err:
                msg += f": {err}"
            print(msg, file=sys.stderr)
            sys.exit(1)
        return tmp_path, None, tmp_path
    return path, None, None


def _print_benchmark_record(
    args: Namespace,
    emu: "C64",
    *,
    wall_start_fallback: float,
    prg_display_basename: Optional[str] = None,
) -> None:
    """Single-line JSON for scripts: grep '^C64PY_BENCHMARK '."""
    cycles = emu.current_cycles
    t0 = getattr(emu, "_speed_throttle_run_wall_start", None)
    if t0 is not None and cycles > 0:
        elapsed = time.perf_counter() - t0
    else:
        elapsed = time.perf_counter() - wall_start_fallback
    mhz = (cycles / elapsed / 1e6) if elapsed > 0 and cycles > 0 else 0.0
    rec = {
        "C64PY_BENCHMARK": 1,
        "accurate_vic": bool(emu.accurate_vic),
        "vic_emulation": getattr(emu, "vic_emulation", "fast"),
        "cycles": cycles,
        "emulated_cpu_mhz": round(mhz, 4),
        "audio_emulation": str(args.audio_emulation),
        "max_cycles_arg": args.max_cycles,
        "prg": prg_display_basename,
        "schema": 1,
        "target_hz": emu.target_cpu_hz,
        "turbo": bool(args.turbo),
        "video_standard": args.video_standard,
        "wall_seconds": round(elapsed, 6),
    }
    print("C64PY_BENCHMARK " + json.dumps(rec, sort_keys=True))


def _default_snapshot_path(prg_basename: Optional[str], cycle: Optional[int], kind: str) -> str:
    """Synthesize snapshots/<prg>_<kind>_<cycle>.snap when user omitted PATH."""
    name = (prg_basename or "session").rsplit(".", 1)[0]
    safe = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in name) or "session"
    tag = f"{kind}_{int(cycle)}" if cycle is not None else kind
    return os.path.join("snapshots", f"{safe}_{tag}.snap")


def _configure_snapshot_saving(args, emu, prg_basename: Optional[str]) -> None:
    """Translate --save-snapshot-* flags into emulator-side scheduling."""
    at_cycle_raw = getattr(args, "save_snapshot_at_cycle", None)
    if at_cycle_raw:
        spec = str(at_cycle_raw)
        path: Optional[str] = None
        if ":" in spec:
            cstr, path = spec.split(":", 1)
            path = path.strip() or None
        else:
            cstr = spec
        try:
            cycle = int(cstr.strip(), 0)
        except ValueError:
            print(
                f"ERROR: --save-snapshot-at-cycle expected CYCLE[:PATH], got {spec!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        if path is None:
            path = _default_snapshot_path(prg_basename, cycle, "cycle")
        note = f"save-at-cycle={cycle} prg={prg_basename or ''}"
        emu._snapshot_at_cycle = (cycle, path, note)

    at_exit_raw = getattr(args, "save_snapshot_at_exit", None)
    if at_exit_raw is not None:
        if at_exit_raw == "__default__":
            # Path resolved at exit time from current_cycles (not known yet).
            path = _default_snapshot_path(prg_basename, None, "exit")
        else:
            path = str(at_exit_raw)
        note = f"save-at-exit prg={prg_basename or ''}"
        emu._snapshot_at_exit = (path, note)


def _parse_debug_inject_pair(lhs_s: str, val_s: str, *, source: str) -> tuple[int | str, int]:
    val = int(val_s.strip(), 16)
    if lhs_s in ("a", "x", "y", "p", "flags"):
        return (lhs_s if lhs_s != "flags" else "p", val)
    return (int(lhs_s, 16), val)


def _parse_debug_inject_map_string(map_str: str) -> list[tuple[int | str, int]]:
    pairs: list[tuple[int | str, int]] = []
    for part in map_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            print(
                f"ERROR: bad --debug-inject-map fragment {part!r} (want addr=val)",
                file=sys.stderr,
            )
            sys.exit(1)
        lhs, rhs = part.split("=", 1)
        pairs.append(_parse_debug_inject_pair(lhs.strip().lower(), rhs, source="--debug-inject-map"))
    return pairs


def _parse_debug_inject_file(path: str) -> list[tuple[int | str, int]]:
    p = Path(path)
    if not p.is_file():
        print(f"ERROR: --debug-inject-file not found: {path}", file=sys.stderr)
        sys.exit(1)
    pairs: list[tuple[int | str, int]] = []
    for lineno, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            print(f"ERROR: {path}:{lineno}: expected addr=value or reg=value", file=sys.stderr)
            sys.exit(1)
        lhs, rhs = s.split("=", 1)
        pairs.append(_parse_debug_inject_pair(lhs.strip().lower(), rhs, source=f"{path}:{lineno}"))
    return pairs


def _check_rust_core_available(
    vic_emulation: str,
    emu: C64,
    *,
    show_ui_logs: bool,
    no_colors: bool,
) -> None:
    """Check if Rust core is available when required (accurate-rust mode).
    
    If accurate-rust is selected but Rust is not available, exit with an error.
    Other modes (fast, accurate-python) only print a warning.
    """
    v = os.environ.get("C64PY_USE_RUST_FAST", "1").strip().lower()
    if v in ("0", "no", "false"):
        return
    try:
        from . import _core
    except ImportError:
        from c64py import _core
    if _core.is_available:
        return
    
    # For accurate-rust, we MUST have the Rust core - fail fast
    if vic_emulation == "accurate-rust":
        lines = (
            "ERROR: --vic-emulation accurate-rust requires the Rust core (c64py_rust_core), but it is not available.",
            "Fix (same Python / venv as this command):",
            "  1. Ensure Rust >= 1.83 is installed:",
            "       macOS:  brew install rust",
            "       Linux:  curl https://sh.rustup.rs -sSf | sh   # or: sudo apt-get install rustc",
            "       (brew/apt Rust may be outdated; rustup is the most reliable way to get 1.83+)",
            "       Check:  rustc --version",
            "  2. Install maturin (if not already):",
            "       pip install 'maturin>=1.7,<2'",
            "  3. Build the extension:",
            "       maturin develop --manifest-path rust/c64py-core/Cargo.toml",
            "Or skip the Rust core and use: --vic-emulation accurate-python",
        )
        for line in lines:
            print(line, file=sys.stderr)
        sys.exit(1)

    # For other modes, just warn
    lines = (
        "WARNING: Optional Rust CPU core (c64py_rust_core) is not installed — using the Python instruction loop.",
        '  The "VIC emulation: …" line is only VIC-II timing. Without the extension, --vic-emulation fast is slower;',
        "  (--vic-emulation accurate-python is unchanged: it always uses Python for cycle-accurate VIC.)",
        "  Fix — ensure Rust >= 1.83, then (same Python / venv as this command):",
        "    pip install 'maturin>=1.7,<2'",
        "    maturin develop --manifest-path rust/c64py-core/Cargo.toml",
    )
    for line in lines:
        if show_ui_logs:
            emu.interface.add_debug_log(line, style=None if no_colors else "orange")
        else:
            print(line, file=sys.stderr)


def _warn_if_rust_fast_core_unavailable(
    vic_emulation: str,
    emu: C64,
    *,
    show_ui_logs: bool,
    no_colors: bool,
) -> None:
    """Warn when the Rust CPU batch path is missing for modes that would use it (fast, accurate-rust)."""
    v = os.environ.get("C64PY_USE_RUST_FAST", "1").strip().lower()
    if v in ("0", "no", "false"):
        return
    # accurate-python runs the CPU in Python with per-cycle VIC in Python; Rust batch is never used.
    if vic_emulation == "accurate-python":
        return
    try:
        from . import _core
    except ImportError:
        from c64py import _core
    if _core.is_available:
        return
    lines = (
        "WARNING: Optional Rust CPU core (c64py_rust_core) is not installed — using the Python instruction loop.",
        '  The "VIC emulation: …" line is only VIC-II timing. Without the extension, --vic-emulation fast is slower;',
        "  --vic-emulation accurate-rust also loses the hybrid Rust VIC+CPU batch and falls back to Python.",
        "  (--vic-emulation accurate-python is unchanged: it always uses Python for cycle-accurate VIC.)",
        "  Fix (same Python / venv as this command):",
        "    maturin develop --manifest-path rust/c64py-core/Cargo.toml",
        "  Silence this hint on Python-only runs:  export C64PY_USE_RUST_FAST=0",
    )
    use_color = not no_colors and sys.stderr.isatty()
    for line in lines:
        if use_color:
            print(f"\033[33m{line}\033[0m", file=sys.stderr)
        else:
            print(line, file=sys.stderr)
    iface = getattr(emu, "interface", None)
    if show_ui_logs and iface is not None and hasattr(iface, "add_debug_log"):
        iface.add_debug_log(
            "⚠ c64py_rust_core not loaded — CPU on Python path (see stderr).",
            style="yellow",
        )


def _preparse_config_args(argv: Optional[list] = None) -> Tuple[argparse.Namespace, list]:
    """First pass: extract config-related flags before building the full parser.

    Returns ``(ns, remaining)`` where ``ns`` has ``config``, ``no_config``,
    ``write_config``, ``force_overwrite_config`` and ``remaining`` is the
    rest of the argv to be processed by the full parser.
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=None, metavar="FILE")
    pre.add_argument("--no-config", action="store_true")
    pre.add_argument(
        "--write-config",
        nargs="?",
        const="__default__",
        default=None,
        metavar="PATH",
    )
    pre.add_argument("--force-overwrite-config", action="store_true")
    return pre.parse_known_args(argv)


def main():
    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # First pass: pull out --config / --no-config / --write-config so we can
    # load the TOML before constructing the full parser. CLI explicit values
    # in the second pass will override config; config overrides hardcoded
    # defaults.
    pre_ns, _remaining = _preparse_config_args()

    if pre_ns.write_config is not None:
        target = pre_ns.write_config
        if target == "__default__":
            home = os.environ.get("HOME") or str(Path.home())
            target_path = Path(home) / ".c64py.toml"
        else:
            target_path = Path(target).expanduser()
        try:
            _config_mod.write_config(
                target_path, force=pre_ns.force_overwrite_config
            )
        except FileExistsError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"Wrote default config to {target_path}")
        sys.exit(0)

    if pre_ns.config is not None:
        cfg = _config_mod.load_config(Path(pre_ns.config).expanduser())
    else:
        cfg = _config_mod.load_config(skip_search=pre_ns.no_config)

    # Convenience accessors with defaults baked in (cfg is already merged).
    cfg_video = cfg.get("video", {})
    cfg_audio = cfg.get("audio", {})
    cfg_debug = cfg.get("debug", {})
    cfg_emulation = cfg.get("emulation", {})
    _allowed_drive_tiers = ("fast", "accurate-python", "accurate-rust")
    _raw_disk_tier = str(cfg_emulation.get("disk_emulation", "fast")).strip()
    if _raw_disk_tier not in _allowed_drive_tiers:
        print(
            "ERROR: [emulation] disk_emulation must be one of "
            f"{', '.join(_allowed_drive_tiers)}, got {_raw_disk_tier!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    drive_tier: str = _raw_disk_tier

    ap = argparse.ArgumentParser(description="C64 Emulator")
    # Re-declare the config-control flags on the full parser so they show up
    # in --help and round-trip through parse_args. Their values were already
    # consumed above; we just need them recognised.
    ap.add_argument(
        "--config",
        default=None,
        metavar="FILE",
        help="Force loading this TOML config file (skips the default search).",
    )
    ap.add_argument(
        "--no-config",
        action="store_true",
        help="Skip loading any config file; use built-in defaults only.",
    )
    ap.add_argument(
        "--write-config",
        nargs="?",
        const="__default__",
        default=None,
        metavar="PATH",
        help=(
            "Write a fully-populated default config and exit. Default path: "
            "~/.c64py.toml. Refuses to overwrite without --force-overwrite-config."
        ),
    )
    ap.add_argument(
        "--force-overwrite-config",
        action="store_true",
        help="With --write-config: overwrite an existing file.",
    )
    ap.add_argument(
        "media_file",
        nargs="?",
        metavar="FILE",
        help="PRG to load, D64 to attach as drive 8, or .bas (requires petcat) converted to PRG at startup",
    )
    ap.add_argument(
        "--rom-dir",
        default=None,
        help="Directory containing ROM files (default: auto-detect common locations)",
    )
    ap.add_argument(
        "--video-rendering",
        choices=("per-frame", "per-raster", "per-cycle", "fast", "accurate"),
        default=cfg_video.get("rendering", "per-frame"),
        help=(
            "Pygame output sampling tier. "
            "`per-frame` (aka `fast`): one VIC latch per presented frame — cheapest, but "
            "miscomposites games that change VIC registers mid-frame (raster splits). "
            "`per-raster` (aka `accurate`): one VIC/CIA2 sample per raster line, dispatched "
            "per content row — handles vertical split screens (HUD + playfield, color bars, "
            "charset/bank swaps mid-frame) for text/bitmap/MCM/ECM modes. "
            "`per-cycle`: one VIC/CIA2 sample per emulated cycle in the 320×200 window "
            "(requires `--vic-emulation accurate-python` or it is forced automatically; "
            "see docs/per_cycle_vic.md). Text and bitmap modes use that grid; sprites still "
            "latch once per frame. "
            "See docs/DEBUGGING.md."
        ),
    )
    ap.add_argument(
        "--accurate",
        action="store_true",
        help=(
            "Shortcut: set --video-rendering per-raster and --vic-emulation accurate-rust "
            "(overrides TOML defaults for those two). Drive tier stays "
            "[emulation] disk_emulation (set in c64py.toml or via the standalone "
            "c1541_emulator --emulation). If you also select `--video-rendering per-cycle` "
            "(from CLI or TOML), VIC stays on the Python cycle path (`accurate-python`) so "
            "per-cycle sampling can run."
        ),
    )
    ap.add_argument(
        "--monitor-port",
        type=int,
        default=None,
        metavar="PORT",
        help="TCP port for minimal debugger (REGS, STEP, M, BREAK); see docs/DEBUGGING.md",
    )
    ap.add_argument(
        "--tcp-drive",
        action="append",
        metavar="DEVICE:HOST:PORT",
        default=[],
        help=(
            "Attach a TCP drive client. Format: DEVICE:HOST:PORT, e.g. "
            "8:localhost:6408. Repeatable for multiple drives. Requires "
            "``python -m c64py.drives.c1541_emulator`` with ``--disk`` (existing image) "
            "or ``--new-disk`` (create blank). Example: "
            "``python -m c64py.drives.c1541_emulator --disk game.d64 --device 8 --port 6408``"
        ),
    )
    ap.add_argument(
        "--disk2",
        metavar="PATH",
        default=None,
        help="With a D64 in media_file: auto-spawn drive 9 (headless TCP) for this image.",
    )
    ap.add_argument(
        "--disk3",
        metavar="PATH",
        default=None,
        help="With a D64 in media_file: auto-spawn drive 10 (headless TCP) for this image.",
    )
    ap.add_argument("--tcp-port", type=int, help="TCP port for control interface")
    ap.add_argument("--udp-port", type=int, help="UDP port for control interface")
    ap.add_argument("--max-cycles", type=int, default=None, help="Maximum cycles to run (default: unlimited)")
    ap.add_argument("--dump-memory", help="Dump memory to file after execution")
    ap.add_argument(
        "--dump-ram-raw",
        metavar="FILE",
        default=None,
        help="After run: write exactly 65536 bytes ($0000–$FFFF) of RAM to FILE (no PRG header).",
    )
    ap.add_argument(
        "--dump-ram-sha256",
        action="store_true",
        help="After run: print one line with sha256 of full 64 KiB RAM (cheap fingerprint for diffs).",
    )
    ap.add_argument(
        "--dump-cpu-state",
        action="store_true",
        help="After run: print one line with PC, A, X, Y, SP, P and cumulative cpu_cycles.",
    )
    ap.add_argument(
        "--dump-hex-range",
        metavar="START-END",
        default=None,
        help=(
            "After run (non-graphics path), print hex dump of inclusive RAM [START,END] hex, "
            "e.g. C200-C2FF, plus sha256 of that byte range (for compare vs VICE monitor m …)."
        ),
    )
    ap.add_argument("--debug", action="store_true", help="Enable debug output")
    ap.add_argument("--udp-debug", action="store_true", default=cfg_debug.get("udp_debug", False), help="Send debug events via UDP")
    ap.add_argument(
        "--no-autoquit",
        action="store_true",
        help="Don't automatically quit when max cycles is reached (by default, --max-cycles implies autoquit)",
    )
    ap.add_argument("--udp-debug-port", type=int, default=cfg_debug.get("udp_port", 64738), help="UDP port for debug events (default: 64738)")
    ap.add_argument("--udp-debug-host", type=str, default="127.0.0.1", help="UDP host for debug events (default: 127.0.0.1)")
    ap.add_argument(
        "--screen-update-interval",
        type=float,
        default=cfg_debug.get("screen_update_interval", 0.1),
        help=(
            "Screen refresh cadence in seconds for text/headless status updates "
            "(lower values update logs/UI more frequently)."
        ),
    )
    ap.add_argument(
        "--video-standard",
        choices=["pal", "ntsc"],
        default=cfg_video.get("standard", "pal"),
        help="Video standard (pal or ntsc, default: pal)",
    )
    ap.add_argument("--no-colors", action="store_true", help="Disable ANSI color output")
    ap.add_argument("--fullscreen", action="store_true", default=cfg_video.get("fullscreen", False), help="Show only C64 screen output (no debug panel or status bar)")
    ap.add_argument(
        "--interface",
        choices=("textual", "text", "tui", "headless", "graphics", "pygame"),
        default=cfg_emulation.get("interface", "textual"),
        help="Interface mode: textual|text|tui (default), headless, graphics|pygame",
    )
    ap.add_argument("--graphics-scale", type=int, default=cfg_video.get("scale", 2), help="Graphics window scale factor (default: 2)")
    ap.add_argument(
        "--graphics-fps",
        type=int,
        default=cfg_video.get("fps", 30),
        help="Graphics target FPS / max host present rate (default: 30)",
    )
    ap.add_argument(
        "--graphics-border",
        type=int,
        default=cfg_video.get("border", 32),
        help="Graphics border size in pixels (default: 32)",
    )
    ap.add_argument(
        "--audio-emulation",
        choices=("resid", "python-sid", "disabled"),
        default=cfg_audio.get("emulation", "resid"),
        help="Audio emulation backend: resid (default), python-sid, disabled",
    )
    ap.add_argument(
        "--audio-volume",
        type=float,
        default=cfg_audio.get("volume", 1.0),
        help="Audio volume (0.0 to 1.0, where 0.0 is muted)",
    )
    ap.add_argument(
        "--audio-muted",
        action="store_true",
        help="Mute audio output (equivalent to --audio-volume 0.0)",
    )
    ap.add_argument(
        "--inject-keys",
        action="append",
        default=None,
        metavar="WHEN:WHAT",
        help=(
            "Schedule one keyboard/joystick inject (repeat flag for multiple). Format: "
            "<int>c:<what> or <float>s:<what>. Only the first : splits; <what> is sent verbatim "
            "(may start with spaces or contain text like 1c:foo). Escapes \\\\n \\\\r \\\\t; "
            "braced {F1}-{F8}, {joy1-left}, etc. See keyboard_inject module docstring."
        ),
    )
    ap.add_argument(
        "--save-snapshot-at-cycle",
        metavar="CYCLE[:PATH]",
        default=None,
        help=(
            "Save a full-state snapshot once cumulative cpu_cycles >= CYCLE. "
            "Optional :PATH overrides the default location "
            "(snapshots/<basename>_<cycle>.snap). See docs on snapshot format."
        ),
    )
    ap.add_argument(
        "--save-snapshot-at-exit",
        metavar="PATH",
        nargs="?",
        const="__default__",
        default=None,
        help=(
            "Save a snapshot when the emulator stops (max-cycles, KIL, stuck). "
            "Without PATH, uses snapshots/<basename>_exit_<cycle>.snap."
        ),
    )
    ap.add_argument(
        "--load-snapshot",
        metavar="PATH",
        default=None,
        help=(
            "Load a snapshot file at startup and resume from that state. "
            "Skips BASIC boot, PRG/disk autoloading, and the reset vector. "
            "ROMs are still required and loaded from --rom-dir as usual."
        ),
    )
    ap.add_argument("--turbo", action="store_true", default=cfg_debug.get("turbo", False), help="Run at maximum speed (no speed limiting)")
    ap.add_argument("--benchmark", action="store_true", help="Run benchmark (implies --turbo --autoquit --no-colors)")
    ap.add_argument("--vice-trace", type=str, metavar="FILE", help="Write VICE-compatible CPU trace to FILE for comparison debugging")
    ap.add_argument(
        "--vice-trace-wall",
        action="store_true",
        help="With --vice-trace: record host seconds since previous line (monotonic) for profiling between instructions",
    )
    ap.add_argument(
        "--vice-trace-inline-wall",
        action="store_true",
        help="With --vice-trace: same as wall time but append ' ; w <sec>' on the instruction line instead of a separate '; w' line",
    )
    ap.add_argument(
        "--vic-emulation",
        choices=("fast", "accurate-python", "accurate-rust"),
        default=cfg_emulation.get("vic_emulation", "fast"),
        help=(
            "VIC timing: fast (coarse raster); accurate-python (per-cycle Python VIC+BA stalls); "
            "accurate-rust (PAL hybrid VIC in optional Rust core when built — default). "
            "NTSC accurate-rust falls back to Python accurate path."
        ),
    )
    ap.add_argument(
        "--accurate-vic",
        action="store_true",
        help="Deprecated: same as --vic-emulation accurate-python (pure Python cycle VIC).",
    )
    ap.add_argument(
        "--debug-inject-at-cycle",
        type=int,
        default=None,
        metavar="N",
        help=(
            "One-shot experiment: on the first instruction fetch with cumulative CPU cycles >= N, "
            "poke addresses from --debug-inject-map (then continue normally). Printed on stderr."
        ),
    )
    ap.add_argument(
        "--debug-inject-map",
        type=str,
        default=None,
        metavar="MAP",
        help=(
            "With --debug-inject-at-cycle: comma-separated pairs. RAM: hex addr=value (e.g. 2f=53,30=e7). "
            "CPU regs: a=, x=, y=, p= (e.g. a=d6,x=da to match a VICE snapshot). "
            "Optional if --debug-inject-file is set."
        ),
    )
    ap.add_argument(
        "--debug-inject-file",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "With --debug-inject-at-cycle: text file of addr=value lines (hex), # comments OK. "
            "Useful for stack page from VICE (m 0100 01ff). Applied before --debug-inject-map (map overrides)."
        ),
    )

    args = ap.parse_args()
    interface_mode = str(args.interface).strip().lower()
    if interface_mode in ("text", "tui"):
        interface_mode = "textual"
    elif interface_mode == "pygame":
        interface_mode = "graphics"
    args.interface = interface_mode

    # Backward-compatible aliases. Old names remain valid CLI inputs; we
    # normalize to the canonical new names so downstream code only sees
    # `per-frame` / `per-raster`.
    _VIDEO_RENDERING_ALIASES = {"fast": "per-frame", "accurate": "per-raster"}
    if args.video_rendering in _VIDEO_RENDERING_ALIASES:
        args.video_rendering = _VIDEO_RENDERING_ALIASES[args.video_rendering]

    # Master "max accuracy" flag — per-cycle video keeps Python VIC stepping (see below).
    if args.accurate:
        if args.video_rendering != "per-cycle":
            args.video_rendering = "per-raster"
            args.vic_emulation = "accurate-rust"
        else:
            args.vic_emulation = "accurate-python"
            print(
                "Note: --accurate with --video-rendering per-cycle uses accurate-python VIC "
                "(per-cycle sampling requires the Python cycle engine).",
                file=sys.stderr,
            )

    if args.accurate_vic:
        if args.vic_emulation != "accurate-python":
            print(
                "WARNING: --accurate-vic is deprecated; using --vic-emulation accurate-python "
                f"(was {args.vic_emulation!r}).",
                file=sys.stderr,
            )
        vic_emulation = "accurate-python"
    else:
        vic_emulation = args.vic_emulation

    if args.video_rendering == "per-cycle" and vic_emulation != "accurate-python":
        print(
            f"Note: --video-rendering per-cycle requires accurate-python VIC for sampling "
            f"(was {vic_emulation!r}); switching to accurate-python.",
            file=sys.stderr,
        )
        vic_emulation = "accurate-python"

    has_inject_src = bool(args.debug_inject_map or args.debug_inject_file)
    if args.debug_inject_at_cycle is not None and not has_inject_src:
        print(
            "ERROR: --debug-inject-map and/or --debug-inject-file is required with --debug-inject-at-cycle",
            file=sys.stderr,
        )
        sys.exit(1)
    if has_inject_src and args.debug_inject_at_cycle is None:
        print(
            "ERROR: --debug-inject-at-cycle is required with --debug-inject-map / --debug-inject-file",
            file=sys.stderr,
        )
        sys.exit(1)

    # --max-cycles implies autoquit (unless explicitly disabled with --no-autoquit)
    if args.max_cycles is not None and not args.no_autoquit:
        args_autoquit = True
    else:
        args_autoquit = not args.no_autoquit

    # --benchmark implies other flags and loads benchmark PRG
    if args.benchmark:
        args.turbo = True
        args_autoquit = True
        args.no_colors = True
        if args.interface != "graphics":
            args.interface = "headless"
        if args.max_cycles is None:
            args.max_cycles = 15_000_000  # Enough cycles for benchmark to complete
        # Auto-load benchmark PRG if no file specified
        if args.media_file is None:
            benchmark_prg = os.path.join(script_dir, "programs", "benchmark.prg")
            if os.path.exists(benchmark_prg):
                args.media_file = benchmark_prg
            else:
                print(f"Warning: Benchmark PRG not found at {benchmark_prg}")
                print("Run: ./tools/compile.sh to build it (needs VICE petcat).")

    temp_prg_cleanup: Optional[str] = None
    prg_path: Optional[str] = None
    disk_path: Optional[str] = None
    if args.media_file:
        prg_path, disk_path, temp_prg_cleanup = _resolve_media_cli_arg(args.media_file)
        if temp_prg_cleanup:
            atexit.register(lambda p=temp_prg_cleanup: _unlink_if_exists(p))

    prg_display_basename = os.path.basename(args.media_file) if args.media_file else None

    # Track start time for speed calculation
    start_time = time.perf_counter()

    interface_factory = None
    if args.interface == "headless":
        interface_factory = lambda _emu: None
    if args.interface == "graphics":
        try:
            from .graphics import PygameInterface
        except ImportError:
            from c64py.graphics import PygameInterface
        # Resolve [input.joystick] from the loaded TOML (None if absent).
        cfg_input = cfg.get("input", {}) if isinstance(cfg, dict) else {}
        joystick_cfg = (
            cfg_input.get("joystick")
            if isinstance(cfg_input, dict) and isinstance(cfg_input.get("joystick"), dict)
            else None
        )
        interface_factory = functools.partial(
            PygameInterface,
            max_cycles=args.max_cycles,
            scale=args.graphics_scale,
            fps=args.graphics_fps,
            border_size=args.graphics_border,
            joystick_config=joystick_cfg,
        )

    # Audio is pointless in headless mode and pygame.mixer.init() can hang
    # on machines without an audio device; skip it entirely.
    audio_mode = str(args.audio_emulation).strip().lower()
    enable_resid = (audio_mode == "resid") and (args.interface != "headless")
    enable_sid = (audio_mode == "python-sid") and (args.interface != "headless")
    
    # Handle audio volume
    audio_volume = 0.0 if args.audio_muted else max(0.0, min(1.0, args.audio_volume))
    
    emu = C64(
        interface_factory=interface_factory,
        enable_sid=enable_sid,
        enable_resid=enable_resid,
        audio_volume=audio_volume,
        vic_emulation=vic_emulation,
        disk_emulation=drive_tier,
    )
    # Expose merged host config to UI layers (graphics input/gamepad, tools).
    emu.host_config = cfg
    # Pygame needs latched VIC regs for rendering; headless skips copies for throughput.
    emu.memory.vic_render_snapshots = bool(args.interface == "graphics")
    # Fast VIC: latch when pygame presents (~Hz), not every emulated PAL frame (turbo regression).
    # Accurate VIC: keep CPU-thread snapshot at each emulated frame for cycle-stable sampling.
    emu.memory.vic_snapshot_each_emulated_frame = bool(args.interface == "graphics") and bool(emu.accurate_vic)
    if args.debug_inject_at_cycle is not None:
        inject_pairs: list[tuple[int | str, int]] = []
        if args.debug_inject_file:
            inject_pairs.extend(_parse_debug_inject_file(args.debug_inject_file))
        if args.debug_inject_map:
            inject_pairs.extend(_parse_debug_inject_map_string(args.debug_inject_map))
        if not inject_pairs:
            print("ERROR: no inject entries after parsing file/map", file=sys.stderr)
            sys.exit(1)
        emu.cpu.debug_inject_at_cycle = args.debug_inject_at_cycle
        emu.cpu.debug_inject_writes = inject_pairs
    emu.debug = args.debug
    emu.autoquit = args_autoquit
    emu.turbo = args.turbo
    emu.screen_update_interval = args.screen_update_interval
    emu.no_colors = args.no_colors
    emu.inject_key_rules = []
    _inject_entries = args.inject_keys or []
    if _inject_entries:
        try:
            from .keyboard_inject import parse_inject_key_entries
        except ImportError:
            from c64py.keyboard_inject import parse_inject_key_entries
        try:
            emu.inject_key_rules = parse_inject_key_entries(_inject_entries)
        except ValueError as exc:
            print(f"ERROR: --inject-keys: {exc}", file=sys.stderr)
            # os._exit: pygame/reSID are already initialised; sys.exit() raises
            # SystemExit which SDL atexit hooks can intercept, causing a hang.
            # os._exit() bypasses all cleanup and terminates the process immediately.
            os._exit(1)
    if args.debug:
        emu.cpu.enable_trace(1024)
    supports_ui_logs = (emu.interface is not None) and hasattr(emu.interface, "fullscreen")
    if supports_ui_logs:
        emu.interface.fullscreen = args.fullscreen
    show_ui_logs = (not args.fullscreen) if supports_ui_logs else False

    vice_trace = None
    try:
        print(
            f"VIC emulation: {vic_emulation}  |  video rendering: {args.video_rendering}  "
            f"|  drive tier (config): {drive_tier}"
        )
        _check_rust_core_available(
            vic_emulation,
            emu,
            show_ui_logs=show_ui_logs,
            no_colors=emu.no_colors,
        )
        if args.debug and show_ui_logs and emu.interface is not None:
            emu.interface.add_debug_log("🐛 Debug mode enabled")

        # Setup UDP debug logging if requested
        if args.udp_debug:
            emu.udp_debug = UdpDebugLogger(port=args.udp_debug_port, host=args.udp_debug_host)
            emu.udp_debug.enable()
            if show_ui_logs and emu.interface is not None:
                emu.interface.add_debug_log(f"📡 UDP debug logging enabled: {args.udp_debug_host}:{args.udp_debug_port}")
            # Test UDP connection
            try:
                test_msg = {'type': 'test', 'message': 'UDP debug initialized'}
                emu.udp_debug.send('test', test_msg)
                if show_ui_logs and emu.interface is not None:
                    emu.interface.add_debug_log("✅ UDP test message sent successfully")
            except Exception as e:
                if show_ui_logs and emu.interface is not None:
                    emu.interface.add_debug_log(f"❌ UDP test failed: {e}")

        # Pass UDP debug logger to memory
        if emu.udp_debug:
            emu.memory.udp_debug = emu.udp_debug

        # Setup VICE-compatible trace logging if requested
        if args.vice_trace:
            vice_trace = ViceTraceLogger(
                filename=args.vice_trace,
                wall_time=args.vice_trace_wall or args.vice_trace_inline_wall,
                wall_inline=args.vice_trace_inline_wall,
            )
            vice_trace.enable()
            emu.vice_trace = vice_trace
            if show_ui_logs and emu.interface is not None:
                emu.interface.add_debug_log(f"📝 VICE trace logging to: {args.vice_trace}")

        # Video standard (memory + SID/reSID clock when audio is enabled)
        emu.set_video_standard(args.video_standard)
        if show_ui_logs and emu.interface is not None:
            emu.interface.add_debug_log(f"📺 Video standard: {args.video_standard.upper()}")

        # Load ROMs (auto-detect common locations if not provided).
        # Import ROM helper with support for both package and script execution.
        try:
            from .roms import ensure_roms_available
        except ImportError:
            from c64py.roms import ensure_roms_available

        try:
            explicit_rom_dir = args.rom_dir
            if explicit_rom_dir and not os.path.isabs(explicit_rom_dir):
                # Relative to the directory containing this script (repo root when C64.py is there).
                explicit_rom_dir = os.path.normpath(os.path.join(script_dir, explicit_rom_dir))

            print("[INIT] Searching for ROMs...", flush=True)
            rom_dir_path = ensure_roms_available(
                explicit_rom_dir,
                allow_prompt=True,
                require_char_rom=args.interface == "graphics",
            )
            emu.load_roms(str(rom_dir_path), require_char_rom=args.interface == "graphics")
            if show_ui_logs and emu.interface is not None:
                emu.interface.add_debug_log(f"💾 ROM directory: {rom_dir_path}")

            # All disk tiers now run the real 1541 6502 + DOS ROM. The tiers
            # differ in *how* the disk surface and IEC bus are emulated:
            #
            #   fast            → job-queue trap (zero-page) + KERNAL LOAD
            #                     shortcut at $FFD5. Disk I/O is essentially
            #                     free; the drive CPU runs but isn't stressed
            #                     by GCR / serial bit-banging.
            #   accurate-python → real GCR head + bit-level IEC, no shortcuts
            #                     (in active development; until M2 lands this
            #                     tier currently behaves like `fast` and
            #                     warns).
            #   accurate-rust   → same as accurate-python but in the Rust core
            #                     (M3; falls back to accurate-python today).
            # Drive tier comes from [emulation] disk_emulation (TOML); the
            # standalone c1541_emulator uses --emulation with the same names.
            # All tiers run the real 1541 6502 + DOS ROM. The job-queue trap
            # is currently always enabled (until M2b ships a real GCR head).
            # The KERNAL `$FFD5`/`$FFD8` shortcut is the dominant difference:
            # `fast` keeps it on (instant LOAD); `accurate-*` turns it off so
            # the real KERNAL serial routines bit-bang the IEC bus and DOS
            # ships file bytes back over the wire — exercising the bit-level
            # handshake end-to-end.
            emu._iec_disk_full_impl = True
            # The KERNAL shortcut ($FFD5/$FFD8 hook → TCP RPC) must stay ON
            # whenever --tcp-drive is used: the TCP drive has no IEC bus wired
            # up, so disabling the shortcut would hang KERNAL serial routines.
            # accurate-* tiers disable the shortcut only for the local IEC path
            # (attach_disk without --tcp-drive, M2 work in progress).
            # With --tcp-drive, kernal_load_shortcut_enabled stays ON by design (see
            # below); no stderr warning — mixing TOML accurate-* with TCP is normal.
            # KERNAL $FFD5/$FFD8 shortcut: set after IEC init + optional auto-spawn
            # (see below once ``iec_drives`` is populated).
            # Parse --tcp-drive DEVICE:HOST:PORT entries into a dict.
            tcp_drives: dict[int, str] = {}
            for entry in args.tcp_drive:
                parts = entry.split(":", 1)
                if len(parts) != 2:
                    print(
                        f"ERROR: --tcp-drive must be DEVICE:HOST:PORT, got: {entry!r}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                try:
                    tcp_drives[int(parts[0])] = parts[1]
                except ValueError:
                    print(
                        f"ERROR: --tcp-drive device must be an integer, got: {entry!r}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            emu.initialize_iec_bus(tcp_drives=tcp_drives or None)

            # Auto-spawn a headless drive for args.disk only when the user
            # did NOT pass any --tcp-drive (so we don't double-attach).
            if disk_path and not tcp_drives:
                dos_rom = None
                _dos_candidate = rom_dir_path / "dos1541"
                if _dos_candidate.exists():
                    dos_rom = str(_dos_candidate)
                try:
                    emu._spawn_local_drive(
                        disk_path, device=8,
                        tier=drive_tier,
                        dos_rom_path=dos_rom,
                    )
                    # Signal the post-boot code to inject LOAD"$",8 without
                    # going through the old local attach_disk path.
                    emu._auto_spawned_drive = True
                    if show_ui_logs and emu.interface is not None:
                        emu.interface.add_debug_log(
                            f"💾 Auto-spawned drive 8 (headless, tier={drive_tier})"
                        )
                except Exception as exc:
                    print(f"ERROR: could not auto-spawn drive: {exc}", file=sys.stderr)
                    sys.exit(1)

            def _any_tcp_drive_client() -> bool:
                from c64py.drives.tcp_drive_client import TcpDriveClient

                return any(
                    isinstance(x, TcpDriveClient) for x in emu.iec_drives.values()
                )

            # Keep the KERNAL LOAD/SAVE shortcut ON whenever there is no drive to
            # answer bit-level IEC (avoids infinite KERNAL wait → "hang"), when
            # using `fast`, or when any attached drive is a TcpDriveClient
            # (including auto-spawned headless servers) — TCP path has no host IEC
            # wiring yet. Only disable for a future local non-TCP accurate path.
            emu.kernal_load_shortcut_enabled = (
                drive_tier == "fast"
                or _any_tcp_drive_client()
                or not emu.iec_drives
            )

            if drive_tier == "accurate-python" and not emu.kernal_load_shortcut_enabled:
                msg = (
                    "Note: [emulation] disk_emulation accurate-python disables the KERNAL "
                    "LOAD shortcut; LOAD goes through the real 1541 DOS over IEC. "
                    "Expect noticeably slower disk I/O than `fast` until the GCR "
                    "head + Rust drive port land."
                )
                print(msg, file=sys.stderr)
                if show_ui_logs and emu.interface is not None:
                    emu.interface.add_debug_log(f"ℹ {msg}")
            if show_ui_logs and emu.interface is not None:
                labels = {
                    "fast": "fast (real 1541 DOS ROM + job-queue trap + KERNAL LOAD shortcut)",
                    "accurate-python": "accurate-python (real KERNAL ↔ DOS over IEC, job-queue trap for sectors)",
                    "accurate-rust": "accurate-rust (drive port WIP → falls back to accurate-python)",
                }
                emu.interface.add_debug_log(
                    f"📀 Drive tier: {labels.get(drive_tier, drive_tier)}"
                )

            if args.video_rendering == "per-raster":
                emu.memory.beam_render_enabled = True
                emu.memory.per_cycle_render_enabled = False
                emu.memory.ensure_beam_buffers()
                emu.memory.prime_beam_snapshots_from_current_vic()
            elif args.video_rendering == "per-cycle":
                emu.memory.beam_render_enabled = False
                emu.memory.per_cycle_render_enabled = True
                emu.memory.ensure_per_cycle_buffers()
                emu.memory.prime_per_cycle_snapshots_from_current_vic()
            else:
                emu.memory.beam_render_enabled = False
                emu.memory.per_cycle_render_enabled = False
        except Exception as e:
            # Ensure UI is not left running, then show a clear error.
            try:
                if hasattr(emu, "interface") and hasattr(emu.interface, "exit"):
                    emu.interface.exit()
            except Exception:
                pass
            print(f"ERROR: {e}")
            sys.exit(1)

        # Store PRG file path for loading after boot (BASIC boot clears $0801-$0802)
        if prg_path:
            emu.prg_file_path = prg_path
            if show_ui_logs and emu.interface is not None:
                emu.interface.add_debug_log(f"📂 PRG file will be loaded after BASIC boot: {prg_path}")

        # Store D64 disk image path for attaching after boot.
        # When a drive was auto-spawned the disk is already in the subprocess;
        # skip the local attach_disk path and just inject LOAD"$",8 after boot.
        if disk_path and not getattr(emu, "_auto_spawned_drive", False):
            emu.disk_image_path = disk_path
            if show_ui_logs and emu.interface is not None:
                emu.interface.add_debug_log(f"💾 D64 disk will be attached after BASIC boot: {disk_path}")
        elif disk_path and getattr(emu, "_auto_spawned_drive", False):
            # Drive is remote; inject directory load after BASIC boot.
            emu._auto_spawned_drive_device = 8

        # Initialize CPU (use _read_word to ensure correct byte order and ROM mapping)
        reset_vector = emu.cpu._read_word(0xFFFC)
        emu.cpu.state.pc = reset_vector
        if show_ui_logs and emu.interface is not None:
            emu.interface.add_debug_log(f"🔄 Reset vector: ${reset_vector:04X}")

        # --load-snapshot: replace full state (after reset vector so ROMs still
        # present; snapshot overrides CPU.PC, RAM, VIC, CIAs, cycles, etc.).
        if getattr(args, "load_snapshot", None):
            try:
                emu.load_snapshot(args.load_snapshot)
            except Exception as exc:
                print(f"ERROR: failed to load snapshot {args.load_snapshot}: {exc}")
                sys.exit(1)
            # Suppress autoload paths: snapshot already carries the loaded game.
            emu.prg_file_path = None
            emu.disk_image_path = None
            emu._program_loaded_after_boot = True
            emu._disk_attached_after_boot = True
            if show_ui_logs and emu.interface is not None:
                emu.interface.add_debug_log(
                    f"📥 Snapshot loaded from {args.load_snapshot} — skipping BASIC boot/autoload"
                )

        # --save-snapshot-at-cycle CYCLE[:PATH] / --save-snapshot-at-exit [PATH].
        _configure_snapshot_saving(args, emu, prg_display_basename)

        if args.debug and show_ui_logs and emu.interface is not None:
            emu.interface.add_debug_log(
                f"🖥️ Initial CPU state: PC=${emu.cpu.state.pc:04X}, A=${emu.cpu.state.a:02X}, "
                f"X=${emu.cpu.state.x:02X}, Y=${emu.cpu.state.y:02X}"
            )
            emu.interface.add_debug_log(f"💾 Memory config ($01): ${emu.memory.ram[0x01]:02X}")
            emu.interface.add_debug_log(
                f"📺 Screen memory sample ($0400-$040F): {[hex(emu.memory.ram[0x0400 + i]) for i in range(16)]}"
            )

        if args.monitor_port is not None:
            try:
                from .monitor_tcp import C64MonitorTcpServer
            except ImportError:
                from c64py.monitor_tcp import C64MonitorTcpServer

            emu.monitor_server = C64MonitorTcpServer(emu, int(args.monitor_port))
            emu.monitor_server.start()
            print(f"Monitor TCP on 127.0.0.1:{int(args.monitor_port)} (see docs/DEBUGGING.md)")

        # Start server if requested (runs in parallel with UI)
        server = None
        if args.tcp_port or args.udp_port:
            server = EmulatorServer(emu, tcp_port=args.tcp_port, udp_port=args.udp_port)
            server.start()
            if show_ui_logs and emu.interface is not None:
                emu.interface.add_debug_log("📡 TCP/UDP server started")
                emu.interface.add_debug_log("📡 Server commands: STATUS, STEP, RUN, MEMORY, DUMP, SCREEN, LOAD")
            print("Server started on port(s): ", end="")
            if args.tcp_port:
                print(f"TCP:{args.tcp_port}", end="")
            if args.tcp_port and args.udp_port:
                print(", ", end="")
            if args.udp_port:
                print(f"UDP:{args.udp_port}", end="")
            print()

        # Start graphics interface if requested
        if args.interface == "graphics" and emu.interface is not None:
            emu.interface.max_cycles = args.max_cycles
            if show_ui_logs and emu.interface is not None:
                emu.interface.add_debug_log("🎨 Graphics interface active")
            try:
                emu.interface.run()
            finally:
                if hasattr(emu.interface, "_get_last_log_lines"):
                    last_lines = emu.interface._get_last_log_lines(20)
                    if last_lines:
                        print("\n=== Last log messages ===")
                        for line in last_lines:
                            print(line)
            if server:
                server.running = False
            # Show emulation speed
            _show_speed(emu, emu.current_cycles, wall_start_fallback=start_time, target_hz=emu.target_cpu_hz)
            if args.benchmark:
                _print_benchmark_record(
                    args,
                    emu,
                    wall_start_fallback=start_time,
                    prg_display_basename=prg_display_basename,
                )
            return

        # Start Textual interface (unless explicitly disabled with --no-colors)
        if (not args.no_colors) and (emu.interface is not None):
            emu.interface.max_cycles = args.max_cycles
            # fullscreen flag already set earlier
            if show_ui_logs:
                emu.interface.add_debug_log("🚀 C64 Emulator started")
                emu.interface.add_debug_log("🎨 Textual interface with TCSS active")
            try:
                emu.interface.run()  # This will block and run the Textual app
            finally:
                # Capture and print last log lines after UI shuts down
                if hasattr(emu.interface, '_get_last_log_lines'):
                    last_lines = emu.interface._get_last_log_lines(20)
                    if last_lines:
                        print("\n=== Last log messages ===")
                        for line in last_lines:
                            print(line)
            # After UI closes, stop server if running
            if server:
                server.running = False
            # Show emulation speed
            _show_speed(emu, emu.current_cycles, wall_start_fallback=start_time, target_hz=emu.target_cpu_hz)
            if args.benchmark:
                _print_benchmark_record(
                    args,
                    emu,
                    wall_start_fallback=start_time,
                    prg_display_basename=prg_display_basename,
                )
            return  # Exit after Textual interface closes

        # This code should never be reached since Textual blocks
        # But if --no-colors is used, we fall through here
        try:
            print("Running emulator...")
            emu.run(args.max_cycles)
        except KeyboardInterrupt:
            print("\nStopping emulator...")
            emu.running = False
            if emu.screen_update_thread and emu.screen_update_thread.is_alive():
                emu.screen_update_thread.join(timeout=1.0)

        if args.debug:
            chrout_count = getattr(emu.cpu, "chrout_count", None)
            if chrout_count is not None:
                print(f"DEBUG: CHROUT calls: {chrout_count}")
            cursor_row = emu.memory.read(CURSOR_ROW_ADDR)
            cursor_col = emu.memory.read(CURSOR_COL_ADDR)
            blnsw = emu.memory.read(BLNSW)
            blnct = emu.memory.read(BLNCT)
            print(f"DEBUG: Cursor row={cursor_row} col={cursor_col} BLNSW=${blnsw:02X} BLNCT=${blnct:02X}")
            first_line = [emu.memory.read(SCREEN_MEM + i) for i in range(40)]
            hex_line = " ".join(f"{code:02X}" for code in first_line)
            ascii_line = "".join(chr(code) if 0x20 <= code <= 0x7E else "." for code in first_line)
            print(f"DEBUG: Screen row0 hex: {hex_line}")
            print(f"DEBUG: Screen row0 ascii: {ascii_line}")
            vic_d018 = emu.memory.peek_vic(0x18) & 0xFF
            screen_base = ((vic_d018 >> 4) & 0x0F) * 0x0400
            non_space = 0
            for i in range(1000):
                if emu.memory.read(screen_base + i) != 0x20:
                    non_space += 1
            print(f"DEBUG: VIC $D018=${vic_d018:02X} screen_base=${screen_base:04X} non_space={non_space}")
            if screen_base != SCREEN_MEM:
                base_line = [emu.memory.read(screen_base + i) for i in range(40)]
                base_hex = " ".join(f"{code:02X}" for code in base_line)
                base_ascii = "".join(chr(code) if 0x20 <= code <= 0x7E else "." for code in base_line)
                print(f"DEBUG: Screen base row0 hex: {base_hex}")
                print(f"DEBUG: Screen base row0 ascii: {base_ascii}")
            kb_len = emu.memory.read(KEYBOARD_BUFFER_LEN_ADDR)
            kb_codes = [emu.memory.read(KEYBOARD_BUFFER_BASE + i) for i in range(kb_len)]
            kb_hex = " ".join(f"{code:02X}" for code in kb_codes)
            kb_ascii = "".join(chr(code) if 0x20 <= code <= 0x7E else "." for code in kb_codes)
            print(f"DEBUG: Keyboard buffer len={kb_len} hex: {kb_hex}")
            print(f"DEBUG: Keyboard buffer ascii: {kb_ascii}")
            input_idx = emu.memory.read(INPUT_BUFFER_INDEX_ADDR)
            input_len = emu.memory.read(INPUT_BUFFER_LEN_ADDR)
            print(f"DEBUG: Input buffer idx={input_idx} len={input_len}")
            trace_entries = emu.cpu.get_trace()
            if trace_entries:
                print(f"DEBUG: Last {len(trace_entries)} instructions:")
                for entry in trace_entries:
                    print(
                        "DEBUG: "
                        f"CYC={entry['cycles']} PC=${entry['pc']:04X} OP=${entry['opcode']:02X} "
                        f"OP1=${entry['op1']:02X} OP2=${entry['op2']:02X} "
                        f"A=${entry['a']:02X} X=${entry['x']:02X} Y=${entry['y']:02X} "
                        f"SP=${entry['sp']:02X} P=${entry['p']:02X}"
                    )

        if args.dump_hex_range:
            rng = args.dump_hex_range.replace(" ", "").lower()
            if "-" not in rng:
                print("ERROR: --dump-hex-range wants START-END (hex), e.g. C200-C2FF", file=sys.stderr)
                sys.exit(1)
            lo_s, hi_s = rng.split("-", 1)
            lo, hi = int(lo_s, 16) & 0xFFFF, int(hi_s, 16) & 0xFFFF
            if lo > hi:
                lo, hi = hi, lo
            blob = bytes(emu.memory.ram[lo : hi + 1])
            digest = hashlib.sha256(blob).hexdigest()
            print(
                f"\n=== dump-hex-range ${lo:04X}-${hi:04X} "
                f"cpu_cycles={emu.current_cycles} sha256={digest} ==="
            )
            for base in range(lo, hi + 1, 16):
                chunk_end = min(base + 16, hi + 1)
                chunk = emu.memory.ram[base:chunk_end]
                hexb = " ".join(f"{b:02X}" for b in chunk)
                print(f"${base:04X}: {hexb}")

        if args.dump_ram_sha256:
            ram64 = bytes(emu.memory.ram[0:0x10000])
            digest = hashlib.sha256(ram64).hexdigest()
            print(
                f"=== ram-sha256-full cpu_cycles={emu.current_cycles} "
                f"sha256={digest} ==="
            )

        if args.dump_cpu_state:
            st = emu.cpu.state
            ec = int(emu.current_cycles)
            sc = int(st.cycles)
            print(
                "=== cpu-state "
                f"emulated_cycles={ec} cpu_state_cycles={sc} "
                f"PC=${st.pc & 0xFFFF:04X} "
                f"A=${st.a & 0xFF:02X} X=${st.x & 0xFF:02X} Y=${st.y & 0xFF:02X} "
                f"SP=${st.sp & 0xFF:02X} P=${st.p & 0xFF:02X} ==="
            )

        if args.dump_ram_raw:
            raw_path = Path(args.dump_ram_raw)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            with open(raw_path, "wb") as rf:
                rf.write(bytes(emu.memory.ram[0:0x10000]))
            print(f"Raw 65536-byte RAM written to {raw_path.resolve()}")

        # Dump memory if requested
        if args.dump_memory:
            memory_dump = emu.dump_memory()
            with open(args.dump_memory, 'wb') as f:
                f.write(bytes([0x00, 0x00]))  # PRG header
                f.write(memory_dump)
            print(f"Memory dumped to {args.dump_memory}")

        # Show final screen (only if Rich was not used). Skip in headless + RAM snapshot mode so
        # stdout stays text-safe for grep/pipes (PETSCII/control chars confuse grep -a on some setups).
        skip_final_text = (args.interface == "headless") and (
            args.dump_ram_sha256 or args.dump_cpu_state or bool(args.dump_ram_raw)
        )
        if not server or not server.running:
            if args.no_colors and not skip_final_text:
                # Only show final screen if colors are disabled
                emu._update_text_screen()
                print("\nFinal Screen output:")
                print(emu.render_text_screen(no_colors=True))

        # Textual interface handles its own cleanup

        # Stop screen update thread
        emu.running = False
        if emu.screen_update_thread and emu.screen_update_thread.is_alive():
            emu.screen_update_thread.join(timeout=1.0)

        # Show emulation speed
        _show_speed(emu, emu.current_cycles, wall_start_fallback=start_time, target_hz=emu.target_cpu_hz)
        if args.benchmark:
            _print_benchmark_record(
                args,
                emu,
                wall_start_fallback=start_time,
                prg_display_basename=prg_display_basename,
            )

        # Close UDP debug logger (flush all pending messages)
        if emu.udp_debug:
            emu.udp_debug.close()
        
        # Close VICE trace logger
        if vice_trace:
            vice_trace.close()
            print(f"VICE trace written to: {args.vice_trace}")
    finally:
        emu.shutdown()



if __name__ == "__main__":
    main()
