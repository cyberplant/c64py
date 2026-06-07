# Debugging c64py (Rust core, VIC, disk, hangs)

This document describes how to narrow down differences between the optional Rust fast path, Python execution, and VICE when a program hangs or misbehaves.

## 0. Interpreter: use the same venv as `maturin`

The optional extension **`c64py_rust_core`** is loaded only by the Python that had **`maturin develop`** (or `pip install`) run against it. If you run `python3 C64.py` from the system interpreter while the wheel was built in **`.venv`**, imports fail or you silently fall back to **pure Python** (no Rust batch, no hybrid VIC in Rust).

- Prefer: **`./.venv/bin/python C64.py …`** from the repo root (after `python3 -m venv .venv` and installing the project there).
- Quick check: `./.venv/bin/python -c "import c64py_rust_core; print(c64py_rust_core.rust_core_version())"` — if this errors, you are not using the Rust-enabled interpreter.
- **`pytest`** should use the same interpreter: `./.venv/bin/python -m pytest …`

A scripted reproduction matrix for title-screen / VIC issues lives at [`scripts/run_vic_hang_matrix.sh`](../scripts/run_vic_hang_matrix.sh).

## 1. Reproduction matrix

Run the same PRG or D64 with these combinations and note **which configuration fails**:

| CPU path | How |
|----------|-----|
| Python (no Rust batch) | Uninstall / do not build `c64py_rust_core`, **`C64PY_USE_RUST_FAST=0`**, or **`--vic-emulation accurate-python`** (always Python). |
| Smaller Rust batches | `C64PY_RUST_BATCH=1` — still uses **`run_fast_batch`** each time (one instruction per call); does **not** switch to the Python CPU loop; useful only to stress the Python↔Rust boundary. |
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

| Disk | Config / tool |
|------|-----------------|
| Real 1541 6502 + DOS ROM, job-queue trap + KERNAL `$FFD5` shortcut (full speed) | `[emulation] disk_emulation = "fast"` (default) |
| Real 1541, full bit-level IEC + GCR head in Python (WIP, falls back to `fast` until M2 lands) | `disk_emulation = "accurate-python"` or standalone `--emulation accurate-python` |
| Same as `accurate-python` but drive runs in the Rust core (WIP, falls back to `accurate-python` then `fast`) | `disk_emulation = "accurate-rust"` or standalone `--emulation accurate-rust` |

| Beam / per-raster sampling | Flag |
|---------------------------|------|
| Frame latched VIC (default graphics) | `--video-rendering per-frame` (alias: `fast`, default) |
| Per-raster-line VIC + CIA2 samples, per-row dispatch for all modes (split-screen safe) | `--video-rendering per-raster` (alias: `accurate`, see §5) |

### Interpreting results

- **Fails only with large Rust batches and `accurate-rust`:** suspect hybrid VIC raster/IRQ alignment vs Python.
- **Fails with any Rust batch but not with `C64PY_RUST_BATCH=1`:** suspect memory/IO semantics across the Python↔Rust boundary (CIA, CIA2/IEC, 6510 port, etc.).
- **Fails only with disk:** compare `disk_emulation = "fast"` vs an accurate tier in `c64py.toml`; the fast path uses KERNAL hooks at `$FFD5` / `$FFD8` (still delegated from Rust batches via stop PCs).

## 2. Tracing and logging

- **`--vice-trace FILE`:** VICE-style instruction log from [`debug.py`](../debug.py) (`ViceTraceLogger`). Compare with the same program in VICE.
- **`--udp-debug` / `--udp-debug-port`:** JSON events over UDP for external tools.
- **VICE remote monitor:** [`scripts/vice_monitor_client.py`](../scripts/vice_monitor_client.py) automates breakpoints, memory dumps, and watchpoints against **VICE’s** TCP monitor (default port 6510). Use this as the reference machine when c64py’s own monitor is not enough.

## 3. Memory fingerprint at a hang

