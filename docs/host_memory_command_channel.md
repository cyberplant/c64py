# Host command channel via C64 RAM

A guest program (BASIC or ML) running on the emulated C64 controls the host emulator by writing into a fixed pair of 256-byte mailboxes in C64 RAM. The host polls those mailboxes once per CPU quantum, dispatches the request, and writes a reply back into RAM where the guest can read it.

This is **not** a security boundary. It is **off by default** and only active when `--host-command-ctrl` is passed. The dispatcher reuses the full TCP server grammar (`STATUS`, `MEMORY`, `WRITE`, `LOAD`, `ATTACH-DISK`, …), so anything the user can do over TCP they can also do from the guest CPU. Treat it like an open debug port.

## CLI

```text
python C64.py --host-command-ctrl TX=0xC000,RX=0xC100 ...
```

- `TX` (guest → host): 256-byte mailbox where the guest places its request.
- `RX` (host → guest): 256-byte mailbox where the host places its reply.
- Addresses are mandatory and have **no defaults**. Hex (`0x…` / `$…`) and decimal both accepted.
- Each region is `[base, base+255]` inclusive (256 bytes total). The two regions must not overlap and each base must be ≤ `$FF00`.

## Wire format

Both directions are identical and symmetric:

| Offset | Bytes | Meaning |
|--------|-------|---------|
| `+0`   | 1     | **Size byte**: `0` = idle, `1..255` = N bytes ready |
| `+1..+N` | N   | Payload bytes |

State machine, same on both sides:

1. **Idle**: size byte is `0`. Producer may start writing payload.
2. **Producer commits**: writes payload bytes first, then writes the byte count to `+0` last (single store). Until the size byte changes, the consumer sees idle.
3. **Consumer reads**: when it sees a non-zero size byte, it copies that many bytes from `+1`, processes them, and writes `0` back to `+0` to acknowledge.
4. Producer must wait for the size byte to be `0` again before sending the next message.

Maximum payload per message: **255 bytes**. The host returns `ERROR: reply too long (N bytes, max 255)` (text mode) or `{"ok":false,"error":"reply too long…"}` (JSON mode) when a reply would exceed the cap.

## Encoding sniff

The host inspects the **first byte** of every guest request:

- `payload[0] == 0x7B` (ASCII `{`) → parsed as **JSON** (UTF-8 expected). Replies are always `{"ok":true,"result":"…"}` or `{"ok":false,"error":"…"}`.
- otherwise → treated as a **simple text command** (the existing TCP server grammar). Replies are the raw string the dispatcher returns.

The host does **not** PETSCII-normalize. The guest is responsible for poking the byte values it wants the host to see. Note that BASIC `PRINT "{"` and ASCII `{` both happen to encode as `$7B`, so JSON mode works identically from BASIC strings or hand-poked bytes.

### JSON envelope

Request:

```json
{"cmd": "STATUS"}
{"cmd": "WRITE", "args": ["$C200", "$AB"]}
```

`cmd` is a non-empty string. `args` is an optional list; numbers and strings are stringified and joined with the `cmd` (so `WRITE`, `["$C200", "$AB"]` becomes the text command `WRITE $C200 $AB`).

Reply:

```json
{"ok": true,  "result": "PC=$1000 A=$11 ..."}
{"ok": false, "error":  "missing or empty 'cmd' field"}
```

## Polling cadence

The host polls TX once per CPU quantum, immediately after `_service_snapshot_requests()` in the main run loop. Latency is roughly one Rust fast-batch (default 64 instructions). There is no rate limiting beyond that — a guest that pokes the size byte every iteration will drive the dispatcher every quantum.

When the host produces a reply but RX size byte is still non-zero (the guest hasn't acked the previous reply yet), the new reply is **dropped** and a counter is bumped (`HostCommandChannel.replies_dropped`). The host never blocks waiting for the guest.

## Examples

### BASIC

```basic
10 T = 49152 : R = 49408
20 A$ = "STATUS" + CHR$(13)
30 FOR I = 1 TO LEN(A$) : POKE T+I, ASC(MID$(A$,I,1)) : NEXT
40 POKE T, LEN(A$)
50 IF PEEK(R) = 0 THEN 50
60 N = PEEK(R)
70 FOR I = 1 TO N : PRINT CHR$(PEEK(R+I)); : NEXT : PRINT
80 POKE R, 0
```

### Machine code (sketch)

```asm
; assume A=cmd-len, X=lo(payload), Y=hi(payload), TX=$C000, RX=$C100
        ; ... copy payload bytes to $C001+ ...
        sta $C000               ; commit (size byte last)
.wait   lda $C100
        beq .wait
        ; reply length now in A; payload at $C101..
        ; ... read it ...
        lda #0
        sta $C100               ; ack
```

## Security and abuse

- **Off by default.** Distro packages and CI defaults must not flip this on.
- The dispatcher is the same one the TCP server uses, so it accepts `LOAD`, `ATTACH-DISK`, `WRITE`, `STOP`, `QUIT/EXIT`, etc. Trust level matches the user running the emulator — this is **not** a sandbox.
- A read-only / allow-listed sub-mode is out of scope for v1. If you need one, file an issue.

## Limits and future work

- Max single message: 255 bytes. Replies that overflow are returned as an explicit error, not truncated silently.
- No chunked / streaming replies. If you need to read more than 255 bytes (e.g. `DUMP`), use multiple narrower commands (`MEMORY $XXXX` per byte) or use the TCP server.
- No PETSCII normalization. The guest decides what bytes to send.
- No `--host-command-ctrl-readonly` flag yet.
- No TOML config key — flag must be passed on the CLI.

## Implementation pointers

- Module: `host_command_channel.py` — `HostCommandChannel` (poll/handle/encode) and `parse_host_command_ctrl` (CLI parser).
- Polling site: `emulator.py`, inside `C64.run()`, next to `_service_snapshot_requests()`.
- Construction site: `C64.py`, near the monitor / TCP server bring-up.
- Dispatcher: `command_dispatch.py` — shared with `EmulatorServer` so both transports speak the same grammar.
- Tests: `test/test_host_command_channel.py` (no ROMs required).

## Status

Implemented (v1). Off by default. See the security notes before enabling.
