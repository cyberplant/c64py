# Rust core: status and remaining work

The optional PyO3 module **`c64py_rust_core`** (under
`rust/c64py-core/`) hosts the hot 6502 + memory + hybrid-VIC loop.
Python exposes it via `c64py._core` and `CPU6502.step_fast_batch()`.
The main run loop calls `C64.run_cpu_instruction_quantum`, which
dispatches into the Rust batch when it is safe and falls back to a
single Python `step()` otherwise.

Build and install: `maturin develop --manifest-path rust/c64py-core/Cargo.toml`.

**Default CLI path:** `C64.py` uses **`--vic-emulation accurate-rust`** (PAL
and NTSC) when the extension is built; that mode **requires** the extension
and exits with an error if it is missing. **`--vic-emulation fast`** also uses
the Rust CPU batch when available (coarse raster, no hybrid VIC in Rust).

## What's implemented

- 6502 fast path (full opcode coverage including illegal opcodes
  exercised by historical loader investigations).
- Hybrid accurate VIC for PAL **and** NTSC (VICE `cycle_tab_*`, raster IRQ,
  sprite DMA state machine, sprite-sprite + sprite-bg per-pixel
  collisions, **BA/CPU stall arbitration** on READ bus phases).
- Stop-PC handoff for `$FFD2` (CHROUT shortcut), `$FFD5`/`$FFD8`
  (KERNAL LOAD/SAVE hooks), `$FF5B`/`$FFCF` when there is no
  KERNAL ROM, plus TCP IEC KERNAL hook vectors (`$F9ED`, `$FDF9`,
  `$FFC0`–`$FFCF`) when `kernal_tcp_iec_vectors` is active.
- Optional reSID lockstep clocking from inside the Rust batch via the
  shared `resid_c.dylib` (including hybrid-IRQ phases so SID time matches
  the emulated cycle counter).
- **Per-raster beam capture:** when `MemoryMap.beam_render_enabled`, Rust
  writes `beam_vic_flat` / `beam_cia2_flat` in place during batches
  (see [DEBUGGING.md](DEBUGGING.md) §5).
- **Per-cycle pygame tier:** with the extension built, hybrid VIC fills
  `per_cycle_vic_flat` / `per_cycle_cia2_flat` during `run_fast_batch`, and
  `_core.composite_per_cycle_frame` draws text, bitmap, and sprites in one
  pass (sprite expansion, line-latched attribute regs, ``$D01B`` vs opaque fg;
  env toggles `C64PY_RUST_PER_CYCLE`, `C64PY_RUST_COMPOSITE`).
- **VICE-format trace** from Rust batches (`--vice-trace`, `C64PY_RUST_VICE_TRACE`)
  without falling back to Python `step()` per instruction.
- **Limited IEC CIA2 merge:** peer CLK/DATA sampled at batch start; optional
  CIA2 write-log replay after the batch for wire-decode taps (`iec_cia2_write_log`).
- CIA1 keyboard matrix and joystick state updated in Rust; batches run under
  `py.detach()` (GIL released) so audio/UI threads can progress.
- Test coverage: `test/test_rust_core_parity.py`, `test/test_kernal_hook_rts.py`,
  `test/test_iec_rust_interlock.py`, `test/test_per_cycle_rust_composite.py`,
  `test/test_rust_env_batch_gate.py`, plus Rust `cargo test` in `rust/c64py-core/`.

## Batch bypass / limitations

Rust batching is **skipped** (Python `step()` per instruction) when:

- **`--udp-debug`** is enabled.
- **`C64PY_TRACE_SYNC_PC`** is set, or **`debug_inject_at_cycle`** / inject maps are active.
- **`iec_disk_full_impl=True`** (accurate disk tier — mid-batch `$DD00` must reach
  `IECBus` and the 1541 VIA; see `test/test_iec_rust_interlock.py`).
- **`C64PY_USE_RUST_FAST=0`**, or **`C64PY_RUST_HYBRID_VIC=0`** with `accurate-rust`.
- reSID lockstep cannot use the Rust SID path (`C64PY_RUST_RESID_LOCKSTEP=0` or no `resid_c` ptr).

For wire-level IEC fidelity on the Rust path, **`C64PY_IEC_WIRE_DECODE_STRICT_RUST=1`**
caps batches to one instruction (still Rust, not full Python fallback).

Mid-batch IEC peer updates remain a limitation when batching is active: only
snapshot-at-start plus optional post-batch CIA2 replay.

## Remaining work

1. **Rust hybrid vs ``accurate-python``** — parity for newly reported VIC/CPU
   timing cases; see `test/test_rust_core_parity.py` (suite still small).

2. **Coarse "run N cycles with breakpoints"**. Today each batch is
   bounded by `C64PY_RUST_BATCH` (default 64), stop PCs, and optional
   `max_cycles` cap; a future API could let Rust run until the next IRQ /
   raster breakpoint and call back into Python only on edge events,
   reducing FFI overhead further.

3. **UDP debug and trace-sync paths** — `--udp-debug`, `C64PY_TRACE_SYNC_PC`,
   and debug-inject still force Python `step()` per instruction. VICE trace
   is already native in Rust (see above).

4. **Accurate disk tier in Rust** — true bit-level IEC + 1541 stepping inside
   batches (today requires `iec_disk_full_impl` Python lockstep).
