# Debugging c64py (Rust core, VIC, disk, hangs)

This document describes how to narrow down differences between the optional Rust fast path, Python execution, and VICE when a program hangs or misbehaves.

## 1. Reproduction matrix

Run the same PRG or D64 with these combinations and note **which configuration fails**:

| CPU path | How |
|----------|-----|
| Python (no Rust batch) | Uninstall / do not build `c64py_rust_core`, or set `C64PY_RUST_BATCH=1` to minimize batch size (still uses Rust if installed — see below). |
| Rust batches | Default when the extension is built; `C64PY_RUST_BATCH` sets max instructions per batch (default `64`). |

| VIC / display | Flag |
|---------------|------|
| Coarse raster | `--vic-emulation fast` |
| Cycle Python VIC + BA stalls | `--vic-emulation accurate-python` |
| Hybrid VIC in Rust (when built) | `--vic-emulation accurate-rust` (default in CLI today) |

| SID / reSID | Env |
|-------------|-----|
| Lockstep reSID in Rust batches | `C64PY_RUST_RESID_LOCKSTEP=1` (default) |
| Disable Rust reSID batch path | `C64PY_RUST_RESID_LOCKSTEP=0` |

| Disk | Flag |
|------|------|
| KERNAL hooks + virtual `DiskDrive` | `--disk-emulation fast` (default) |
| IEC bus + 1541 ROM structure (in progress) | `--disk-emulation accurate` |

| Beam / per-raster sampling | Flag |
|---------------------------|------|
| Frame latched VIC (default graphics) | `--video-rendering fast` (default) |
| Per-raster-line VIC + CIA2 samples (Python capture + Rust in-place beam buffers) | `--video-rendering accurate` (see §5) |

### Interpreting results

- **Fails only with large Rust batches and `accurate-rust`:** suspect hybrid VIC raster/IRQ alignment vs Python.
- **Fails with any Rust batch but not with `C64PY_RUST_BATCH=1`:** suspect memory/IO semantics across the Python↔Rust boundary (CIA, CIA2/IEC, 6510 port, etc.).
- **Fails only with disk:** compare `--disk-emulation fast` vs `accurate`; fast path uses KERNAL hooks at `$FFD5` / `$FFD8` (still delegated from Rust batches via stop PCs).

## 2. Tracing and logging

- **`--vice-trace FILE`:** VICE-style instruction log from [`debug.py`](../debug.py) (`ViceTraceLogger`). Compare with the same program in VICE.
- **`--udp-debug` / `--udp-debug-port`:** JSON events over UDP for external tools.
- **VICE remote monitor:** [`scripts/vice_monitor_client.py`](../scripts/vice_monitor_client.py) automates breakpoints, memory dumps, and watchpoints against **VICE’s** TCP monitor (default port 6510). Use this as the reference machine when c64py’s own monitor is not enough.

## 3. Memory fingerprint at a hang

1. In VICE, break when stuck (or at a known PC), then `m <start> <end>` in the monitor.
2. In c64py (non-graphics or headless), stop the emulator and use `--dump-hex-range START-END` (hex, inclusive), e.g. `C200-C2FF`, to print a hex dump and SHA-256 of that RAM range for quick diffing.
3. Compare zero page, stack (`$0100`–`01FF`), and game-specific pointers the same way.

## 4. One-shot register/RAM inject

`--debug-inject-at-cycle N` with `--debug-inject-map` / `--debug-inject-file` applies pokes (and optional `a=`, `x=`, …) once the cumulative CPU cycle count reaches **N**. Useful to align state with a VICE snapshot mid-run.

## 5. Accurate video rendering

`--video-rendering accurate` uses per-raster-line VIC register copies and CIA2 port A (VIC bank) samples. The **Python** CPU path calls `MemoryMap.beam_capture_raster_line` from the raster/VIC tick hooks. When the **Rust** fast batch runs with `MemoryMap.beam_render_enabled`, the core writes **`MemoryMap.beam_vic_flat` / `beam_cia2_flat` in place** (no full-buffer copy back per batch); pygame reads those shared bytearrays.

**Border limits:** The presenter samples **one** border color (`$D020`) per raster line from that line’s VIC snapshot. Real hardware and VICE can change the border **several times on the same line**; matching that needs finer-than-line sampling (e.g. cycle-keyed VIC events), not just RAM write batching.

**Charset:** The VIC-II fetches dot patterns from **character ROM** for offsets ``$1000``–``$1FFF`` inside video **banks** ``$0000`` and ``$8000``; the CPU still sees **RAM** at those physical addresses. Pygame uses `MemoryMap.read_vic_charset_glyph_rows` / `read_vic_charset_block_2k` so the boot charset at bank 0 + ``$1000`` matches hardware. For CPU-visible char ROM at ``$D000`` (CHAREN=0), `MemoryMap.read` is still correct.

## 6. TCP monitor (c64py)

When started with `--monitor-port PORT`, a small line-oriented TCP server (see `monitor_tcp.py`) provides `HELP`, `REGS`, `M`, `STEP`, `GO`, `HALT`, `STOP` (sets `emu.running = False`), `BREAK`, `CLEARBREAK`, `QUIT` (closes the client connection after `OK bye`). This is **not** wire-compatible with VICE’s full monitor protocol.

## 7. VICE screenshot / frame comparison

For pixel-level comparison, capture the same frame in VICE and c64py (e.g. same freeze point or same `max-cycles` headless run), then compare PNGs with `compare` (ImageMagick), `perceptualdiff`, or a short Python PIL script. There is no built-in hash hook yet; the `scripts/` tree may grow helpers over time.
