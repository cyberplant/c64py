"""Render one frame from a loaded snapshot and save as PNG.

Loads ``<snapshot>``, steps the emulator for ``--cycles`` cycles so the beam
buffers fill with real per-raster samples, then invokes the beam or latched
renderer directly (no event loop) and writes the result to ``<out>``.

Usage:
    python scripts/snapshot_render_test.py snapshots/x.snap \
        --cycles 40000 --mode beam --out out/snapshot_beam.png

Meant as a quick visual check for the per-raster text/bitmap dispatch.
"""

from __future__ import annotations

import argparse
import os
import sys


def _bootstrap() -> str:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parent = os.path.dirname(repo_root)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    return os.path.basename(repo_root)


_PKG = _bootstrap()
from importlib import import_module

_C64 = import_module(f"{_PKG}.emulator").C64
_load_snapshot = import_module(f"{_PKG}.snapshot").load_snapshot


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("snapshot", help="Path to .snap file")
    ap.add_argument("--cycles", type=int, default=40000, help="Cycles to run before rendering")
    ap.add_argument("--mode", choices=["beam", "latched"], default="beam")
    ap.add_argument("--out", default="out/snapshot_frame.png")
    args = ap.parse_args()

    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

    emu = _C64(vic_emulation="accurate-rust")
    _load_snapshot(emu, args.snapshot)

    # Enable beam capture regardless of --mode: even latched renderers read
    # the beam border snapshots, and we want the beam buffers primed.
    emu.memory.beam_render_enabled = True
    emu.memory.ensure_beam_buffers()
    emu.memory.prime_beam_snapshots_from_current_vic()

    # Step the CPU to fill beam buffers.
    done = 0
    stuck = 0
    while done < args.cycles:
        try:
            cyc = emu.cpu.cpu_step_quantum(
                None,
                None,
                int(emu.current_cycles),
                int(emu.current_cycles) + (args.cycles - done) + 1,
            )
        except Exception as exc:
            print(f"cpu step failed: {exc}", file=sys.stderr)
            break
        if cyc <= 0:
            stuck += 1
            if stuck > 200:
                print("cpu stuck — halting step loop", file=sys.stderr)
                break
            continue
        stuck = 0
        emu.current_cycles += cyc
        done += cyc

    # Build a headless Pygame renderer. Avoid the interactive run loop: only
    # create the surfaces and call _render_frame directly.
    pygame = __import__("pygame")
    if not pygame.get_init():
        pygame.init()

    graphics_mod = import_module(f"{_PKG}.graphics")
    ui = graphics_mod.PygameInterface(emu, scale=1, fps=30, border_size=32)

    # Minimal setup: create a native-size display/frame surface like PygameInterface.run() does.
    ui._pygame = pygame
    native_w = 320 + 2 * ui.border_size
    native_h = 200 + 2 * ui.border_size
    ui._native_size = (native_w, native_h)
    ui._display_size = (native_w, native_h)
    # Needed before ``Surface.convert`` works in dummy SDL mode.
    display_surface = pygame.display.set_mode((native_w, native_h))
    ui._display_surface = display_surface
    ui._frame_surface = pygame.Surface((native_w, native_h)).convert()
    ui._rgb_frame = graphics_mod.RgbFrameBuffer(native_w, native_h)
    ui._screen_rect = pygame.Rect(ui.border_size, ui.border_size, 320, 200)

    # Do one render call
    if args.mode == "beam":
        ui._render_frame_beam()
    else:
        ui._render_frame_latched()

    buf = bytes(ui._rgb_frame.buf)
    surf = pygame.image.frombuffer(buf, (native_w, native_h), "RGB")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    pygame.image.save(surf, args.out)
    print(f"wrote {args.out} ({native_w}x{native_h}) from snapshot {args.snapshot}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
