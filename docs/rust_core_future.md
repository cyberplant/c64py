# Future native core (Rust) — design sketch only

This document records a **possible** direction for much higher throughput: move the **hot emulation loop** (6502 + memory decode + fast-path VIC advance + optional CIA stepping) into a **Rust** library, callable from Python with a thin FFI boundary.

## Motivation

Python spends significant time per instruction on dispatch, object overhead, and frequent calls across the CPU / memory / VIC boundary. A compact Rust core could:

- Run **millions of emulated cycles per call** with **minimal Python involvement**.
- **Release the GIL** for the duration of `run_cycles(n)` so other Python threads (pygame presenter, audio) stay responsive.
- Preserve **`--accurate-vic`** as either a second Rust implementation or a slower path that remains in Python until ported.

## Suggested boundary

- **Input:** initial or delta state (RAM pages that changed, register file, IRQ lines, optional disk/IEC “events” queue).
- **Output:** cycles executed, updated 6502 state, memory dirty flags, audio samples produced (if SID is in the same crate or a sibling), raster snapshot markers for the UI thread.

The Python layer would keep **ROM loading**, **pygame**, **CLI**, tests that compare against VICE traces, and gradual migration of subsystems.

## Testing strategy

- **Golden traces:** Fixed PRGs with `--max-cycles` and memory/register dumps at checkpoints (existing tests extended).
- **Differential:** Run the same program in Python vs Rust core for N cycles and assert identical state (or allow known divergences documented per mode).
- **Performance:** Reuse the **swinth + graphics + ReSID + turbo** canary from [performance.md](performance.md) once the Rust path can drive the same front end.

## Status (v1 implemented)

An **optional** PyO3 module `c64py_rust_core` lives under `rust/c64py-core/`. Python exposes it via `c64py._core` and `CPU6502.step_fast_batch()` when it is safe to use.

### Build

From the repository root (Rust stable + `maturin`):

```bash
pip install maturin
maturin develop --manifest-path rust/c64py-core/Cargo.toml
```

Pure-Python installs are unchanged; omit the step above if you do not need the native core.

### When the Rust batch runs

`CPU6502.step_fast_batch(n)` calls Rust only if **all** of the following hold:

- Extension import succeeds and `C64PY_USE_RUST_FAST` is not `0` / `false`.
- **Fast VIC** (`accurate_vic` is false).
- No **trace** (`trace_enabled` false), no **trace sync PC** env hook, no **debug inject** fields set.
- **SID** is absent or ReSID/SID is in **decoupled** mode (`_cpu_lockstep` is false). Lockstep audio forces the Python `step()` path.
- **`interface` is None** so the **CHROUT ($FFD2) shortcut** in `step()` is not required (Rust does not implement that shortcut).
- `memory.ram` is a **`bytearray`** (shared copy-in/copy-out in Rust).

Otherwise the same API runs **n** Python `step()` calls.

### Known gaps vs full `step()`

- **CHROUT / CINT** fast paths and other **interface** hooks only exist on the Python path.
- **`--accurate-vic`**, **UDP / VICE trace**, **lockstep SID**, and **badline extra cycles** are out of scope for the Rust batch (see [emulation_modes.md](emulation_modes.md)).
- **SID** is advanced in Python after the batch via `memory.sid_tick_cpu_cycles(total_cycles)` (decoupled mode).

### Tests

- Rust: `cargo test` in `rust/c64py-core/`.
- Python differential: `test/test_rust_core_parity.py` (skipped if the extension is not built).
