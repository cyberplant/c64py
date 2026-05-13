# Rust core: status and remaining work

The optional PyO3 module **`c64py_rust_core`** (under
`rust/c64py-core/`) hosts the hot 6502 + memory + fast-path-VIC loop.
Python exposes it via `c64py._core` and `CPU6502.step_fast_batch()`.
The main run loop calls `C64.run_cpu_instruction_quantum`, which
dispatches into the Rust batch when it is safe and falls back to a
single Python `step()` otherwise.

This is shipped and the default path on PAL when the extension is
built (`maturin develop --manifest-path rust/c64py-core/Cargo.toml`).

## What's implemented

- 6502 fast path (full opcode coverage including illegal opcodes
  exercised by historical loader investigations).
- Hybrid accurate VIC for PAL **and** NTSC (cycle tables, raster IRQ,
  sprite DMA state machine, sprite-sprite + sprite-bg per-pixel
  collisions).
- Stop-PC handoff for `$FFD2` (CHROUT shortcut), `$FFD5`/`$FFD8`
  (KERNAL LOAD/SAVE hooks), and `$FF5B`/`$FFCF` when there is no
  KERNAL ROM.
- Optional reSID lockstep clocking from inside the Rust batch via the
  shared `resid_c.dylib` (including hybrid-IRQ phases so SID time matches
  the emulated cycle counter).
- **Per-cycle pygame tier:** with the extension built, hybrid VIC fills
  `per_cycle_vic_flat` / `per_cycle_cia2_flat` during `run_fast_batch`, and
  `_core.composite_per_cycle_frame` draws text, bitmap, and sprites in one
  pass (sprite expansion, line-latched attribute regs, ``$D01B`` vs opaque fg;
  env toggles `C64PY_RUST_PER_CYCLE`, `C64PY_RUST_COMPOSITE`).
- Differential test coverage: `test/test_rust_core_parity.py`,
  `test/test_kernal_hook_rts.py`, plus Rust `cargo test` in
  `rust/c64py-core/`.

## Remaining work

1. **Rust hybrid vs ``accurate-python``** — parity for newly reported VIC/CPU
   timing cases; see `test/test_rust_core_parity.py`.

2. **Coarse "run N cycles with breakpoints"**. Today each batch is
   bounded by `C64PY_RUST_BATCH` (default 64) plus stop PCs; a
   future API could let Rust run until the next IRQ / raster
   breakpoint and call back into Python only on edge events,
   reducing FFI overhead further.

3. **Trace / UDP-debug paths** still force Python `step()` per
   instruction. A native trace emitter in Rust would let those modes
   keep batching.
