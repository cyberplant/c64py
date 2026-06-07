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

## Status

**Not implemented** in this repository as of the document date; this is a roadmap note for contributors.
