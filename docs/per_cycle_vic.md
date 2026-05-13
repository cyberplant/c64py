# Per-cycle VIC rendering tier (planned)

c64py's `--video-rendering` flag offers two tiers today:

- **`per-frame`** — sample VIC registers once per frame.
- **`per-raster`** — sample VIC registers at each row's top scanline,
  dispatch to text or bitmap renderers per row. This is the default
  and handles split-screen mode/charset/background-color changes
  across the frame.

A third tier — **`per-cycle`** — is filed as future work. The
per-raster path samples VIC state only at row boundaries (every 8
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
allocates matching VIC and CIA2 snapshot grids plus flat bytearrays for
future renderer/Rust handoff; it does not render or sample yet.

**B2 sampler:** when `MemoryMap.per_cycle_render_enabled` is true,
`MemoryMap.per_cycle_capture_vic_sample()` runs at the end of each
`CPU6502._vic_tick_one()` (accurate VIC path only). It copies the current
VIC shadow (`$D000`–`$D03F`) and CIA2 port A into the slot for the current
`(raster_line, raster_cycle)` when that pair lies in the visible window.
Fast VIC mode does not call `_vic_tick_one`, so per-cycle buffers stay
empty unless accurate VIC is enabled.

2. **Pixel-by-pixel renderer.** Walk left-to-right within each
   scanline emitting one pixel at a time using the cycle-correct VIC
   state. Standard text, multicolor text, ECM, hires bitmap and
   multicolor bitmap renderers all reduce to "given current VIC
   state and the next 8 bits of the c-access byte, emit pixels".
   Sprites overlay on top using the same per-cycle state.

3. **Gating.** Make `per-cycle` opt-in via the existing
   `--video-rendering` flag. The first regression target is a frame
   where the per-raster output differs from a known-good reference;
   `per-cycle` should converge to the reference. Use
   `scripts/render_n_frames.py` as the harness — it already produces
   reproducible frame hashes from a snapshot.

4. **Performance.** Expect the per-cycle path to be 5–10× slower than
   per-raster. It is acceptable as a correctness mode, not the
   default. Consider a Rust implementation in `c64py-core` once the
   Python prototype is stable.

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
