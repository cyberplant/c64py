# Disk drive support

c64py supports D64 disk images. All tiers run the real 1541 6502 + DOS
ROM; tiers differ in how much of the disk-controller and IEC-bus
hardware is actually emulated.

## Tiers (`[emulation] disk_emulation` / `c1541_emulator --emulation`)

| Tier | Drive 6502 | IEC bus | Disk surface | KERNAL `$FFD5`/`$FFD8` shortcut | Speed |
|---|---|---|---|---|---|
| `fast` (default) | Real 1541 6502 in Python | Bytes synthesized; ATN handshake stub | Job-queue trap → `D64Image` sector I/O | Yes (instant LOAD) | Practical full speed |
| `accurate-python` | Real, in Python | Full bit-level (planned: ATN/CLK/DATA edges, EOI, talk/listen) | Real GCR head: SYNC/PB7, density, stepper, GCR codec | None | WIP, falls back to `fast` until M2 lands |
| `accurate-rust` | Real, in Rust | Same as `accurate-python`, in Rust | Same, in Rust | None | WIP, falls back to `accurate-python` then `fast` |

The `--accurate` shortcut on `C64.py` sets aggressive **VIC + video** defaults only; it does not change `disk_emulation` (set that in TOML or on the standalone drive).

## What works

- **D64 parser** (`d64.py`): 35-track 1541 images, BAM, directory, file
  reads. Directory listings are formatted as the C64 would display them.
- **D64 write-back** (`d64.py`): `D64Image.write_file` allocates free
  sectors via the BAM, chains data sectors, inserts a directory entry
  (linking a new directory sector when the chain is full), and persists
  via `save_to_file`. `_free_sector` is available for delete/rename.
- **Drive emulation** (`drives/c1541_emulator.py`, `drives/drive.py`): real
  1541 6502 + DOS ROM running in a subprocess (TCP) or test harness. Devices
  8–11 supported simultaneously.
- **CLI**: pass a `.d64` file as the positional argument to attach on
  startup; `LOAD"$",8` is auto-injected after BASIC is ready. Optional
  `--disk2` / `--disk3` paths auto-spawn drives 9 and 10 when the primary
  image is auto-spawned (no `--tcp-drive`). To **create** a new image for
  the standalone drive process, use
  ``python -m c64py.drives.c1541_emulator --new-disk path.d64 …`` (see
  `docs/drive_emulator.md`).
- **Server commands** (`server.py`): `ATTACH-DISK <file.d64> [device]`,
  `DETACH-DISKS`.
- **Job-queue trap** (`drives/c1541_emulator.py`, `fast` tier): when DOS writes a
  job code to `$00-$05`, the trap synthesizes the read/write directly
  from the attached `D64Image`, bypassing GCR + bit-level disk I/O.
- **KERNAL LOAD shortcut** (`fast` tier, or whenever no drive / TCP client is
  attached): intercepts LOAD for devices 8-11, services the file from the D64
  via the drive helper, and writes data straight into C64 RAM.
  Handles `LOAD"$",8` (directory), `LOAD"NAME",8,1` (named PRG), optional
  `,P`/`,S`/`,R`/`,U` filename suffixes, VERIFY (`A=1` at `$FFD5`), and
  `dos_filetype` metadata from the TCP `fast_load_reply`.
- **KERNAL SAVE shortcut** at `$FFD8` (`fast` tier only): `SAVE"NAME",8`
  writes a PRG into the attached D64 and persists the image. Sets
  `FILE EXISTS` / `DISK FULL` / `WRITE PROTECT ON` via the drive
  status channel on failure.
- **Status / command channel** (`drive.py`): `Drive.get_status()`
  reports a Commodore-DOS-style `NN,MESSAGE,TT,SS` string driven by
  `last_error`; `set_error(code)` looks up canonical messages.
  `command_channel_write(line)` parses `I` / `V` / `S0:NAME[,…]`
  (scratch) / `R0:NEW=OLD` (rename) / `N0:NAME,ID` (no-op format).
- **Drive activity LED**: VIA2 PB3 (the actual 1541 LED line) is
  exposed via `Drive1541.led_on`. Both UIs render one indicator per
  attached device (8-11): textual mode shows `8● 9○ …` next to the
  floppy emoji; graphics mode stacks small red squares in the
  top-right corner.

Tested with `test/test_d64_write.py`, `test/test_drive_status.py`,
`test/test_disk_integration.py`, `test/test_disk_accurate_e2e.py`,
and `test/test_drive_led.py`.

## What's missing

- **Bit-level IEC + GCR head (M2).** The `accurate-python` tier
  currently falls back to `fast` until the wire-level IEC handshake
  and the GCR read/write head land. Tracking: plan
  [`.windsurf/plans/disk-support-rework-1758d2.md`](../.windsurf/plans/disk-support-rework-1758d2.md)
  (authoring milestone B0 before implementation).
- **Rust drive port (M3).** `accurate-rust` will move the 1541 6502 +
  VIAs + GCR head into the Rust core (mirroring `--vic-emulation
  accurate-rust`). Until then it falls back to `accurate-python`.
- **REL / SEQ / USR file types.** SEQ and USR are supported via `d64.py` /
  `DiskDrive` and `,S`/`,U` filename suffixes; REL read via side-sector layout
  is implemented for `read_rel_file`; REL save and full `RECORD#` semantics
  remain future work.
- **Real disk format (`N0:`)**. Currently a no-op.
- **Drive 9–11 simultaneous stepping.** Currently only device 8 is
  fully exercised. Multi-device coordination would need independent
  job-queue dispatch per drive while maintaining IEC bus arbitration.
- **Fastloaders and non-standard protocols.** The `fast` tier handles
  standard `LOAD` / `SAVE`. Custom loaders that bit-bang the IEC bus
  directly (bypassing KERNAL vectors) need `accurate-python` /
  `accurate-rust` once those land.

## Changelog

- **2026-04**: tier rename. Removed legacy `--disk-emulation fast`
  (KERNAL hooks + virtual `DiskDrive`) and `--disk-emulation
  semi-accurate` (Python IEC responder). The previous `accurate`
  tier is now the new `fast` default. Two placeholder tiers
  `accurate-python` and `accurate-rust` were added for the in-flight
  bit-level path; both currently fall back to `fast` with a warning.
