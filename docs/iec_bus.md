# IEC serial bus

c64py implements the IEC bus signals (ATN, CLK, DATA) with proper
open-collector semantics and connects the C64's CIA2 port A to up to
four drives (devices 8–11). The byte-level protocol is in place via
a Python "mock drive" responder; standard disk loads still flow
through the KERNAL hook in [`disk_support.md`](disk_support.md) for
performance.

## What works

- IEC bus infrastructure (`iec_bus.py`): open-collector ATN/CLK/DATA,
  multi-device support, peer state visible to CIA2.
- CIA2 port A ↔ IEC wiring at `$DD00` bits 3–7. Polarity follows the
  real C64's 7406 inverters (PRA bit set → bus pulled low).
- Drive container (`drive1541.py`) with DOS/Serial ROMs located by
  `roms.py::find_drive_rom()`.
- **Byte-level IEC protocol** (`iec_bus.py`): TALK / LISTEN /
  UNLISTEN / UNTALK / OPEN / DATA / CLOSE command bytes routed through
  explicit phase state. Used by the Python mock responder for the
  default LOAD path.
- **Mock drive responder** (`drive1541.py`): per-channel `ChannelState`
  accumulates filename, streams bytes from `Drive.load_file(name)`,
  EOI handling, file-not-found sentinel.
- **KERNAL hook fast path** for LOAD/SAVE so disk I/O remains usable
  without paying real-1541 timing cost.
- **1541 6502 stepping** (Stages 1–5): the drive's DOS ROM runs on
  its own cycle-accurate 6502 with two real VIA 6522 chips
  (`via6522.py`). Drive boot RAM-test completes; ATN delivery
  through the 1541's 7406 inverter raises CA1 → VIA1 IRQ; the drive's
  ATN service handler fires and pulls DATA low. A job-queue trap
  (`drive1541._on_job_queue_write`) services sector reads/writes
  against the attached `D64Image` so we don't need GCR-level
  emulation. The job-queue trap is suppressed inside the boot
  RAM-test PC window so spurious `INC $00` cycles don't look like
  read jobs. Independent VIA-driven IRQ dispatch from `Drive1541.step`
  (the C64 CPU's fast-mode IRQ path is wired to the host CIA1 only).

## What's missing

- **Byte-level IEC handshake after ATN.** Drive boot + ATN ack is
  verified, but the per-byte CLK/DATA edge handshake that follows
  (command frame, secondary address, payload bytes) is not yet
  exercised end-to-end. Until it is, the Python mock responder
  remains the default LOAD path. Tracked by the `xfail` slow test
  `test/test_disk_accurate_e2e.py::test_true_drive_load_named_file_end_to_end`
  (run with `C64PY_RUN_SLOW_TESTS=1`).
- **`disk_emulation` accurate-python / accurate-rust** (TOML or standalone `--emulation`). Reserved
  for the bit-level IEC + GCR head path. Both tiers currently fall
  back to `fast` (job-queue trap + KERNAL LOAD shortcut) until the
  byte-level handshake and GCR controller land. See
  `docs/disk_support.md` for the tier matrix.
- **Channel-15 command bytes through IEC.** `command_buffer` accepts
  bytes on channel 15 in the mock responder, but the parser is not
  wired to consume them on UNLISTEN.
- **SAVE through IEC.** The KERNAL `$FFD8` hook persists into the
  D64 directly; the IEC OPEN-for-write path is not yet exercised
  end-to-end.
- **PETSCII↔ASCII filename conversion** is a plain `decode("ascii")`.
- **Multi-drive simultaneous stepping**, fastloaders, and GCR/raw
  bytestream are explicitly out of scope.

## Recent fixes

The CIA2 ↔ IEC polarity was inverted on both write and read paths
(real hardware uses 7406 inverters between the chip pins and bus
lines). Same applies to PB6/CA1 inside the 1541. Fixed; old tests
that encoded the inverted behaviour have been updated.
