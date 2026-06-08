# c64py TODO

Cross-cutting items grouped by area. Each entry links to a doc under `docs/`
(or `.windsurf/plans/`) with the detailed design. Keep entries short — one
bullet per topic.

Status legend:
- `[ ]` pending
- `[w]` in progress
- `[X]` done

> **For the 1541** specifically, the authoritative status, gap analysis, and
> forward plan live in `docs/1541_status_and_plan.md`. Read that before
> picking up any drive work.

## Video rendering

- [ ] **`per-cycle` rendering tier** — CLI/config accept `per-cycle` and map to
  `per-raster` with a stderr note until the cycle sampler + pixel walk from
  `docs/per_cycle_vic.md` land. Harness: `scripts/render_n_frames.py`.
- [X] **Per-raster beam background** — row-dispatched text/bitmap; sprites use end-of-frame latch (per-row sprite DMA needs per-cycle video).
  in `graphics.py` samples sprites from the same beam VIC/CIA2 row as text/bitmap;
  sub-scanline sprite motion still needs the full per-cycle renderer.
- [ ] **Confirm Arkanoid render with `per-raster`** — initial report had bricks
  bleeding into the score panel under the default `per-frame` tier. If the
  glitch persists with `--video-rendering per-raster` (or `--accurate`),
  capture a snapshot and feed it to `scripts/snapshot_render_test.py` for
  diagnosis; that case becomes the per-cycle tier's first regression target.

## VIC-II emulation

- [ ] **NTSC `accurate-rust` parity** — `--vic-emulation accurate-rust` only
  drives PAL today; NTSC silently falls back to the Python accurate path
  (see `C64.py` `--vic-emulation` help text). Port the NTSC cycle table
  to `c64_vicii.rs`.
- [ ] **Coarse-raster mode raster-IRQ accuracy** — `cpu.py:_advance_raster`
  now triggers raster IRQs (was missing before, see commit `cf09f29`),
  but only at line boundaries with no sub-line precision. `accurate-vic`
  remains the authoritative path for raster-tight code.

## Rust core

- [ ] **Cleanup `cargo` warnings** — 19 warnings on every build (unused `mem`
  parameters in `dex/dey/inx/iny/asl_acc/lsr_acc/rol_acc/ror_acc/tax/tay/`
  `txa/tya/tsx/txs`, dead `lda_absx`, useless `result >= 0` comparisons in
  `execute_opcode_match.rs:129,573`). Either silence with `_mem` /
  `#[allow(...)]` or remove the dead code.
- [ ] **Audit remaining arithmetic for debug-build overflow** — commit
  `cf09f29` fixed `u8 + u8` panics in zero-page indexed addressing. A
  pass through the rest of `cpu_ops_generated.rs` and
  `execute_opcode_match.rs` for any other bare `+` on narrow integers
  would prevent the next freeze. Suggested: `cargo build` with
  `RUSTFLAGS=-C overflow-checks=on` and exercise a long-running game.

## 1541 / disk

- [ ] **Bit-level IEC + GCR head (`accurate-python` tier)** — the big one.
  Plan to be authored at `.windsurf/plans/disk-support-rework-1758d2.md`.
  Scope and rationale in `docs/1541_status_and_plan.md` §3-§4.
- [ ] **Rust drive port (`accurate-rust` tier)** — depends on the bit-level
  work. See `docs/1541_status_and_plan.md` §4.1 B8 and `docs/rust_core.md`.
- [X] **Multi-drive auto-spawn** — `--disk2` / `--disk3` with primary D64
  auto-spawn drives 9–10. See `docs/1541_status_and_plan.md` §5.1.
- [ ] **REL `RECORD#` / full REL write** — `read_rel_file` + `,R` loads; SEQ/USR
  and `,S`/`,U` via fast path implemented; REL save still out.
  See `docs/1541_status_and_plan.md` §5.2.
- [X] **Drive status channel TCP test** — `test_tcp_status_reply_includes_implementation`
  exercises `status` RPC (BASIC `OPEN/INPUT#` still optional).
  See `docs/1541_status_and_plan.md` §5.3.
- [X] **VERIFY support** — KERNAL VERIFY compares bytes to RAM (`test_kernal_verify_match_and_mismatch`).
  See `docs/1541_status_and_plan.md` §5.4.
- [ ] **Graphics-mode drive UI (pygame)** — render a 1541 image with a live LED.
  See `docs/drive_emulator.md` §"Graphics mode".

## Input / UX

- [X] **Gamepad support** — add host gamepad support (pygame joystick/gamepad events);
  details in `docs/input_ux.md`.
  and mapping to C64 joystick bits/ports in runtime input.
- [X] **Config editor in `config.py` (pygame)** — add an interactive config editor
  mode in `config.py` that can edit all TOML-backed settings, including keyboard and
  gamepad mappings. Keep config read/write/edit in the same file; initialize pygame
  only when launched explicitly in editor mode. See `docs/config.md`.
- [X] **`F10` turbo toggle** — runtime keybinding to toggle turbo mode; see `docs/input_ux.md`.
- [X] **`F11` fullscreen toggle** — runtime keybinding to toggle window/fullscreen; see `docs/input_ux.md`.
- [X] **`F12` screenshot** — runtime keybinding to save a screenshot; see `docs/input_ux.md`.

## Doc-drift fixes (do alongside the related work)

- [X] `docs/disk_support.md` references `disk-support-rework-1758d2.md` — plan
  added at `.windsurf/plans/disk-support-rework-1758d2.md`.
- [X] `docs/disk_support.md` drive file paths updated (`drives/c1541_emulator.py`).
- [ ] `docs/drive_emulator.md` §"LED semantics" overstates what works today —
  see `docs/1541_status_and_plan.md` §2.3 item 1.
## Done

- [X] **Raster IRQ loop fix + Rust u8 overflow panics** — commit `cf09f29`.
  Stale `vic_interrupt_state` cleared on `$D012` write; `_advance_raster`
  now generates raster IRQs; zero-page indexed addressing uses
  `wrapping_add`; CPU-thread exception handler clears `emulator.running`
  so a panic no longer freezes the UI thread.
- [X] **Drive-emulator split (M0-M6)** — 1541 is now its own subprocess;
  C64 side has zero in-process disk state. See
  `.windsurf/plans/drive-emulator-split-e2fbd2.md` and `progress.txt`.
- [X] **TUI polish + KERNAL error reporting** — TUI ASCII art aligned,
  stdout log leak fixed, blank-disk label, new-disk filename dialog,
  BASIC error codes corrected (4 = FILE NOT FOUND, 5 = DEVICE NOT
  PRESENT). Commits `7d06d69`, `85394c9`.
