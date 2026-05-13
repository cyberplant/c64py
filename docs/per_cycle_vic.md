# Per-cycle VIC rendering tier (planned)

c64py's `--video-rendering` flag offers two tiers today:

- **`per-frame`** — sample VIC registers once per frame.
- **`per-raster`** — sample VIC registers at each row's top scanline,
  dispatch to text or bitmap renderers per row. This is the default
  and handles split-screen mode/charset/background-color changes
  across the frame.

A third tier — **`per-cycle`** — is implemented for **text**, **bitmap**
(hires + multicolor), and **sprites**: the CPU cycle engine records VIC/CIA2 in a 40×200 sample
grid, and the pygame path composites one scanline per sample. Sprites use the **same**
per-column register samples as the background (so mid-raster X/Y/pointer changes show up);
sprite DMA timing and collisions are still not modeled here.

The per-raster path samples VIC state only at row boundaries (every 8
scanlines), so any effect that depends on register changes mid-row
or mid-scanline is lost. Examples that need per-cycle resolution:

- **FLI** (Flexible Line Interpretation) — color RAM is reloaded on
  every scanline by toggling `$D011` to force bad lines, producing
  near-arbitrary per-line color.
- **Open sideborder / open top-border** tricks — clearing
  `$D016.CSEL` or `$D011.RSEL` in a precise cycle window inside the
  border.
- **Per-line color bars / DYCP** — programs that update `$D020`,
  `$D021`, or `$D022..$D024` in a tight raster loop to produce
  smooth gradients along a single character row.
- **Sub-row charset / `$D018` swaps** — switching the character
  pointer between the upper and lower halves of an 8-line text row,
  effectively producing a 4-line tall row of glyphs from a different
  charset.

## Design sketch

The per-raster pipeline is structured so that a per-cycle tier can be
added additively without rewriting the existing renderers:

1. **Cycle-resolved VIC sampler.** Replace the per-row VIC snapshot
   with one snapshot per emulated cycle inside the visible window
   (40 cycles/line × 200 lines = 8000 snapshots/frame for a 25-row
   display). Memory cost is bounded; the per-row dispatcher already
   stores a per-line snapshot, so this is roughly a 7× expansion of
   that buffer.

### Geometry and buffers

The B1 foundation is checked into `video_beam.py` and `memory.py`:

| Standard | Frame lines | Cycles/line | Content raster window | Content cycle window | Samples/frame |
|---|---:|---:|---:|---:|---:|
| PAL | 312 | 63 | 51..250 | 14..53 (0-based VIC cycle) | 8000 |
| NTSC | 263 | 65 | 51..250 | 14..53 (0-based VIC cycle) | 8000 |

`video_beam.per_cycle_geometry()` returns these bounds and maps a
`(raster_line, raster_cycle)` pair to a visible sample index, or `None`
outside the 320x200 content area. `MemoryMap.ensure_per_cycle_buffers()`
allocates matching VIC and CIA2 **flat** bytearrays (`per_cycle_vic_flat`,
`per_cycle_cia2_flat`) for the renderer (and a future native compositor).

**B2 sampler:** when `MemoryMap.per_cycle_render_enabled` is true,
`MemoryMap.per_cycle_capture_vic_sample()` runs at the end of each
`CPU6502._vic_tick_one()` (accurate VIC path only). It copies the current
VIC shadow (`$D000`–`$D03F`) and CIA2 port A into the slot for the current
`(raster_line, raster_cycle)` when that pair lies in the visible window.
Fast VIC mode does not call `_vic_tick_one`, so per-cycle buffers stay
empty unless accurate VIC is enabled.

**B4 (CLI):** `--video-rendering per-cycle` turns on `per_cycle_render_enabled`, primes buffers
at startup, and (when `c64py_rust_core` is built) prefers **`--vic-emulation accurate-rust`**
with hybrid VIC so `run_fast_batch` fills the flat buffers each visible cycle; use
`C64PY_RUST_PER_CYCLE=0` to keep sampling in Python. Without the Rust extension, per-cycle
still requires **`accurate-python`** VIC so `_vic_tick_one` runs each cycle.

**B3 (text + bitmap + sprites):** `PygameInterface._render_frame_per_cycle` (or
`_core.composite_per_cycle_frame` when enabled) reads the sample grid and draws hires /
multicolor / ECM text, **hires / multicolor bitmap**, and **sprites**. Per-column samples
drive sprite **X**, **enable**, and video-matrix context; **$D017 / $D01D / $D01C / $D01B** and
sprite colours **$D025–$D02E** are taken from the **first visible cycle** of each raster line
(column 0) so one sprite is not torn across mixed expansion/palette bytes when the KERNAL
changes those registers between character cycles. **Sprite X/Y expansion** is honoured
(48×42 screen extent from 24×21 data). Sprite-sprite order matches silicon (**sprite 0 in
front of sprite 7**; compositor draws 7 → 0). ``$D01B`` sprite/background priority is
approximated for opaque foreground pixels in hires and multicolor text/bitmap rows. True
BA/DMA timing is still approximated compared to silicon.

5. **Sprite DMA / finer priority.** Further align sprite drawing with VIC internal fetch
   buffers, multiplex edge cases, and border/latch timing beyond the current foreground mask
   and line-latched attribute registers.

6. **Gating and golden tests.** `per-cycle` is opt-in via `--video-rendering`
   / TOML. The first regression target is a frame where per-raster output
   differs from a known-good reference; `per-cycle` should converge there.
   `scripts/render_n_frames.py` supports `--video-rendering per-cycle` for
   snapshot-based frame hashes.

7. **Performance.** With the Rust extension, hybrid VIC + **native compositor** (`_core.composite_per_cycle_frame`,
   on by default; `C64PY_RUST_COMPOSITE=0` for Python drawing) makes per-cycle usable for
   interactive play. Optional `C64PY_PER_CYCLE_NO_SPRITES=1` skips sprite overlay. Remaining
   cost is dominated by anything still on the Python path (e.g. IEC stepping, debugging).

## When to implement

When a program in your library exhibits a rendering anomaly that the
per-raster path cannot reproduce — typically a flicker, a stripe, or
a sub-row visual effect that disappears when the renderer is sampled
at row boundaries.

## Worker handoff

For **milestones, buffer sizing, parallel B3a/B3b/B3 splits, and CI golden
frames**, use the consolidated plan:
[`docs/plans/release_blockers_iec_percycle_vic.md`](plans/release_blockers_iec_percycle_vic.md)
(workstream B).
