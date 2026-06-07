# Emulation modes (VIC and audio)

c64py exposes two main **VIC timing** modes and ties **ReSID** lockstep to that choice.

## Fast VIC (default)

- **CLI:** omit `--accurate-vic` (this is the default).
- **Behavior:** Raster uses **`_advance_raster` once per completed instruction** (batched by the instruction’s cycle count), not once per CPU bus access. That matches older c64py throughput; stepping raster/CIA on every `_mr`/`_mw` was a large regression for fast mode.
- **Throughput:** Much faster than accurate mode; suitable for games and demos when cycle-exact VIC IRQ/badline behavior is not required.
- **Graphics:** With `--graphics`, the pygame thread uses a **latched VIC + CIA2 bank snapshot** for compositing (`MemoryMap.snapshot_vic_render_state`). **Fast VIC** sets `vic_snapshot_each_emulated_frame = False`, so the latch updates **once per host present** (before `_render_frame`), not on every emulated raster wrap — this restores throughput vs older builds while keeping stable regs for what you draw. **`--accurate-vic`** keeps **per-emulated-frame** CPU-thread snapshots for cycle-aligned sampling. **Headless** sets `vic_render_snapshots = False` so snapshots are skipped entirely.
- **ReSID (`--enable-resid`):** The SID runs in **decoupled** mode: the CPU thread does not call into reSID every emulated cycle; the audio path advances the emulated SID clock in **larger chunks** when filling PCM buffers. Audio should still track the program well for typical music playback; **SID register readback** timing vs the CPU is **not** cycle-accurate in this mode.

## Accurate VIC (`--accurate-vic`)

- **Behavior:** One **`ViciiCycleEngine.tick()`** per **CPU cycle**, aligned with the current PAL/NTSC model. IRQ and badline-related cases match VICE much more closely.
- **Cost:** Substantially slower; intended for regression tests and difficult VIC timing.
- **ReSID:** **`cpu_lockstep=True`** — SID advancement stays tied to the CPU thread per emulated cycle (slower, closer for code that polls SID state).

## Choosing a mode

| Goal | Suggestion |
|------|------------|
| Everyday play, benchmarks, pygame + music | Fast VIC (default) |
| VIC IRQ / raster / badline tests, VICE parity | `--accurate-vic` |
| SID readback / sample timing vs CPU | `--accurate-vic` + `--enable-resid` |

## Related code

- `C64.py` / `emulator.py`: `accurate_vic` passed into `CPU6502` and ReSID construction.
- `cpu.py`: `accurate_vic` selects `_vic_tick_one` vs `_advance_raster` in the step loop.
- `resid.py`: `ReSIDEmulator(..., cpu_lockstep=...)`.
- `memory.py`: `vic_render_snapshots` enables snapshot copies; `vic_snapshot_each_emulated_frame` selects **CPU-thread** (accurate + graphics) vs **present-time** (fast + graphics) latching — wired in `C64.py`.

See [performance.md](performance.md) for benchmark commands and the **graphics + ReSID + turbo** regression canary.
