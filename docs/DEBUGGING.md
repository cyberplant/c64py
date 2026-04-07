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
| Frame latched VIC (default graphics) | omit `--render-beam` |
| Per-raster-line VIC snapshots when the CPU advances the beam | `--render-beam` (best results with `--vic-emulation accurate-python` or small `C64PY_RUST_BATCH`; see below) |

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

## 5. Beam render mode limitations

`--render-beam` fills a per-raster-line VIC register history as the **Python** CPU path advances the raster (coarse step in `fast` mode, cycle engine in `accurate-python`). **Rust hybrid batches** advance raster inside native code without recording each line in Python; in that configuration, beam history may be incomplete and the UI may fall back to behavior closer to single-frame latch. Prefer `accurate-python` or `C64PY_RUST_BATCH=1` when debugging raster splits.

## 6. TCP monitor (c64py)

When started with `--monitor-port PORT`, a small line-oriented TCP server (see implementation) provides `HELP`, `REGS`, `M`, `STEP`, `G`, `BREAK`, `CLEARBREAK`, `HALT`. It forces small CPU steps for predictable stepping. This is **not** wire-compatible with VICE’s full monitor protocol.

## 7. VICE screenshot / frame comparison

For pixel-level comparison, capture the same frame in VICE and c64py (e.g. same freeze point or same `max-cycles` headless run), then compare PNGs with `compare` (ImageMagick), `perceptualdiff`, or a short Python PIL script. There is no built-in hash hook yet; the `scripts/` tree may grow helpers over time.
