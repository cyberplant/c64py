# TCP hardware drive bridge (ESP32, Pi, etc.)

The C64 side speaks to a **remote drive** over TCP using the same **newline-delimited JSON** protocol as [`c1541_emulator.py`](../drives/c1541_emulator.py) (`_run_server`). Any device that implements that protocol can sit in place of the Python drive process — for example firmware on an **ESP32** that bridges Wi‑Fi/Ethernet to a **real Commodore 1541-compatible** mechanism.

See also: [`drive_emulator.md`](drive_emulator.md), [`1541_status_and_plan.md`](1541_status_and_plan.md).

## Transport

- **TCP**, one connection per logical drive (per `TcpDriveClient` / `--tcp-drive DEVICE:HOST:PORT`).
- Messages are **one JSON object per line** (UTF-8), terminated by `\n`.
- On connect, the **server must send first**: `{"type":"ready","device":<N>}\n` where `<N>` is the device number (8–11). The host client drains this line so it does not mix with RPC replies ([`TcpDriveClient.connect`](../drives/tcp_drive_client.py)).

## Reconnect

The host uses a **5 s backoff** after a failed connect or dropped socket ([`TcpDriveClient.RECONNECT_DELAY`](../drives/tcp_drive_client.py)). Firmware should accept a new TCP session at any time.

## Minimal message set (KERNAL `fast` parity)

To support **`LOAD` / `SAVE` / directory `LOAD"$"`** the way the reference Python server does today, implement at least:

| Request `type` | Required fields | Reply `type` |
|----------------|-----------------|--------------|
| *(greeting)* | — | `ready` with `device` |
| `fast_load` | `filename`, `secondary` | `fast_load_reply` |
| `fast_save` | `filename`, `data` (base64) | `fast_save_reply` |
| `status` | — | `status_reply` |
| `attach_disk` | `path` (host path; firmware may ignore or map) | `attach_disk_reply` |
| `detach_disk` | — | `detach_disk_reply` |

**`fast_load_reply`:** `ok: true` with `data` (base64 PRG/directory bytes including load-address header as on a real LOAD), or `ok: false` with `error_code` / `error_message` (1541-style codes; the C64 maps 62/63 to FILE NOT FOUND / FILE EXISTS semantics).

**`fast_save_reply`:** same pattern.

**`status_reply`:** Reference server sends `led_on`, `disk`, `status` (drive status string). Optional extra keys (ignored by older clients): `implementation`, `media` — see Python server for examples.

## Full IEC / byte-level messages (optional)

For future **accurate** disk tiers without the KERNAL shortcut, the host may send `listen`, `talk`, `unlisten`, `untalk`, `open_channel`, `close_channel`, `secondary`, `send_byte`, `request_byte`. Replies: `byte` / `no_data` for `request_byte`. A hardware bridge that only implements **`fast_*`** does not need these for typical games using KERNAL LOAD/SAVE.

## Real **1571** vs **1541**

A **1571** in **1541-compatible** mode is fine for **single-sided 1541 GCR / D64-style** workloads: the C64 sees a normal device 8–11. c64py’s protocol and D64 helpers are **1541-centric**; native **1571 MFM double-sided** images are **not** represented in the current JSON/`fast_load` contract. A bridge should either restrict to 1541-style media or extend the protocol later (e.g. format flags, different attach semantics).

## Security note

`attach_disk` carries a **host path** string. A network-facing firmware should **not** blindly expose the host filesystem; validate peers, authenticate if exposed beyond localhost, and map `path` to intentional image slots only.
