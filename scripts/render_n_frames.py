"""Render N consecutive frames from a snapshot for glitch diagnosis.

Used by item F of ``docs/input_config_plan.md`` (title-screen glitch
diagnosis). Loads ``--snapshot``, then repeatedly advances the emulator
by roughly one PAL frame (~19656 CPU cycles) and saves the rendered
output as ``frame_NNN.png``. Sampling can be sub-sampled with
``--stride``.

Running this twice with the same arguments and diffing the resulting
PNGs distinguishes between a Rust/Python beam-buffer race (H1 — runs
diverge) and a deterministic unmodeled VIC effect (H2 — runs are
byte-identical, but periodic glitch frames still appear).

Usage:
    python scripts/render_n_frames.py \
        --snapshot snapshots/title.snap \
        --frames 240 --stride 1 --out-dir out/glitch_diag/runA
"""

from __future__ import annotations

import argparse
import hashlib
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

# PAL: 312 lines * 63 cycles/line.
PAL_CYCLES_PER_FRAME = 312 * 63


def _step_cycles(emu, target: int) -> None:
    done = 0
    stuck = 0
    while done < target:
        try:
            cyc = emu.cpu.cpu_step_quantum(
                None,
                None,
                int(emu.current_cycles),
                int(emu.current_cycles) + (target - done) + 1,
            )
        except Exception as exc:
            print(f"cpu step failed: {exc}", file=sys.stderr)
            return
        if cyc <= 0:
            stuck += 1
            if stuck > 200:
                print("cpu stuck — halting step loop", file=sys.stderr)
                return
            continue
        stuck = 0
        emu.current_cycles += cyc
        done += cyc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", required=True, help="Path to .snap file")
    ap.add_argument("--frames", type=int, default=120, help="Number of frames to advance")
    ap.add_argument("--stride", type=int, default=1, help="Save every K-th frame")
    ap.add_argument("--out-dir", required=True, help="Directory to write frame_NNN.png into")
    ap.add_argument(
        "--video-rendering",
        choices=("per-raster", "per-cycle"),
        default="per-raster",
        help="Host sampling tier for PygameInterface._render_frame (default: per-raster beam)",
    )
    ap.add_argument(
        "--warmup-cycles",
        type=int,
        default=PAL_CYCLES_PER_FRAME,
        help="Cycles to step before first capture (primes beam buffers)",
    )
    args = ap.parse_args()

    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.makedirs(args.out_dir, exist_ok=True)

    emu = _C64(
        vic_emulation="accurate-python" if args.video_rendering == "per-cycle" else "accurate-rust"
    )
    _load_snapshot(emu, args.snapshot)

    if args.video_rendering == "per-cycle":
        emu.memory.beam_render_enabled = False
        emu.memory.per_cycle_render_enabled = True
        emu.memory.ensure_per_cycle_buffers()
        emu.memory.prime_per_cycle_snapshots_from_current_vic()
    else:
        emu.memory.beam_render_enabled = True
        emu.memory.per_cycle_render_enabled = False
        emu.memory.ensure_beam_buffers()
        emu.memory.prime_beam_snapshots_from_current_vic()

    _step_cycles(emu, args.warmup_cycles)

    pygame = __import__("pygame")
    if not pygame.get_init():
        pygame.init()

    graphics_mod = import_module(f"{_PKG}.graphics")
    ui = graphics_mod.PygameInterface(emu, scale=1, fps=30, border_size=32)
    ui._pygame = pygame
    native_w = 320 + 2 * ui.border_size
    native_h = 200 + 2 * ui.border_size
    ui._native_size = (native_w, native_h)
    ui._display_size = (native_w, native_h)
    display_surface = pygame.display.set_mode((native_w, native_h))
    ui._display_surface = display_surface
    ui._frame_surface = pygame.Surface((native_w, native_h)).convert()
    ui._rgb_frame = graphics_mod.RgbFrameBuffer(native_w, native_h)
    ui._screen_rect = pygame.Rect(ui.border_size, ui.border_size, 320, 200)

    hashes: list[tuple[int, str]] = []
    saved = 0
    for frame_idx in range(args.frames):
        _step_cycles(emu, PAL_CYCLES_PER_FRAME)
        if frame_idx % args.stride != 0:
            continue
        ui._render_frame()
        buf = bytes(ui._rgb_frame.buf)
        digest = hashlib.sha256(buf).hexdigest()
        hashes.append((frame_idx, digest))
        surf = pygame.image.frombuffer(buf, (native_w, native_h), "RGB")
        out_path = os.path.join(args.out_dir, f"frame_{frame_idx:04d}.png")
        pygame.image.save(surf, out_path)
        saved += 1

    manifest_path = os.path.join(args.out_dir, "hashes.txt")
    with open(manifest_path, "w") as f:
        for idx, digest in hashes:
            f.write(f"{idx:04d} {digest}\n")

    print(
        f"wrote {saved} frames ({native_w}x{native_h}) to {args.out_dir} "
        f"(snapshot={args.snapshot}, frames={args.frames}, stride={args.stride})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
