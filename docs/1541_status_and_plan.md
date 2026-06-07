# 1541 emulation — status & forward plan

> **Audience.** A future implementer (human or model) resuming this work
> from a fresh context. Read this top-to-bottom before touching any
> drive code. Cross-references to other docs are real links to files in
> this repo.

---

## 1. TL;DR

The 1541 is now a **standalone TCP-server process** spun up either
manually (`python -m c64py.drives.c1541_emulator …`) or automatically
by `C64.py` when a `.d64` is supplied. The C64 emulator side holds **no
disk state** — it only knows how to speak the JSON protocol defined in
[`docs/drive_emulator.md`](drive_emulator.md) and to short-circuit
KERNAL `$FFD5`/`$FFD8` into a `fast_load`/`fast_save` RPC.

That whole split (M0-M6 of
[`.windsurf/plans/drive-emulator-split-e2fbd2.md`](../.windsurf/plans/drive-emulator-split-e2fbd2.md))
is **done**. `progress.txt` confirms it.

What is **not** done is the *bit-level* path: the `accurate-python`
and `accurate-rust` tiers are placeholders that fall back to `fast`
with a warning. That is the next big piece of work and the bulk of
this document.

---

## 2. Current architecture (as of 2026-04)

```text
┌─────────────────────┐         JSON over TCP            ┌────────────────────────┐
│      C64.py         │  ──── attach_disk / status ───▶  │  c1541_emulator        │
│  emulator.py        │  ──── fast_load / fast_save ──▶  │  (subprocess)          │
│                     │  ◀───────── byte stream ───────  │                        │
│  KERNAL $FFD5 hook ─┼─▶ TcpDriveClient.fast_load       │  Drive1541             │
│  KERNAL $FFD8 hook ─┼─▶ TcpDriveClient.fast_save       │   ├── 6502 + DOS ROM   │
│                     │                                  │   ├── VIAs (1+2)       │
│  No DiskDrive,      │                                  │   ├── DiskDrive helper │
│  no D64Image,       │                                  │   │     (D64 file ops) │
│  no IEC bus state.  │                                  │   └── (TUI) text_ui.py │
└─────────────────────┘                                  └────────────────────────┘
```

### 2.1 Files of interest

| Concern | File | Notes |
|---|---|---|
| C64-side hooks | `emulator.py:1020-1200` | `_handle_kernal_load` / `_handle_kernal_save` — synchronous TCP RPC, sets `A` / `$90` / carry per real KERNAL contract. |
| Auto-spawn helper | `emulator.py` (`_spawn_local_drive`) | Spawns child, waits for port, registers `atexit` cleanup. |
| TCP client | `drives/tcp_drive_client.py` | Auto-reconnect with 5 s backoff. `fast_load` / `fast_save` / `attach_disk_remote` / `detach_disk_remote` / `get_remote_status`. |
| Drive process | `drives/c1541_emulator.py` | argparse, asyncio JSON server, `Drive1541` class (6502+VIAs+DOS ROM), JSON dispatch in `_run_server`. |
| Drive disk helper | `drives/drive.py` (`DiskDrive`) | Sector / file ops over a `D64Image`. Used both inside `Drive1541` (job-queue trap) and directly for `fast_load`/`fast_save`. |
| IEC backend ABC | `drives/iec_backend.py` | Subclassed by `Drive1541`; the **bit-level** path will need a more elaborate subclass. |
| C64-side IEC bus | `iec_bus.py` | Currently mostly bypassed in `fast` mode (the KERNAL shortcut skips it). Needs work for accurate tiers. |
| TUI | `drives/text_ui.py` | Textual app with Static ASCII art, RichLog right pane. |
| BASIC error mapping | `emulator.py:1075-1080, 1191-1194` | 1541 DOS code → BASIC error code (4=FILE NOT FOUND, 5=DEVICE NOT PRESENT). Confirmed via the BASIC ROM error pointer table at `$A328`. |

### 2.2 What works end-to-end today

