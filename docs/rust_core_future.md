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

## Status (v1 + v2 run-loop integration)

An **optional** PyO3 module `c64py_rust_core` lives under `rust/c64py-core/`. Python exposes it via `c64py._core` and `CPU6502.step_fast_batch()` when it is safe to use.

The main emulator loop (`C64.run`, Textual `ui.py`, pygame `graphics.py`) calls **`C64.run_cpu_instruction_quantum`**, which runs KERNAL LOAD/SAVE hooks then either a Rust **batch** (`CPU6502.cpu_step_quantum` → `step_fast_batch`) or a single Python **`step()`** when batching is unsafe.

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
- `memory.ram` is a **`bytearray`** (shared copy-in/copy-out in Rust).

Otherwise the same API runs **n** Python `step()` calls.

### Stop PCs (hand off to Python)

Before each instruction, the Rust runner checks **`cpu.pc`** against a **stop set** passed from Python (`CPU6502._rust_delegate_stop_pcs`). If the PC matches, the batch ends with **`instructions_run == 0`** for that call (no opcode executed at that address in Rust). The next Python **`step()`** then runs the real behavior (CHROUT shortcut, optional KERNAL-less CINT/CHRIN, or normal opcode fetch).

Default stop addresses:

- **`$FFD2`** — CHROUT shortcut in `CPU6502.step`.
- **`$FFD5` / `$FFD8`** — so `C64._handle_kernal_load` / `_handle_kernal_save` always run between batches when the CPU reaches those vectors.
- **`$FF5B` / `$FFCF`** — only when **`kernal_rom` is None** (CINT / CHRIN Python fallbacks).

If you add new **pre-`_execute_opcode`** shortcuts in `step()`, extend **`_python_only_step_pcs`** and **`_rust_delegate_stop_pcs`** together (comments in `cpu.py` point to this).

### Run-loop batch size

`C64PY_RUST_BATCH` (default **`64`**) caps how many instructions one Rust call may attempt per outer loop iteration. Larger values reduce FFI overhead; stop PCs and IRQs still yield correctly. **`C64PY_RUST_BATCH=1`** approximates one instruction per batch.

**UDP debug** and **VICE trace** disable batching: each iteration uses **`step()`** only.

### Known gaps vs full `step()`

- **CHROUT / CINT / CHRIN** shortcuts and **interface**-dependent behavior remain on the Python **`step()`** path; Rust stops at the PCs above instead of duplicating them.
- **`--accurate-vic`**, **UDP / VICE trace**, **lockstep SID**, and **badline extra cycles** are out of scope for the Rust batch (see [emulation_modes.md](emulation_modes.md)).
- **SID** is advanced in Python after the batch via `memory.sid_tick_cpu_cycles(total_cycles)` (decoupled mode).

### Trying it with `C64.py`

- **Rust batching is off when you pass `--accurate-vic`.** That mode uses Python `step()` with cycle-accurate VIC/BA stalls, so throughput stays similar to pre-Rust. To measure the fast core, run **without** `--accurate-vic` (default is fast VIC).
- **Build the extension** in the same venv you use for `python C64.py` (`maturin develop` as above). If `c64py_rust_core` is missing, the emulator silently falls back to Python `step()` only.
- **Optional checks**
  - `C64PY_USE_RUST_FAST=0` — force the Python path (compare speed or debug).
  - `C64PY_RUST_BATCH=128` — larger batches, less FFI overhead (tune if profiling).
- **Headless throughput** (no Textual UI overhead):  
  `python C64.py programs/swinth.prg --headless --turbo --autoquit --max-cycles 5000000`  
  Then read `=== Emulation Speed ===` or the `C64PY_BENCHMARK` line with `--benchmark`.

## Next milestone: hybrid accurate VIC (Rust + Python)

**Goal:** keep **Python `ViciiCycleEngine` / `MemoryMap` raster + BA stall rules** as the **reference**, while letting a **Rust inner loop** run the 6502 fast path **per emulated CPU cycle** in accurate mode (same semantics as today’s `cpu.step()` accurate branch: VIC tick, CIA, optional IRQ, then opcode).

**Suggested sequence**

1. **Extract a “single CPU cycle” contract** from Python: inputs (memory, VIC shadow, CIA, CPU state) and outputs (RAM mutations, raster, `pending_irq`, cycle count). Match existing `step()` accurate path in tests (fixed PRGs, compare state each N cycles).
2. **Port or duplicate the VIC tick for that path in Rust** by **translating** `ViciiCycleEngine::tick` + the glue in `cpu.py` (`_vic_tick_one`, BA stall rules) into `c64py-core`, driven by the same register shadow as today. **Badlines** stay inside that model — they are not a separate subsystem.
3. **Gate with a new flag** (e.g. `C64PY_RUST_ACCURATE_VIC=1`) until parity is good; keep pure-Python `--accurate-vic` as fallback.
4. **Optional:** coarse “run N cycles” in Rust with callback to Python only on IRQ / raster breakpoints (later optimization).

## ReSID and the Rust core — one `resid_c` dylib

**Yes — you only build `resid_c` once.** Python (`resid.py` via **ctypes**) and Rust (**`extern "C"`** declarations matching [src/resid_wrapper/resid_c.h](../src/resid_wrapper/resid_c.h)) load the **same** `resid_c.dylib` / `resid_c.so`. No second reSID build is required unless you choose to **statically** link reSID into the PyO3 crate (optional packaging choice, not required).

**Today**

- **Fast VIC + ReSID decoupled** (`_cpu_lockstep == false`): the CPU thread skips `tick_cpu_cycles`; Rust batching is **allowed**; audio advances on the pygame thread via `resid_clock`. **Same dylib, no Rust involvement.**
- **Accurate VIC or lockstep ReSID** (`_cpu_lockstep == true`): `_rust_fast_batch_usable()` is **false**, so you stay on Python `step()` and existing `tick_cpu_cycles` → **`resid_clock`** with a scratch buffer (see `resid.py`).

**Re-enabling lockstep ReSID while using the Rust CPU batch**

- The **C API** you already have is enough: `resid_read` / `resid_write` / `resid_clock` (same as Python). Rust would call these during a batch when it decodes **$D400–$D41C** accesses and when advancing **emulated cycles**, mirroring `memory.sid_tick_cpu_cycles` + `read_register` / `write_register`.
- **Threading:** `ReSIDEmulator` protects the SID with a Python **`threading.Lock`**. The Rust batch runs with the **GIL held** today, so the audio thread can block on that lock — same as a long Python opcode. If you later **`allow_threads`** around the Rust batch, you must **not** call `resid_*` without the same exclusion rule (e.g. only clock SID from the CPU thread while holding the lock, or move to a `resid_c`-level mutex — larger change).
- **Optional C helper:** if calling `resid_clock` into a scratch buffer every cycle from Rust is awkward, add something like `resid_advance_cycles(resid_sid_t *sid, int cycles)` in `resid_c.cpp` **only if** the underlying reSID API exposes a cheap “clock only” path; otherwise keep **`resid_clock`** with a small stack buffer (same strategy as Python).

**Summary:** one **`resid_c`** shared library for **Python-only** and **Python+Rust**; Rust binds to the **existing C symbols** (or `libloading`). Next implementation step is **wiring** those calls into the Rust memory map / cycle loop when lockstep is on, with **locking** rules matching `resid.py`.

### Tests

- Rust: `cargo test` in `rust/c64py-core/`.
- Python differential: `test/test_rust_core_parity.py` (skipped if the extension is not built), including CHROUT stop-PC coverage.
- KERNAL hook RTS: `test/test_kernal_hook_rts.py`.
