# IEC serial bus (C64 host)

The **C64 emulator process** models the IEC wires between the 6510 (via CIA2
port A at `$DD00`) and attached drives. **Drive CPUs and D64 data live in the
TCP drive subprocess** — see [disk_support.md](disk_support.md) §1.

## What works

- **IEC bus infrastructure** ([`iec_bus.py`](../iec_bus.py)): open-collector
  ATN/CLK/DATA, multi-device support (8–11), peer state visible to CIA2 reads.
- **CIA2 ↔ IEC wiring** at `$DD00` bits 3–7 (7406 inverter polarity).
- **TCP drive clients** ([`TcpDriveClient`](../drives/tcp_drive_client.py)):
  implement [`IECDriveBackend`](../drives/iec_backend.py); logical LISTEN/TALK/
  OPEN/DATA forwarded as JSON to the remote `c1541_emulator`.
- **KERNAL IEC tap** ([`iec_kernal_bridge.py`](../iec_kernal_bridge.py)):
  records CIA2-derived line transitions; optional wire decoder when
  `C64PY_IEC_WIRE_DECODE=1` ([`iec_wire_decode.py`](../iec_wire_decode.py)).
- **KERNAL shortcuts** — `$FFD5`/`$FFD8` LOAD/SAVE and (when enabled)
  [`kernal_tcp_iec_hooks`](../kernal_tcp_iec_hooks.py) for OPEN/PRINT#/INPUT#
  over TCP; see [disk_support.md](disk_support.md) §4.
- **1541 in the drive process** — real 6502 + VIAs + DOS ROM in
  `c1541_emulator`; job-queue trap for `fast` tier sector I/O. The C64 host
  only calls `TcpDriveClient.step()` to drain socket replies.

## What's missing

- **Edge-accurate IEC** for fastloaders and `accurate-python` tier — today’s
  bus is byte/logical level plus optional wire decode; full edge timing is
  tracked in [disk_support.md](disk_support.md) §6.
- **`accurate-python` / `accurate-rust` disk tiers** — placeholders on the
  drive side (fall back to `fast`); bit-level IEC + GCR head not shipped.
- **PETSCII↔ASCII filename conversion** — plain ASCII decode in places.
- **Multi-drive stress** under simultaneous bit-level load — lightly tested.

## Related

- [disk_support.md](disk_support.md) — tiers, TCP split, forward plan
- [drive_emulator.md](drive_emulator.md) — standalone server protocol
