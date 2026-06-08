# Disk and 1541 support

> **Audience.** Single source of truth for D64 handling, the TCP drive split,
> emulation tiers, and forward plan. The standalone 1541 server is documented
> in [drive_emulator.md](drive_emulator.md); third-party TCP firmware in
> [tcp_hardware_drive.md](tcp_hardware_drive.md).

---

## 1. Architecture

The 1541 is a **separate process** (`python -m c64py.drives.c1541_emulator`).
The C64 emulator holds **no `DiskDrive`, no `D64Image`, and no 1541 CPU state**.
All disk bytes and DOS logic live in the drive subprocess; the C64 side speaks
JSON over TCP via [`TcpDriveClient`](../drives/tcp_drive_client.py).

```text
┌─────────────────────────┐      JSON/TCP       ┌────────────────────────────┐
│  C64.py / emulator.py   │ ◀────────────────▶  │  c1541_emulator            │
│                         │  fast_load/save     │  (subprocess)              │
│  TcpDriveClient         │  attach_disk        │                            │
│  IECBus + CIA2          │  status / step      │  Drive1541 (6502+VIAs+ROM) │
│  KERNAL $FFD5/$FFD8     │                     │  DiskDrive → D64Image      │
│  kernal_tcp_iec_hooks   │                     │  job-queue trap (fast)     │
│  (optional wire decode) │                     │  text_ui (optional TUI)    │
└─────────────────────────┘                     └────────────────────────────┘
```

**“Local” disk in `C64.py game.d64`** does **not** mean in-process emulation.
[`_spawn_local_drive`](../emulator.py) launches a **headless** `c1541_emulator`
child on `127.0.0.1`, waits for a free port, and attaches it with
[`attach_tcp_drive`](../emulator.py) — same TCP path as `--tcp-drive`.

### 1.1 What lives on the C64 side (host only)

| Component | Role |
|---|---|
| [`TcpDriveClient`](../drives/tcp_drive_client.py) | TCP client: `fast_load`, `fast_save`, `attach_disk_remote`, `status`, IEC backend hooks that forward logical bytes as JSON. |
| [`IECBus`](../iec_bus.py) + CIA2 `$DD00` | Wired-AND ATN/CLK/DATA when KERNAL bit-bangs the serial bus. |
| [`KernalIecTap`](../iec_kernal_bridge.py) | Records CIA2 line transitions; optional wire decoder when `C64PY_IEC_WIRE_DECODE=1`. |
| [`kernal_tcp_iec_hooks`](../kernal_tcp_iec_hooks.py) | Fast-path OPEN / CHKIN / CHKOUT / CLOSE / BSOUT over TCP when KERNAL shortcuts are enabled. |
| `_handle_kernal_load` / `_handle_kernal_save` | `$FFD5` / `$FFD8` → `fast_load` / `fast_save` RPC → bytes written into C64 RAM. |
| `_spawn_local_drive` | Convenience: spawn headless drive subprocess + TCP connect (not in-process disk). |
| `_step_iec_drives` | Calls `TcpDriveClient.step()` (drains socket replies; remote server runs its own loop). |

There is **no** `Drive1541`, **no** sector I/O, and **no** D64 parser inside
`emulator.py`. Shared library code [`d64.py`](../d64.py) and
[`drives/drive.py`](../drives/drive.py) are used by the **drive process** and
tests, not by the C64 core loop.

### 1.2 What lives in the drive process

| Component | Role |
|---|---|
| [`c1541_emulator.py`](../drives/c1541_emulator.py) | asyncio JSON server, CLI, `Drive1541` lifecycle. |
| [`Drive1541`](../drives/c1541_emulator.py) | Real 1541 6502 + two VIA 6522 chips + DOS ROM. |
| [`DiskDrive`](../drives/drive.py) | D64 attach, directory, `fast_load`/`fast_save`, status channel, command channel parser. |
| [`D64Image`](../d64.py) | 35-track image I/O, BAM, file read/write. |
| Job-queue trap (`fast` tier) | Intercepts DOS ZP job writes; serves sectors from `D64Image` without GCR head. |

---

## 2. Connecting a drive

| Method | Example |
|---|---|
| Manual TCP | Terminal 1: `python -m c64py.drives.c1541_emulator --disk game.d64 --device 8 --port 6408` — Terminal 2: `python C64.py --tcp-drive 8:localhost:6408` |
| Multiple drives | Spawn one `c1541_emulator` per device (8–11), then `python C64.py --tcp-drive 8:localhost:6408 --tcp-drive 9:localhost:6409` |
| Auto-spawn (drive 8 only) | `python C64.py game.d64` — spawns one headless drive 8, connects via TCP. |
| Control server | `ATTACH-DISK path.d64 [device]` / `DETACH-DISKS` in [`command_dispatch.py`](../command_dispatch.py) → `attach_disk` RPC. |

