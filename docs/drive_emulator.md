# Standalone 1541 drive emulator

Run the 1541 as its own process. The C64 emulator connects over TCP
(`--tcp-drive DEVICE:HOST:PORT`). This mirrors how a real C64 talks to a
real 1541 over the IEC serial bus.

## Quick start

    # Terminal 1: run a drive serving game.d64 as device 8 on port 6408
    python -m c64py.drives.c1541_emulator \
        --interface text --emulation fast \
        --disk game.d64 --device 8 --port 6408

    # Terminal 2: run the C64 and attach to it
    python C64.py --tcp-drive 8:localhost:6408

Or let the C64 auto-spawn a headless drive for you:

    python C64.py game.d64                 # spawns drive 8 in background

## `--disk` and `--new-disk` (standalone server)

| Flag | Behavior |
|------|----------|
| `--disk PATH` | **Existing** D64 only; exits with an error if the file is missing. |
| `--new-disk PATH` | Writes a new **blank** `.d64` at `PATH` (standard 35-track layout with empty BAM/directory). **Refuses** if `PATH` already exists. Cannot be combined with `--disk`. |

Omit both to run **without** an inserted image (TCP `attach_disk` / fast RPC only, depending on client).

## C64 with ``--tcp-drive``: KERNAL vs IEC JSON

- **`LOAD` / `SAVE`** (devices 8–11) use the host KERNAL shortcuts and TCP
  **`fast_load`** / **`fast_save`** JSON when `kernal_load_shortcut_enabled` is
  on (default for all TCP clients, including auto-spawn).
- **`OPEN` / `PRINT#` / `INPUT#` / `CLOSE`** can work over TCP via
  [`kernal_tcp_iec_hooks.py`](../kernal_tcp_iec_hooks.py) (same shortcut gate).
  For CIA2 bit-bang paths without those hooks, set **`C64PY_IEC_WIRE_DECODE=1`**
  so [`KernalIecTap`](../iec_kernal_bridge.py) decodes wire transitions into
  logical `IECBus` commands forwarded as JSON
  (`test/test_iec_tcp_wire_integration.py`). Coverage is still partial — treat
  complex BASIC disk I/O as a regression target (`test/fixtures/README_disk_bas.md`).

The ``test/fixtures/disk_host_*.bas`` listings intentionally include **``OPEN`` / ``PRINT#``** (and **``SAVE``**) so they remain **regression targets** once the KERNAL→logical bridge lands; see ``test/fixtures/README_disk_bas.md``.

### KERNAL IEC tap JSONL

For bridge debugging, set ``C64PY_IEC_TAP_JSONL=/path/to/tap.jsonl`` while running a C64 instance with a TCP drive attached. ``iec_kernal_bridge.KernalIecTap`` appends one newline-delimited JSON object for each CIA2-derived resolved bus transition:

```json
{"cyc":120,"atn":true,"clk":false,"data":false}
```

Schema:

| Key | Type | Meaning |
|---|---|---|
| ``cyc`` | integer | ``MemoryMap.debug_last_cycles`` at the CIA2 apply point. |
| ``atn`` | boolean | Resolved ATN line after the write. |
| ``clk`` | boolean | Resolved CLK line after the write. |
| ``data`` | boolean | Resolved DATA line after the write. |

Line booleans follow the ``IECBus`` convention: ``true`` means released/high, ``false`` means asserted/low. The tap records transitions only after C64 CIA2 port A writes; it does not yet capture device-side line changes or synthesize logical JSON commands.

## `--interface`

| Value | Behavior |
|---|---|
| `headless` (default) | Log-only, no UI. Best for scripts, tests, CI. |
| `text` | Textual TUI with an ASCII 1541 and a live LED. |
| `graphics` | **TODO** — pygame window with a 1541 image. See `TODO.md`. |

## `--emulation`

Same tier names as `[emulation] disk_emulation` in `c64py.toml` (used when the C64 auto-spawns a headless drive). See [disk_support.md](disk_support.md) for architecture and tier details.

| Value | Behavior |
|---|---|
| `fast` (default) | Real 1541 6502 + DOS ROM, but sector I/O served via job-queue trap directly from the D64 image. `fast_load`/`fast_save` RPC is enabled. |
| `accurate-python` | Real GCR head + bit-level IEC (WIP, falls back to `fast`). |
| `accurate-rust` | Same, in Rust (WIP, falls back to `accurate-python`). |

## Protocol

Newline-delimited JSON over TCP. See `drives/c1541_emulator.py` `_run_server` for the
authoritative message list. For **third-party TCP servers** (ESP32, embedded bridge,
custom daemon), see [`tcp_hardware_drive.md`](tcp_hardware_drive.md) (greeting line,
minimal message set, 1571 notes). New messages added in this rework:

- Request: `{"type":"fast_load","filename":"FOO","secondary":1}`
  Reply: `{"type":"fast_load_reply","ok":true,"data":"<base64>","load_addr":2049}`
  or `{"type":"fast_load_reply","ok":false,"error_code":62,"error_message":"FILE NOT FOUND"}`
- Request: `{"type":"fast_save","filename":"FOO","data":"<base64>"}`
  Reply: `{"type":"fast_save_reply","ok":true}`
  or `{"type":"fast_save_reply","ok":false,"error_code":63,"error_message":"FILE EXISTS"}`
- Request: `{"type":"attach_disk","path":"/abs/path/game.d64"}`
  Reply: `{"type":"attach_disk_reply","ok":true,"disk_name":"...","disk_id":".."}`
- Request: `{"type":"detach_disk"}`
  Reply: `{"type":"detach_disk_reply","ok":true}`
- Request: `{"type":"status"}`
  Reply: `{"type":"status_reply","led_on":false,"disk":"game.d64","status":"00, OK,00,00"}`

## Architecture

```
C64.py / emulator.py
  │
  │  (auto-spawns on first disk arg, or explicit --tcp-drive)
  ▼
TcpDriveClient  ── JSON/TCP ──▶  c1541_emulator  (subprocess)
  │                                  │
  │ fast_load / fast_save RPC        │  Drive1541 (6502 + VIAs + DOS ROM)
  │ attach_disk / detach_disk        │  DiskDrive helper (D64 file ops)
  │ status                           │  text_ui.py (Textual TUI, optional)
  ▼
emulator: writes/reads C64 RAM directly (zero-copy on the host side)
```

The C64 emulator no longer holds any `DiskDrive` or D64 image state. All disk
state lives in the drive subprocess.  The KERNAL `$FFD5`/`$FFD8` hooks in
`emulator.py` call `TcpDriveClient.fast_load` / `fast_save` synchronously and
write the result straight into C64 RAM.

## LED semantics

Text mode polls `Drive1541.led_on` at ~30 Hz and renders a filled/empty circle.
LED is only meaningful for real bit-level tiers; in `fast` mode the LED mostly
stays off (the job-queue trap bypasses the VIA2 PB3 line that drives it).

## Graphics mode

**TODO.** Planned pygame renderer. Tracking item in `TODO.md`.
