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
        "enable_resid": bool(args.enable_resid),
        "enable_sid": bool(args.enable_sid),
        "max_cycles_arg": args.max_cycles,
        "prg": prg_display_basename,
        "schema": 1,
        "target_hz": emu.target_cpu_hz,
        "turbo": bool(args.turbo),
        "video_standard": args.video_standard,
        "wall_seconds": round(elapsed, 6),
    }
    print("C64PY_BENCHMARK " + json.dumps(rec, sort_keys=True))


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


def main():
    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))

    ap = argparse.ArgumentParser(description="C64 Emulator")
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
        "--disk-emulation",
        choices=("fast", "accurate"),
        default="fast",
        help=(
            "fast: KERNAL hooks + virtual DiskDrive (default). "
            "accurate: also initialize IEC bus + 1541 ROM drives when DOS ROM is found; "
            "interleaved drive stepping; Rust CIA2 IEC merge when IEC is active."
        ),
    )
    ap.add_argument(
        "--video-rendering",
        choices=("fast", "accurate"),
        default="fast",
        help=(
            "Pygame output sampling. fast: one VIC latch per presented frame (default). "
            "accurate: per-raster-line VIC + CIA2 bank for vertical splits when the Python "
            "CPU runs every instruction (e.g. --vic-emulation accurate-python, or no Rust core); "
            "with the Rust batch + default accurate-rust, output uses the same latch as fast. "
            "See docs/DEBUGGING.md."
        ),
    )
    ap.add_argument(
        "--accurate",
        action="store_true",
        help=(
            "Shortcut: set --disk-emulation accurate, --video-rendering accurate, "
            "and --vic-emulation accurate-rust."
        ),
    )
    ap.add_argument(
        "--monitor-port",
        type=int,
        default=None,
        metavar="PORT",
        help="TCP port for minimal debugger (REGS, STEP, M, BREAK); see docs/DEBUGGING.md",
    )
    ap.add_argument("--tcp-port", type=int, help="TCP port for control interface")
    ap.add_argument("--udp-port", type=int, help="UDP port for control interface")
    ap.add_argument("--max-cycles", type=int, default=None, help="Maximum cycles to run (default: unlimited)")
    ap.add_argument("--dump-memory", help="Dump memory to file after execution")
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
    ap.add_argument("--udp-debug", action="store_true", help="Send debug events via UDP")
    ap.add_argument("--autoquit", action="store_true", help="Automatically quit when max cycles is reached")
    ap.add_argument("--udp-debug-port", type=int, default=64738, help="UDP port for debug events (default: 64738)")
    ap.add_argument("--udp-debug-host", type=str, default="127.0.0.1", help="UDP host for debug events (default: 127.0.0.1)")
    ap.add_argument("--screen-update-interval", type=float, default=0.1, help="Screen update interval in seconds (default: 0.1)")
    ap.add_argument("--video-standard", choices=["pal", "ntsc"], default="pal", help="Video standard (pal or ntsc, default: pal)")
    ap.add_argument("--no-colors", action="store_true", help="Disable ANSI color output")
    ap.add_argument("--fullscreen", action="store_true", help="Show only C64 screen output (no debug panel or status bar)")
    ap.add_argument("--graphics", action="store_true", help="Render output in a pygame graphics window")
    ap.add_argument("--graphics-scale", type=int, default=2, help="Graphics window scale factor (default: 2)")
    ap.add_argument(
        "--graphics-fps",
        type=int,
        default=30,
        help="Graphics target FPS / max host present rate (default: 30)",
    )
    ap.add_argument("--graphics-border", type=int, default=None, help="Graphics border size in pixels (default: 32)")
    ap.add_argument("--enable-sid", action="store_true", help="Enable SID audio output via pygame")
    ap.add_argument(
        "--enable-resid",
        action="store_true",
        help=(
            "Enable high-accuracy SID audio via the VICE-Team reSID C++ library "
            "(requires resid_c.so – see src/resid_wrapper/README.md)"
        ),
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
    ap.add_argument("--turbo", action="store_true", help="Run at maximum speed (no speed limiting)")
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
    ap.add_argument("--headless", action="store_true", help="Run without UI (useful for trace automation)")
    ap.add_argument(
        "--vic-emulation",
        choices=("fast", "accurate-python", "accurate-rust"),
        default="accurate-rust",
        help=(
            "VIC timing: fast (coarse raster); accurate-python (per-cycle Python VIC+BA stalls); "
            "accurate-rust (PAL/NTSC hybrid VIC in optional Rust core when built — default; "
            "requires the extension)."
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

    if args.accurate:
        args.disk_emulation = "accurate"
        args.video_rendering = "accurate"
        args.vic_emulation = "accurate-rust"

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

    # --benchmark implies other flags and loads benchmark PRG
    if args.benchmark:
        args.turbo = True
        args.autoquit = True
        args.no_colors = True
        if not args.graphics:
            args.headless = True
        if args.max_cycles is None:
            args.max_cycles = 15_000_000  # Enough cycles for benchmark to complete
        # Auto-load benchmark PRG if no file specified
        if args.media_file is None:
            benchmark_prg = os.path.join(script_dir, "programs", "benchmark.prg")
            if os.path.exists(benchmark_prg):
                args.media_file = benchmark_prg
            else:
                print(f"Warning: Benchmark PRG not found at {benchmark_prg}")
                print("Run: ./compile.sh to build it (needs VICE petcat).")

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
    if args.headless:
        interface_factory = lambda _emu: None
    if args.graphics:
        try:
            from .graphics import PygameInterface
        except ImportError:
            from c64py.graphics import PygameInterface
        interface_factory = functools.partial(
            PygameInterface,
            max_cycles=args.max_cycles,
            scale=args.graphics_scale,
            fps=args.graphics_fps,
            border_size=args.graphics_border,
        )

    emu = C64(
        interface_factory=interface_factory,
        enable_sid=args.enable_sid,
        enable_resid=args.enable_resid,
        vic_emulation=vic_emulation,
        disk_emulation=args.disk_emulation,
    )
    # Pygame needs latched VIC regs for rendering; headless skips copies for throughput.
    emu.memory.vic_render_snapshots = bool(args.graphics)
    # Fast VIC: latch when pygame presents (~Hz), not every emulated PAL frame (turbo regression).
    # Accurate VIC: keep CPU-thread snapshot at each emulated frame for cycle-stable sampling.
    emu.memory.vic_snapshot_each_emulated_frame = bool(args.graphics) and bool(emu.accurate_vic)
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
    emu.autoquit = args.autoquit
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
            sys.exit(1)
    print(
        f"VIC emulation: {vic_emulation}  |  video rendering: {args.video_rendering}  "
        f"|  disk emulation: {emu.disk_emulation}"
    )
    if args.debug:
        emu.cpu.enable_trace(1024)
    supports_ui_logs = (emu.interface is not None) and hasattr(emu.interface, "fullscreen")
    if supports_ui_logs:
        emu.interface.fullscreen = args.fullscreen
    show_ui_logs = (not args.fullscreen) if supports_ui_logs else False
    _warn_if_rust_fast_core_unavailable(
        vic_emulation,
        emu,
        show_ui_logs=show_ui_logs,
        no_colors=args.no_colors,
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
    vice_trace = None
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

    try:
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

            rom_dir_path = ensure_roms_available(
                explicit_rom_dir,
                allow_prompt=True,
                require_char_rom=args.graphics,
            )
            emu.load_roms(str(rom_dir_path), require_char_rom=args.graphics)
            if show_ui_logs and emu.interface is not None:
                emu.interface.add_debug_log(f"💾 ROM directory: {rom_dir_path}")

            if args.disk_emulation == "accurate":
                iec_ok = emu.initialize_iec_bus(str(rom_dir_path))
                if not iec_ok:
                    print(
                        "ERROR: --disk-emulation accurate needs a 1541 DOS ROM in the ROM path "
                        "(e.g. dos1541, d1541-325302-01.bin, or VICE DRIVES/ "
                        "dos1541-325302-01+901229-05.bin — usually 16 KiB). "
                        "Optional serial ROM: d1541II / 901229-05.bin (see roms.py, IEC_BUS_STATUS.md).",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                if disk_path:
                    w = (
                        "WARNING: Accurate disk emulation (IEC) is planned but not yet functional; "
                        "LOAD from disk will show a stub error instead of hanging."
                    )
                    print(w, file=sys.stderr)
                    if show_ui_logs and emu.interface is not None:
                        emu.interface.add_debug_log(f"⚠ {w}")
                if show_ui_logs and emu.interface is not None:
                    emu.interface.add_debug_log("📀 Disk emulation: accurate (IEC bus active)")
            elif show_ui_logs and emu.interface is not None:
                emu.interface.add_debug_log("📀 Disk emulation: fast (KERNAL hooks)")

            if args.video_rendering == "accurate":
                emu.memory.beam_render_enabled = True
                emu.memory.ensure_beam_buffers()
                emu.memory.prime_beam_snapshots_from_current_vic()
            else:
                emu.memory.beam_render_enabled = False
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

        # Store D64 disk image path for attaching after boot
        if disk_path:
            emu.disk_image_path = disk_path
            if show_ui_logs and emu.interface is not None:
                emu.interface.add_debug_log(f"💾 D64 disk will be attached after BASIC boot: {disk_path}")

        # Initialize CPU (use _read_word to ensure correct byte order and ROM mapping)
        reset_vector = emu.cpu._read_word(0xFFFC)
        emu.cpu.state.pc = reset_vector
        if show_ui_logs and emu.interface is not None:
            emu.interface.add_debug_log(f"🔄 Reset vector: ${reset_vector:04X}")

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
        if args.graphics and emu.interface is not None:
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

        # Dump memory if requested
        if args.dump_memory:
            memory_dump = emu.dump_memory()
            with open(args.dump_memory, 'wb') as f:
                f.write(bytes([0x00, 0x00]))  # PRG header
                f.write(memory_dump)
            print(f"Memory dumped to {args.dump_memory}")

        # Show final screen (only if Rich was not used)
        if not server or not server.running:
            if args.no_colors:
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