See [drive_emulator.md](drive_emulator.md) for `--new-disk`, interfaces (`headless` / `text`), and the JSON protocol.

---

## 3. Emulation tiers

Set via `[emulation] disk_emulation` in `c64py.toml` or
`c1541_emulator --emulation`. The `--accurate` shortcut on `C64.py` changes
**VIC/video** defaults only; it does **not** change `disk_emulation`.

| Tier | Where it runs | Disk surface | KERNAL `$FFD5`/`$FFD8` on C64 | Today |
|---|---|---|---|---|
| `fast` (default) | Drive subprocess | Job-queue trap → `D64Image` | Shortcut ON (TCP `fast_load`/`fast_save`) | **Shipped** |
| `accurate-python` | Drive subprocess | Real GCR head + bit-level IEC (planned) | Shortcut OFF when no TCP client | **Placeholder** — drive still uses `fast` internally with a warning |
| `accurate-rust` | Drive in Rust (planned) | Same as accurate-python | Same | **Placeholder** — falls back to accurate-python |

On the C64 side, `kernal_load_shortcut_enabled` stays **ON** whenever any
`TcpDriveClient` is attached (including auto-spawned headless servers), because
LOAD/SAVE over TCP uses the fast RPC path. Disabling the shortcut without a
working IEC bridge would hang KERNAL serial routines.

Optional **`C64PY_IEC_WIRE_DECODE=1`**: KERNAL CIA2 edges are decoded into
logical `IECBus` commands and forwarded to TCP (`test/test_iec_tcp_wire_integration.py`).
This is **partial** (OPEN/LISTEN/byte phases); not all BASIC disk I/O paths
are covered without the shortcut hooks in [`kernal_tcp_iec_hooks.py`](../kernal_tcp_iec_hooks.py).

---

## 4. What works end-to-end

### Drive subprocess (`d64.py`, `drives/drive.py`, `c1541_emulator.py`)

- **D64 parser / write-back** — 35-track images, BAM, directory, chained sectors,
  `write_file`, scratch/rename/no-op format commands on the command channel.
- **Job-queue trap** (`fast` tier) — DOS ZP jobs served from `D64Image`.
- **`fast_load` / `fast_save` RPC** — used by C64 KERNAL hooks; supports PRG,
  directory (`"$"`), `,S`/`,U` suffixes, VERIFY metadata via `dos_filetype`.
- **Status RPC** — `"NN,MESSAGE,TT,SS"` from `Drive.get_status()`.
- **Multi-device routing** — `--tcp-drive 8:… --tcp-drive 9:…` on the C64 side;
  each server binds one device number.

### C64 host

- **`LOAD"NAME",8` / `LOAD"$",8`** — KERNAL hook → TCP → RAM; end addresses and
  directory pointers updated.
- **`SAVE"NAME",8`** — KERNAL hook → TCP → D64 persisted on drive side.
- **VERIFY** — `$FFD5` with `A≠0` compares file bytes to RAM
  (`test/test_d64_filetypes.py::test_kernal_verify_match_and_mismatch`).
- **TCP IEC hooks** — OPEN / PRINT# / INPUT# / CLOSE for attached TCP drives when
  shortcuts are enabled (`kernal_tcp_iec_hooks`, covered by integration tests).
- **Wire decode (opt-in)** — `C64PY_IEC_WIRE_DECODE=1` for CIA2 → logical IEC → JSON
  (`iec_wire_decode.py`, `test/test_iec_wire_decode.py`).
- **Auto-reconnect** — 5 s backoff on TCP drop (`TcpDriveClient.RECONNECT_DELAY`).

### UI

- **Textual drive TUI** — `c1541_emulator --interface text`.
- **C64 textual mode** — per-device activity indicators (TCP `led_on` is always
  `False`; real LED only reflects VIA activity inside the drive process when the
  6502 actually runs I/O, not during `fast_load` RPC).

Tests: `test/test_d64_write.py`, `test/test_drive_status.py`,
`test/test_disk_integration.py`, `test/test_fast_load_rpc.py`,
`test/test_kernal_load.py`, `test/test_iec_tcp_wire_integration.py`, etc.

---

## 5. Gaps and limitations

### UX / correctness

