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

**B4 (CLI):** `--video-rendering per-cycle` (or `video.rendering = "per-cycle"`
in TOML) turns on `per_cycle_render_enabled`, primes buffers at startup, and
forces `--vic-emulation accurate-python` when needed so `_vic_tick_one` runs
each cycle. `--accurate` normally selects per-raster + accurate-rust; if
you combine it with per-cycle (CLI or merged config), VIC stays on
accurate-python so sampling works.

**B3 (text + bitmap + sprites):** `PygameInterface._render_frame_per_cycle` reads the
sample grid and draws hires / multicolor / ECM text, **hires / multicolor bitmap**, and
**sprites** (per 8-pixel column from the sample at that cycle). Sprite-sprite order matches
silicon (**sprite 0 in front of sprite 7**; compositor draws 7 → 0). ``$D01B``
sprite/background priority is approximated for opaque foreground pixels in hires and
multicolor text/bitmap rows (hires bitmap: per-bit ``1`` pixels block behind-sprites). True
BA/DMA timing is still approximated compared to silicon.

5. **Sprite DMA / finer priority.** Further align sprite drawing with VIC fetch timing and
   border/latch edge cases beyond the current ``$D01B`` foreground mask.

6. **Gating and golden tests.** `per-cycle` is opt-in via `--video-rendering`
   / TOML. The first regression target is a frame where per-raster output
   differs from a known-good reference; `per-cycle` should converge there.
   `scripts/render_n_frames.py` supports `--video-rendering per-cycle` for
   snapshot-based frame hashes.

7. **Performance.** The dominant cost is **`--vic-emulation accurate-python`**
   (full Python VIC cycle step per CPU cycle). Host compositing is secondary but
   was improved by: (a) writing only the **flat** `per_cycle_vic_flat` /
   `per_cycle_cia2_flat` in the sampler — no per-slot `bytes(64)` allocations
   (~8000/frame); (b) **bulk priming** of those flats at startup; (c) an LRU
   **glyph row cache** in the pygame path for repeated screen codes; (d) optional
   `C64PY_PER_CYCLE_NO_SPRITES=1` to skip the per-column sprite overlay when you
   only care about background splits. Expect per-cycle to remain much slower than
   per-raster for interactive use; use per-raster for daily play. A future win is
   one **native compositor** pass per frame over the flat buffers (see
   `docs/rust_core.md`); that does not remove the Python VIC stepping cost unless
   sampling moves to Rust too.

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