- **`LOAD"NAME",8`** — fast path, KERNAL hook → `fast_load` RPC → bytes injected into RAM. End address (`$AE`/`$AF`/X/Y) updated, `$2D-$32` updated for directory loads.
- **`LOAD"$",8`** then `LIST` — directory load via the same path (`secondary_addr=0`, filename `"$"`).
- **`SAVE"NAME",8`** — KERNAL hook → `fast_save` RPC → D64 image is updated and persisted on the drive side.
- **Error reporting on the C64 screen** — wrong files, no disk, no drive: BASIC prints the correct
  `?FILE NOT FOUND ERROR` / `?DEVICE NOT PRESENT  ERROR`.
- **Drive status channel** at the protocol layer (the `status` RPC; the drive's `last_error` tuple is exposed as `"NN,MESSAGE,TT,SS"`). End-to-end via `OPEN 1,8,15:INPUT#1,…` over IEC has not been exercised since the rework.
- **Multi-device routing** at the C64 side: `--tcp-drive 8:host:port --tcp-drive 9:host:port` is wired; only auto-spawn is single-drive.
- **Textual TUI** (`--interface text`): ASCII art with LED placeholder, slot fill, disk label, status line, log right pane, keybindings `u` (unload), `r` (replace), `n` (new disk → asks for filename, then confirms, creates blank D64, persists).
- **Auto-reconnect** on TCP drop with 5 s backoff (the C64 side keeps running; LOAD just errors until the drive is back).

### 2.3 What is broken or missing

#### Honest gaps (these directly impact UX)

1. **LED is dark in `fast` mode.** `Drive1541.led_on` reads VIA2 PB3, but `fast_load` / `fast_save` bypass the 6502 entirely → the line never toggles. **Faking** it (timestamp-based pulses) was rejected by the user; the only honest fix is to actually run the drive CPU for these ops, which is the bit-level path (§3 below). The LED works correctly when the drive CPU does run (e.g. plain `LOAD` without the KERNAL shortcut, accurate-python tier).
2. **Multi-drive auto-spawn.** `C64.py --disk foo.d64` only spawns drive 8. Multiple `.d64` paths or new flags `--disk2 / --disk3` are not yet implemented.
3. **REL / SEQ / USR file types** in `d64.py`. PRG only. Affects programs that use `OPEN 1,8,2,"DATA,S,R"` etc.
4. **Real disk format `N0:`**. The command parses but is a no-op (no actual format). Programs that rely on formatting will think it succeeded but the BAM/dir won't be reset.
5. **Drives 9-11 simultaneous stepping.** Only device 8 is exercised under load. The IEC bus arbitration story for multiple active drives is untested.
6. **VERIFY** (`A!=0` on entry to `$FFD5`). The branch exists but is not exercised by tests.
7. **Fastloaders / custom IEC protocols.** Anything that bit-bangs the IEC bus directly bypasses our shortcut and lands in the (broken) `accurate-python` tier.

#### Documentation drift

1. `docs/disk_support.md:63` references plan file `disk-support-rework-1758d2.md`, which **does not exist**. The drive-split plan at `.windsurf/plans/drive-emulator-split-e2fbd2.md` is the only plan checked in. The bit-level rework plan needs to be authored before that work starts (see §4 of this doc).
2. `docs/drive_emulator.md` §"LED semantics" claims the LED works in non-`fast` tiers; in practice the only working tier is `fast`, so the LED **never** lights via that property today.
3. `docs/disk_support.md:25` mentions a file `drive1541.py` that does not exist; the actual file is `drives/c1541_emulator.py`. Search-and-replace needed.

---

## 3. Bit-level rework — high-level shape

This is the single biggest outstanding piece. It is what lets:

- `accurate-python` / `accurate-rust` tiers actually mean something.
- The LED work authentically, including blinking on errors.
- Custom fastloaders run.
- Copy-protected disks load.

### 3.1 What "bit-level" actually means here

Two independent layers, both currently abstracted away in `fast`:

#### A. Bit-level IEC bus

The C64 talks to the 1541 over **3 wires + GND**: ATN (out from C64), CLK, DATA (both bidirectional). Bytes are not transferred as bytes — each bit is clocked individually with a strict timing protocol (see §3.2 references). Today:

- C64 KERNAL: `iec_bus.py` exposes `set_atn`, `read_data`, etc. but the routines that *use* these are bypassed because the `$FFD5`/`$FFD8` shortcut is enabled.
- Drive: `Drive1541` has `iec_send_byte`/`iec_receive_byte` byte-level helpers wired to the JSON protocol — there is no edge-level CLK/DATA toggling.

For accurate-python: implement actual edge-level signalling on both sides. C64 KERNAL bit-bangs through `CIA2 PA` (`$DD00` bits 3-7). Drive bit-bangs through `VIA1 PB` (bits 0-7). The *physical* wiring is `wired-AND` (pull-ups, anyone driving low wins). Model this in `iec_bus.py` as a single shared state with multiple drivers.

#### B. GCR head

The 1541 reads/writes raw flux transitions, not sectors. Today the job-queue trap (`drives/c1541_emulator.py:_on_job_queue_write`) intercepts a memory write at `$00-$05`, looks up the equivalent sector in the `D64Image` object, and stuffs bytes straight into drive RAM bypassing the GCR head and the read circuitry.

For accurate-python: the head must produce a real bitstream. Pieces:

- **Density zones**: tracks 1-17 = 16 bits/cell, 18-24 = 17, 25-30 = 18, 31-35 = 19. VIA2 PB6/PB5 select zone. Drive expects faster bits on outer tracks.
- **GCR encoding**: 4 data bits → 5 channel bits (lookup). No 3 consecutive zeros allowed. Sync = `1111111111` (10+ ones), used as track-start marker.
- **Sector layout on disk**: header (sync + `$08`-marker + checksum + sector + track + ID + GAP) + data (sync + `$07`-marker + 256 data bytes + checksum + GAP).
- **Read path**: VIA2 PA latches a byte every 8 cells; BVC line ("byte ready") pulses on each complete byte; drive CPU reads PA in a tight loop.
- **Write path**: drive shifts out via VIA2 PA → write head; needs head step (PB1/PB0), motor (PB2), write-enable (PB3, also LED on real hardware).

### 3.2 References worth reading before touching this

- VICE source — `src/drive/iec/iec.c`, `src/drive/iecbus.c`, `src/drive/iec1541/iec1541.c`. Authoritative edge-timing model.
- VICE `src/drive/iec/iec-cmdline-options.c` — list of every IEC quirk users care about.
- "Inside Commodore DOS" (Immers & Neufeld, 1984) — chapters on the GCR head and the serial protocol.
- "Inner Space Anthology" (Transactor, 1985) — concise GCR codec listing.
- The 1541 schematic (320008-01). VIA2 PA = R/W head data, PB0/PB1 = head step, PB2 = motor, PB3 = write enable + LED, PB5/PB6 = density, PB7 = SYNC detect, CA1 = byte-ready.
- 64'er Special "Floppy 1541" (1986) — bit-timing diagrams.

### 3.3 Why this is hard

- **Two-CPU lockstep**: drive 6502 and C64 6510 must advance in lockstep with a sub-microsecond budget for IEC edges to be observed correctly. The current `_step_iec_drives` runs them in chunks of cycles, which is good enough for the byte-level protocol but **will not** be accurate enough for fastloaders that depend on edge timing within a few cycles.
- **GCR head writes feed back into reads**: writing a sector and reading it back is the canary test. Until that round-trips byte-for-byte, nothing built on top is trustworthy.
- **Verifying correctness without VICE**: the only realistic way to validate accuracy is to run a known-difficult disk under both VICE and c64py and compare. Pick something with a custom loader (early Hewson, early Ocean) and a copy-protected boot (anything with a "GCR puzzle" track 36-40).
- **Fast vs accurate must coexist**. Many users want `fast` permanently. The bit-level path must be opt-in; the existing tier system supports this.

---

## 4. Proposed plan — `disk-support-rework-1758d2`

This is the plan currently referenced (but missing) from
`docs/disk_support.md`. **Author the full milestone-by-milestone plan
in `.windsurf/plans/disk-support-rework-1758d2.md` before starting
work**, mirroring the level of detail of the existing
`drive-emulator-split-e2fbd2.md`. The skeleton below is the agreed
shape; the detailed plan needs to expand each milestone with: files
to touch, exact symbols to add/modify, copy-pasteable snippets, and a
per-milestone Verify block.

### 4.1 Milestone shape

- **B0 — Plan authoring**. Write `disk-support-rework-1758d2.md` with the same rigor as the drive-split plan. Single commit. No code changes.
- **B1 — IEC bus model in `iec_bus.py`**. Replace today's byte-level helpers with an edge-level state machine. Wired-AND model with explicit drivers. Unit tests against VICE-derived reference traces (capture 50-100 bytes of an `OPEN 1,8,15:PRINT#1,"I0":CLOSE 1` exchange from VICE, check c64py reproduces the same edge sequence).
- **B2 — Drive-side IEC port (VIA1)**. Wire `Drive1541` VIA1 PB to the new bus model. Remove the JSON-level `listen`/`talk`/`send_byte`/`request_byte` shortcuts when `--emulation accurate-python` is selected (keep them for `fast`). At the end of B2, plain `LOAD` over `accurate-python` should work end-to-end through the actual KERNAL serial routines.
- **B3 — GCR codec**. Pure functions in `drives/gcr.py`: `gcr_encode_sector(track, sector, data, disk_id) -> bytes` (raw track bits), `gcr_decode_track(bits) -> [(sector, data)]`. Round-trip property test. No I/O changes yet.
- **B4 — GCR head model**. Replace `_on_job_queue_write` with a real head: VIA2 PA shift register, BVC pulses, PB3 write-enable (and LED!), PB5/PB6 density, PB0/PB1 stepper, PB2 motor, PB7 SYNC. The D64Image becomes a *backing store* for whole-track GCR streams, regenerated on motor-on. Round-trip test: format an empty disk, write a file, eject, reattach, read file, compare bytes.
- **B5 — Disable the KERNAL shortcut for `accurate-python`** (it's still on by default in `fast`). The shortcut already lives behind `kernal_load_shortcut_enabled`; just stop setting it for non-`fast` tiers (currently `C64.py:978` does this conditionally — verify and add tests).
- **B6 — LED fix falls out for free** in B4 (PB3 = write enable + LED on real hardware). Add a regression test that loads a file with `disk_emulation = "accurate-python"` (or equivalent drive `--emulation`) and asserts `led_on` was true at least once during the operation.
- **B7 — Fastloader smoke test.** Pick one well-known, KERNAL-replacing fastloader (e.g. an early Ocean turbo) and verify it loads under `accurate-python`. This is the bar for "useful".
- **B8 — Rust port (`accurate-rust`)**. Mirror B1-B4 in `rust/c64py-core/src/drive_*.rs`. Same Verify suite. Wire under the existing `--vic-emulation accurate-rust` style flag plumbing.
- **B9 — Multi-drive accuracy**. Two drives on the bus, simultaneously, doing a `LOAD"$",8` + `OPEN 15,9,15` exchange. Test that ATN handling correctly addresses one device at a time.
- **B10 — Docs + changelog**. Update `docs/disk_support.md`, add entry to its Changelog, fix the broken plan reference, fix `drive1541.py`→`c1541_emulator.py` references.

### 4.2 Out of scope of this rework (track in `TODO.md`)

- REL/SEQ/USR file types — independent, do separately.
- Real `N0:` format — depends on B4 (need the GCR head to write fresh tracks).
- Graphics-mode TUI — wholly independent, do whenever.
- Multi-drive auto-spawn from the C64 CLI — independent; can ship anytime.

### 4.3 Risk register

- **Two-CPU sync drift** under accurate timing. Mitigate by stepping both CPUs in cycle-accurate lockstep when `accurate-python` is on, even at the cost of throughput. Document the throughput hit (probably 5-10× slower for disk I/O).
- **GCR encoding bugs are silent**. Always verify by round-trip (encode then decode) and by comparing BAM/directory bytes against the original D64.
- **VICE-as-oracle is not free**. You need to run VICE with `-warp -truedrive` and capture traces. Build a small harness that injects key sequences into both emulators and diffs screen RAM.
- **Performance of the Python GCR head**. Each motor-on event regenerates ~7 KB of GCR per track. Cache regenerated tracks per `(D64Image, track)` and invalidate on writes.
- **`atexit` + child subprocesses**. Already a known issue from the drive-split work; revisit if any new subprocesses are introduced.

### 4.4 Definition of done for this entire rework

1. `LOAD"NAME",8` over `accurate-python` works end-to-end with the KERNAL shortcut **off**.
2. `SAVE` round-trips byte-for-byte through the GCR head.
3. The drive LED toggles visibly during load/save in the TUI under `accurate-python`.
4. A representative fastloader (B7) loads.
5. All existing tests still green; new tests in `test/test_iec_bitlevel.py`, `test/test_gcr_codec.py`, `test/test_gcr_head_roundtrip.py`, `test/test_accurate_python_load_save.py`.
6. `docs/disk_support.md` Changelog entry and the broken plan reference fixed.

---

## 5. Other outstanding work (smaller, independent)

Each of these can be done without touching §4. Suggested ordering:

### 5.1 Multi-drive auto-spawn (small, ~half day)

Today only `--disk` → drive 8. Extend to:

```bash
C64.py game.d64                          # drive 8
C64.py game.d64 --disk2 b-side.d64       # drive 8 + drive 9
```

Files: `C64.py` argparse + the auto-spawn block at lines ~999-1014. `emulator._spawn_local_drive` already accepts `device=`. Just call it once per `--disk*` arg. Verify: a quick smoke that runs `LOAD"$",9` after boot.

### 5.2 REL / SEQ / USR file types in `d64.py` (medium, ~1-2 days)

Files: `d64.py` (parser + `write_file`), `drives/drive.py` (helper). Tests: round-trip a SEQ file through `OPEN 1,8,2,"NAME,S,W":PRINT#1,…:CLOSE 1` and read it back via `OPEN 1,8,2,"NAME,S,R":INPUT#1,…`.

### 5.3 Status channel end-to-end test (small)

Programs read drive status with:

```basic
OPEN 1,8,15
INPUT#1,EN,EM$,ET,ES
CLOSE 1
```

Today `Drive.get_status()` produces the right string and the JSON `status` RPC works, but there is no test that exercises this from BASIC over the wire. Add one to `test/test_drive_status.py`.

### 5.4 VERIFY support (tiny)

`$FFD5` with `A!=0` is a VERIFY. The current code branch uses `verify` but does no comparison. Either implement (compare against C64 RAM, set carry/`$90` per result) or document the limitation. Add a test either way.

### 5.5 Graphics mode (`--interface graphics`, separate effort)

Pygame window with a 1541 image and a real LED. Independent; nice-to-have.

---

## 6. Resuming work — handoff checklist

When you sit down fresh:

1. `git status` clean; `git log --oneline -10` to see what shipped.
2. `cat progress.txt` (drive-split milestones; should still be all "done").
3. Read **this document** end-to-end. Reality may have drifted; if so, update this doc *in the same commit* as your fix.
4. Read `TODO.md`. Pick an item.
5. If picking the bit-level rework: first author `.windsurf/plans/disk-support-rework-1758d2.md` (per §4.1 B0). **Do not start B1 until B0 is committed and the plan stands on its own.**
6. If picking a §5 item: scope is small enough to just do it; still commit a one-line entry in `TODO.md` "Done" when finished.
7. Run the relevant test slice as you go: `pytest -q test/test_drive*.py test/test_disk*.py test/test_kernal*.py`.
8. Full-suite green before merge: `pytest -q --ignore=test/test_all_vice.py --ignore=test/test_vice.py`.

---

## 7. Glossary

- **D64**: 174,848-byte sector dump of a 35-track 1541 disk. No GCR, no header gaps. The "logical" disk format c64py natively understands.
- **GCR**: Group-Coded Recording. The 4-to-5 bit channel code the 1541 actually writes to magnetic media.
- **IEC bus**: Commodore's 3-wire serial bus (ATN, CLK, DATA + GND).
- **Job queue**: zero-page slots `$00-$05` on the 1541 where DOS posts "go read sector N" requests to the disk-controller code. The "fast" tier intercepts these.
- **KERNAL shortcut**: hooking `$FFD5`/`$FFD8` on the C64 side and servicing LOAD/SAVE without ever talking to the drive's CPU. Fast, but bypasses everything below it.
- **`fast`** tier: real drive CPU runs, but disk surface is virtualised (job-queue trap) and KERNAL LOAD/SAVE is shortcut.
- **`accurate-python`** tier: real drive CPU, real GCR head, real IEC edges, no shortcuts. Today: placeholder, falls back to `fast`.
- **`accurate-rust`** tier: same as accurate-python but in the Rust core. Today: placeholder, falls back to accurate-python (which falls back to fast).
