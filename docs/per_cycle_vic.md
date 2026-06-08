# Per-cycle VIC rendering tier

c64py's `--video-rendering` flag offers three tiers:

| Tier | Sampling | Best for |
|---|---|---|
| **`per-frame`** | VIC registers once per emulated frame | Default throughput; stable latch for most games |
| **`per-raster`** | VIC + CIA2 bank at each **row** top scanline (every 8 lines) | Split-screen mode/charset/bank changes across the frame |
| **`per-cycle`** | VIC + CIA2 at each **character cycle** in the 320×200 window (40×200 grid) | FLI, border tricks, sub-row color/charset, mid-line sprite motion |

**`per-cycle` is implemented and opt-in.** It composites **hires / multicolor / ECM text**, **hires / multicolor bitmap**, and **sprites** from the sample grid. Use it when per-raster (row-boundary) sampling misses an effect.

Per-raster samples only at row boundaries, so mid-row or mid-scanline register changes are lost. Examples that benefit from per-cycle:

- **FLI** — color RAM reloaded every scanline via `$D011` bad-line tricks.
- **Open sideborder / open top-border** — precise `$D016.CSEL` / `$D011.RSEL` windows.
- **Per-line color bars / DYCP** — `$D020`, `$D021`, `$D022..$D024` updated inside a row.
- **Sub-row charset / `$D018` swaps** — pointer changes within an 8-line text row.

---

## Using per-cycle

```bash
# Recommended when c64py_rust_core is built (accurate-rust VIC + Rust sampler + compositor)
python C64.py --graphics --video-rendering per-cycle game.prg

# Without Rust extension: accurate-python VIC required for sampling
python C64.py --graphics --video-rendering per-cycle --vic-emulation accurate-python game.prg
```

TOML: `[video] rendering = "per-cycle"` (see [config.md](config.md)).

**VIC timing:** per-cycle buffers are filled only when the CPU advances **cycle-accurate VIC** — `accurate-python`, or **`accurate-rust`** with `c64py_rust_core` built. **`fast`** VIC does not call `_vic_tick_one` / Rust hybrid per-cycle capture, so the grid stays empty.

| Env var | Effect |
|---|---|
| `C64PY_RUST_PER_CYCLE=0` | Keep grid sampling in Python even when Rust core is built |
| `C64PY_RUST_COMPOSITE=0` | Python pygame compositor instead of `_core.composite_per_cycle_frame` |
| `C64PY_PER_CYCLE_NO_SPRITES=1` | Skip sprite overlay (Python path; Rust compositor has `skip_sprites`) |

With the Rust extension, **`--accurate`** + `--video-rendering per-cycle` prefers **accurate-rust** for hybrid VIC sampling and native compositing ([rust_core.md](rust_core.md)).

Harness: `scripts/render_n_frames.py --video-rendering per-cycle`.

---

## Architecture (shipped)

### Geometry and buffers (`video_beam.py`, `memory.py`)

| Standard | Frame lines | Cycles/line | Content raster window | Content cycle window | Samples/frame |
|---|---:|---:|---:|---:|---:|
| PAL | 312 | 63 | 51..250 | 14..53 (0-based VIC cycle) | 8000 |
| NTSC | 263 | 65 | 51..250 | 14..53 (0-based VIC cycle) | 8000 |

`video_beam.per_cycle_geometry()` maps `(raster_line, raster_cycle)` → sample index.
`MemoryMap.ensure_per_cycle_buffers()` allocates `per_cycle_vic_flat` (512000 bytes)
and `per_cycle_cia2_flat` (8000 bytes).

### Sampler

When `MemoryMap.per_cycle_render_enabled` is true:

- **Python path:** `MemoryMap.per_cycle_capture_vic_sample()` at the end of each
  `CPU6502._vic_tick_one()` copies VIC shadow `$D000`–`$D03F` and CIA2 port A into
  the slot for the current visible `(raster_line, raster_cycle)`.
- **Rust path:** `run_fast_batch` with hybrid VIC fills the same flat buffers when
  `C64PY_RUST_PER_CYCLE` is on (default).

### Compositor (`graphics.py`, `rust/c64py-core/src/per_cycle_composite.rs`)

`PygameInterface._render_frame_per_cycle` reads the grid (or calls
`_core.composite_per_cycle_frame` when Rust compositing is enabled).

**Sprite behaviour (approximations documented):**

- Per-column samples drive sprite **X**, enable, and video-matrix context.
- **$D017 / $D01D / $D01C / $D01B** and sprite colours **$D025–$D02E** are taken from
  the **first visible cycle** of each raster line (column 0) to avoid tearing when
  those regs change mid-line.
- Sprite **X/Y expansion** honoured (48×42 from 24×21 data); draw order 7→0 (0 in front).
- **`$D01B`** priority vs opaque foreground in hires/multicolor text and bitmap rows.
- **Not modeled here:** true sprite DMA fetch timing, BA stalls vs silicon, border/latch
  edge cases beyond the foreground mask.

---

## Tests

- `test/test_per_cycle_vic_buffers.py` — buffer layout
- `test/test_per_cycle_vic_sampler.py` — visible-window capture, mid-line `$D011` diff
- `test/test_per_cycle_rust_composite.py` — Rust compositor smoke

---

## Remaining work

1. **Golden frame regression** — committed expected hash(es) where per-raster ≠ per-cycle;
   tracked in [plans/release_blockers_iec_percycle_vic.md](plans/release_blockers_iec_percycle_vic.md) (B6).
2. **Finer sprite / DMA fidelity** — internal fetch buffers, multiplex edge cases, border timing.
3. **Performance profiling docs** — interactive play is usable with Rust compositor; document
   expected slowdown vs per-raster on representative hardware.

---

## Related

- [emulation_modes.md](emulation_modes.md) — VIC timing tiers
- [rust_core.md](rust_core.md) — Rust per-cycle capture and compositor
- [DEBUGGING.md](DEBUGGING.md) — reproduction matrix
