# c64py TODO

Outstanding work grouped by area. Detailed design lives in `docs/` and
`.windsurf/plans/`. Keep entries short — one bullet per topic.

> **1541 / disk:** authoritative status and forward plan in
> [`docs/disk_support.md`](docs/disk_support.md).

## Video rendering

- [ ] **Per-cycle golden frame regression** — committed expected hash(es) where
  per-raster ≠ per-cycle; see
  [`docs/plans/release_blockers_iec_percycle_vic.md`](docs/plans/release_blockers_iec_percycle_vic.md)
  (B6) and [`docs/per_cycle_vic.md`](docs/per_cycle_vic.md).

## VIC-II emulation

- [ ] **Coarse-raster mode raster-IRQ accuracy** — `cpu.py:_advance_raster`
  triggers raster IRQs at line boundaries only (no sub-line precision).
  `accurate-python` / `accurate-rust` remain the paths for raster-tight code.

## Rust core

- [ ] **Cleanup `cargo` warnings** — unused `mem` parameters, dead `lda_absx`,
  useless comparisons in `execute_opcode_match.rs`. See `docs/rust_core.md`
  remaining work.
- [ ] **Audit remaining arithmetic for debug-build overflow** — pass through
  `cpu_ops_generated.rs` / `execute_opcode_match.rs` with
  `RUSTFLAGS=-C overflow-checks=on`.

## 1541 / disk

- [ ] **Bit-level IEC + GCR head (`accurate-python` tier)** — plan:
  [`.windsurf/plans/disk-support-rework-1758d2.md`](.windsurf/plans/disk-support-rework-1758d2.md);
  scope in [`docs/disk_support.md`](docs/disk_support.md) §6.
- [ ] **Rust drive port (`accurate-rust` tier)** — depends on bit-level work;
  [`docs/disk_support.md`](docs/disk_support.md) §6 (B8), [`docs/rust_core.md`](docs/rust_core.md).
- [ ] **REL `RECORD#` / full REL write** — REL read via `,R` works; save and
  full `RECORD#` semantics remain. [`docs/disk_support.md`](docs/disk_support.md) §7.
- [ ] **Graphics-mode drive UI (pygame)** — 1541 image with live LED;
  [`docs/drive_emulator.md`](docs/drive_emulator.md).

## IEC (TCP)

- [ ] **KERNAL → logical IEC → JSON over `--tcp-drive`** — wire decode is
  partial; full OPEN/PRINT#/INPUT# without KERNAL shortcuts tracked in
  [`docs/plans/release_blockers_iec_percycle_vic.md`](docs/plans/release_blockers_iec_percycle_vic.md)
  (workstream A).
