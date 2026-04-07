# Emulation modes (VIC and audio)

c64py selects **VIC timing** via **`--vic-emulation`** and ties **ReSID** lockstep to whether the CPU path is “accurate” (non-fast) or not.

## CLI: `--vic-emulation`

| Mode | Meaning |
|------|---------|
| **`accurate-rust`** | **Default for `C64.py`.** Cycle-accurate CPU stepping with VIC raster advanced in the optional **Rust** batch using the **PAL 6569** or **NTSC 6567R8** cycle table (VICE `cycle_tab_*`; hybrid, no BA CPU stalls in Rust). |
| **`accurate-python`** | Full Python path: one **`ViciiCycleEngine.tick()`** per **CPU cycle** (BA stalls included). Same as deprecated **`--accurate-vic`**. |
| **`fast`** | Coarse raster: **`_advance_raster` once per completed instruction** — highest throughput; less exact VIC IRQ/badline behavior. |

Opt out of the Rust VIC step while keeping **`accurate-rust`** selected: set **`C64PY_RUST_HYBRID_VIC=0`** (Python accurate VIC during batches).

Programmatic **`C64(..., vic_emulation=...)`** defaults to **`fast`** so tests and embedders keep the lighter path unless they opt in.

## Fast VIC (`--vic-emulation fast`)

- **Behavior:** Raster uses **`_advance_raster` once per completed instruction** (batched by the instruction’s cycle count), not once per CPU bus access.
- **Throughput:** Much faster than accurate modes; suitable when cycle-exact VIC IRQ/badline behavior is not required.
- **Graphics:** With `--graphics`, **fast** uses **present-time** latching (`vic_snapshot_each_emulated_frame = False` in `C64.py`): the latch updates around each host **present**, not every emulated frame.
- **ReSID (`--enable-resid`):** **Decoupled** mode — the audio thread advances reSID in larger chunks; SID readback vs CPU is not cycle-accurate.

## Accurate Python (`--vic-emulation accurate-python` or `--accurate-vic`)

- **Behavior:** One **`ViciiCycleEngine.tick()`** per **CPU cycle**, with BA stall rules. IRQ and badline-related cases match VICE much more closely.
- **Cost:** Slowest; intended for regression tests and difficult VIC timing.
- **ReSID:** **`cpu_lockstep=True`** when ReSID is enabled — SID tied to the CPU thread per emulated cycle.

## Accurate Rust (`--vic-emulation accurate-rust`, default)

- **Behavior:** Same CPU instruction semantics as accurate modes; on **PAL** with **`c64py_rust_core`** installed, the inner batch advances VIC via the Rust hybrid engine (see [rust_core_future.md](rust_core_future.md)). **Known gap:** no BA/CPU stall arbitration in Rust vs full Python accurate path.
- **ReSID:** With **`--enable-resid`**, Rust can drive **`resid_c`** during batches when the shared library is found; PCM is queued for pygame as today.
- **Graphics + PAL:** Frame snapshots after Rust batches match the Python accurate path so pygame sees stable latched regs.

## Choosing a mode

| Goal | Suggestion |
|------|------------|
| Normal `C64.py` use (default) | **`accurate-rust`** (build Rust core for full speed) |
| Maximum headless throughput | **`--vic-emulation fast`** |
| VICE parity / BA stalls | **`accurate-python`** or **`--accurate-vic`** |

## Related code

- `C64.py`: resolves **`vic_emulation`**; deprecated **`--accurate-vic`** → **`accurate-python`**.
- `emulator.py` / `cpu.py`: **`vic_emulation`** → **`accurate_vic`**, **`rust_hybrid_vic`**, **`CPU6502`** construction.
- `resid.py`: **`ReSIDEmulator(..., cpu_lockstep=accurate_vic)`**.
- `memory.py`: **`vic_render_snapshots`**, **`vic_snapshot_each_emulated_frame`**.

See [performance.md](performance.md) for benchmark commands and the **graphics + ReSID + turbo** regression canary.