1. In VICE, break when stuck (or at a known PC), then `m <start> <end>` in the monitor.
2. In c64py (non-graphics or headless), stop the emulator and use `--dump-hex-range START-END` (hex, inclusive), e.g. `C200-C2FF`, to print a hex dump and SHA-256 of that RAM range for quick diffing.
3. **One-shot full snapshot (recommended for long runs):** add **`--dump-ram-sha256`**, **`--dump-cpu-state`**, and optionally **`--dump-ram-raw FILE`** to the same command. You get a single-line SHA-256 of all 64 KiB RAM, current PC and registers, and a raw binary you can `cmp` or hex-diff against another mode—without a second multi-minute run. See [`scripts/run_vic_hang_matrix.sh`](../scripts/run_vic_hang_matrix.sh).
4. Compare zero page, stack (`$0100`–`01FF`), and game-specific pointers the same way.

## 4. One-shot register/RAM inject

`--debug-inject-at-cycle N` with `--debug-inject-map` / `--debug-inject-file` applies pokes (and optional `a=`, `x=`, …) once the cumulative CPU cycle count reaches **N**. Useful to align state with a VICE snapshot mid-run.

## 5. Per-raster video rendering

`--video-rendering per-raster` (formerly `accurate`) uses per-raster-line VIC register copies and CIA2 port A (VIC bank) samples. Each of the 25 content rows is dispatched independently to the appropriate pixel renderer (hires text, multicolor text, ECM, hires bitmap, multicolor bitmap) based on **that row's** sampled VIC config; games that flip `$D011`/`$D016`/`$D018`/`$D020`-`$D024` or CIA2-PA between bands (HUD+playfield splits, color bars, charset/bank swaps) compose correctly. The **Python** CPU path calls `MemoryMap.beam_capture_raster_line` from the raster/VIC tick hooks. When the **Rust** fast batch runs with `MemoryMap.beam_render_enabled`, the core writes **`MemoryMap.beam_vic_flat` / `beam_cia2_flat` in place** (no full-buffer copy back per batch); pygame reads those shared bytearrays.

**Granularity:** one sample per raster line, latched at the start of the line. Sub-row effects (mode changes *within* an 8-scanline row, open sideborders, FLI proper, AGSP, per-scanline color stripes inside a row) need the future per-cycle renderer — they will look like they snap to row boundaries under `per-raster`.

**Border limits:** The presenter samples **one** border color (`$D020`) per raster line from that line’s VIC snapshot. Real hardware and VICE can change the border **several times on the same line**; matching that needs finer-than-line sampling (e.g. cycle-keyed VIC events), not just RAM write batching.

**Charset:** The VIC-II fetches dot patterns from **character ROM** for offsets ``$1000``–``$1FFF`` inside video **banks** ``$0000`` and ``$8000``; the CPU still sees **RAM** at those physical addresses. Pygame uses `MemoryMap.read_vic_charset_glyph_rows` / `read_vic_charset_block_2k` so the boot charset at bank 0 + ``$1000`` matches hardware. For CPU-visible char ROM at ``$D000`` (CHAREN=0), `MemoryMap.read` is still correct.

## 6. TCP monitor (c64py)

When started with `--monitor-port PORT`, a small line-oriented TCP server (see `monitor_tcp.py`) provides `HELP`, `REGS`, `M`, `STEP`, `GO`, `HALT`, `STOP` (sets `emu.running = False`), `BREAK`, `CLEARBREAK`, `QUIT` (closes the client connection after `OK bye`). This is **not** wire-compatible with VICE’s full monitor protocol.

## 7. VICE screenshot / frame comparison

For pixel-level comparison, capture the same frame in VICE and c64py (e.g. same freeze point or same `max-cycles` headless run), then compare PNGs with `compare` (ImageMagick), `perceptualdiff`, or a short Python PIL script. There is no built-in hash hook yet; the `scripts/` tree may grow helpers over time.