1. **LED dark during `fast_load`/`fast_save`.** RPC bypasses the 1541 CPU → VIA2
   PB3 never toggles. Honest fix: bit-level path (§6) so DOS runs for real loads.
2. **REL save / full `RECORD#`** — REL read exists; REL write and full REL semantics remain open.
3. **Real `N0:` format** — parses as no-op (BAM not reset).
4. **Drives 9–11 under simultaneous load** — routing exists; heavy multi-drive
   arbitration is lightly tested.
5. **Fastloaders / copy protection** — need accurate IEC + GCR (§6).

### C64 / TCP bridge

6. **Mid-batch IEC on Rust CPU path** — peer CLK/DATA snapshotted at batch start;
   see [rust_core.md](rust_core.md) batch limitations when mixing disk I/O with
   large Rust batches.
7. **Without KERNAL shortcuts or wire decode**, OPEN/PRINT# over `--tcp-drive`
   can stall in KERNAL serial code — use auto-spawn + shortcuts, enable wire
   decode, or restrict remote workflows to LOAD/SAVE until accurate tier ships.

---

## 6. Bit-level rework (forward plan)

The largest outstanding piece: make **`accurate-python`** / **`accurate-rust`**
mean real IEC edges + GCR head, KERNAL shortcuts off, LED and fastloaders honest.

**Detailed milestones:** [`.windsurf/plans/disk-support-rework-1758d2.md`](../.windsurf/plans/disk-support-rework-1758d2.md)

Summary:

| Milestone | Goal |
|---|---|
| B0 | Plan authoring (**done** — plan file above) |
| B1 | Edge-level `iec_bus.py` (wired-AND, golden traces) |
| B2 | Drive VIA1 + C64 CIA2; disable JSON byte shortcuts on accurate-python |
| B3 | GCR codec (`drives/gcr.py`) |
| B4 | GCR head (VIA2 PA/PB, motor, stepper, LED from PB3) |
| B5 | Disable KERNAL shortcut for non-`fast` tiers (when IEC path works) |
| B6 | LED regression under accurate-python |
| B7 | Fastloader smoke test |
| B8 | Rust drive port (`accurate-rust`) |
| B9 | Multi-drive IEC accuracy |
| B10 | Docs + changelog |

**Definition of done:** LOAD/SAVE through real KERNAL serial + GCR round-trip;
LED visible in drive TUI; representative fastloader loads; new tests
(`test_iec_bitlevel`, `test_gcr_codec`, `test_gcr_head_roundtrip`, …).

References: VICE `src/drive/iec*.c`, *Inside Commodore DOS*, 1541 schematic
(VIA2 PA = head data, PB3 = write enable + LED).

---

## 7. Smaller independent tasks

Can ship without §6:

| Task | Notes |
|---|---|
| REL save / `RECORD#` | `d64.py` + `DiskDrive`. |
| BASIC status channel over IEC | `OPEN 1,8,15:INPUT#1,…` end-to-end test (RPC status exists). |
| Graphics drive UI | Pygame 1541 window — see [drive_emulator.md](drive_emulator.md). |

---

## 8. Resuming work

1. `git log --oneline -10` — see what shipped.
2. Read **this file** and [drive_emulator.md](drive_emulator.md).
3. Pick from [TODO.md](../TODO.md) §1541 / disk or milestone B1+ in the bit-level plan.
4. Test slice: `pytest -q test/test_drive*.py test/test_disk*.py test/test_kernal*.py test/test_iec*.py`
5. Full suite: `pytest -q --ignore=test/test_all_vice.py --ignore=test/test_vice.py`

---

## 9. Glossary

- **D64** — 174,848-byte logical 1541 sector dump (no on-disk GCR).
- **GCR** — 4-to-5 bit channel encoding on real media.
- **IEC** — ATN + CLK + DATA serial bus.
- **Job-queue trap** — Intercepts 1541 ZP `$00–$05` job posts; `fast` tier shortcut.
- **KERNAL shortcut** — C64 hooks at `$FFD5`/`$FFD8` → TCP RPC, skipping DOS serial I/O.
- **Auto-spawn** — Headless drive subprocess + localhost TCP; not in-process disk.

---

## Changelog

- **2026-05**: consolidated 1541 status and forward plan into this file; corrected
  architecture (TCP-only on C64 side; no in-process `DiskDrive`); documented
  wire decode and VERIFY; pointed bit-level plan to
  `.windsurf/plans/disk-support-rework-1758d2.md`.
- **2026-04**: tier rename (`fast` / `accurate-python` / `accurate-rust`);
  drive split (M0–M6) completed — 1541 runs as TCP server subprocess.
