"""Dump per-raster-line VIC registers from a snapshot.

Boots the emulator from a saved snapshot, enables beam capture, runs a couple
of frames so the beam buffers fill with real per-raster data, then prints
selected VIC registers per raster line so we can see mid-frame register
toggles (e.g., ``$D016.MCM`` flipped by a raster IRQ for HUD/playfield
split-screens).

Usage:
    PYTHONPATH=.. python -m c64py.scripts.dump_beam_d016 snapshots/foo.snap

When run from the repo root with ``python scripts/dump_beam_d016.py`` the
wrapper below massages ``sys.path`` so relative imports inside the package
continue to work.
"""

from __future__ import annotations

import os
import sys


def _bootstrap_path() -> str:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parent = os.path.dirname(repo_root)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    return os.path.basename(repo_root)


_PKG = _bootstrap_path()

from importlib import import_module

_C64 = import_module(f"{_PKG}.emulator").C64
_load_snapshot = import_module(f"{_PKG}.snapshot").load_snapshot


def main(snap_path: str, frames: int = 2, instr_per_step: int = 20000) -> None:
    emu = _C64(vic_emulation="accurate-rust")
    _load_snapshot(emu, snap_path)
    emu.memory.beam_render_enabled = True
    emu.memory.ensure_beam_buffers()
    emu.memory.prime_beam_snapshots_from_current_vic()
    print(
        f"post-load: PC=${emu.cpu.state.pc:04X} cycles={emu.current_cycles} "
        f"cpu_cycles={emu.cpu.state.cycles} raster={emu.memory.raster_line}",
        file=sys.stderr,
    )

    # Run ~frames * 20k instructions; a PAL frame is ~19.6k CPU cycles and most
    # instructions take ~2-6 cycles, so ~5-7k instructions per frame. Overshooting
    # is fine here — we just need the beam buffers to be filled with real samples.
    # Step directly through the CPU for a few frames of work. Each PAL frame
    # is ~19 656 cycles; we run ~3 frames so the beam buffer has steady samples.
    target_cycles = frames * 20000
    done = 0
    stuck = 0
    while done < target_cycles:
        try:
            cyc = emu.cpu.cpu_step_quantum(
                None,
                None,
                int(emu.current_cycles),
                int(emu.current_cycles) + (target_cycles - done) + 1,
            )
        except Exception as exc:
            print(f"step failed: {exc}", file=sys.stderr)
            break
        if cyc <= 0:
            stuck += 1
            if stuck > 100:
                print(f"giving up (stuck). PC=${emu.cpu.state.pc:04X}", file=sys.stderr)
                break
            continue
        stuck = 0
        emu.current_cycles += cyc
        done += cyc
    print(
        f"after run: PC=${emu.cpu.state.pc:04X} cycles_ran={done} "
        f"raster={emu.memory.raster_line}",
        file=sys.stderr,
    )

    # Rust fast batch writes into the flat buffers; the ``beam_vic_lines``
    # list-of-bytes is only the Python-path capture. Prefer flat.
    flat = emu.memory.beam_vic_flat
    c2_flat = emu.memory.beam_cia2_flat
    if flat is not None and len(flat) > 0:
        n = len(flat) // 64
        lines = [bytes(flat[i * 64 : (i + 1) * 64]) for i in range(n)]
        c2 = list(c2_flat) if c2_flat is not None else [0] * n
    else:
        lines = emu.memory.beam_vic_lines or []
        n = len(lines)
        c2 = emu.memory.beam_cia2_lines or []
    print(f"Captured {n} raster-line snapshots from {snap_path}")
    print()
    print("    rl    $D011  $D016  $D018  $D019  $D01A  $D020  $D021  CIA2.PA")
    print("    ----- -----  -----  -----  -----  -----  -----  -----  -------")
    prev_d016 = None
    prev_d018 = None
    for rl, regb in enumerate(lines):
        d011 = regb[0x11] if len(regb) > 0x11 else 0
        d016 = regb[0x16] if len(regb) > 0x16 else 0
        d018 = regb[0x18] if len(regb) > 0x18 else 0
        d019 = regb[0x19] if len(regb) > 0x19 else 0
        d01a = regb[0x1A] if len(regb) > 0x1A else 0
        d020 = regb[0x20] if len(regb) > 0x20 else 0
        d021 = regb[0x21] if len(regb) > 0x21 else 0
        pra = c2[rl] if rl < len(c2) else 0
        marker = ""
        if prev_d016 is not None and d016 != prev_d016:
            marker = f"  <-- D016 changed {prev_d016:02X}->{d016:02X} (MCM={(d016>>4)&1})"
        elif prev_d018 is not None and d018 != prev_d018:
            marker += f"  <-- D018 changed {prev_d018:02X}->{d018:02X}"
        print(
            f"    {rl:5d}  ${d011:02X}   ${d016:02X}   ${d018:02X}   ${d019:02X}   "
            f"${d01a:02X}   ${d020:02X}   ${d021:02X}   ${pra:02X}{marker}"
        )
        prev_d016 = d016
        prev_d018 = d018


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: dump_beam_d016.py <snapshot.snap>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
