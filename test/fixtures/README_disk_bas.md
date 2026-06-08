# Disk round-trip BAS fixtures

These ASCII listings are meant to be converted with VICE **petcat** (same as any `*.bas` passed to `C64.py`).

| File | Purpose |
|------|---------|
| `disk_host_write.bas` | Prints a line, **`OPEN`** a sequential file on device 8, **`PRINT#`**, **`CLOSE`**, then **`SAVE "HOSTPRG",8`** (SAVE uses the KERNAL fast path → TCP `fast_save`). |
| `disk_host_read.bas` | **`OPEN`** the same sequential file, **`LINE INPUT#`**, **`CLOSE`**. Intended to run after `disk_host_write.bas` on the **same** D64. |

**TCP (`--tcp-drive`):** `SAVE` / `LOAD` still use KERNAL hooks and JSON `fast_save` / `fast_load`. **`OPEN` / `PRINT#` / `INPUT#`** use CIA2 bit-bang IEC; with **`C64PY_IEC_WIRE_DECODE=1`** (and a TCP-attached tap from `C64.py` / :meth:`c64py.emulator.C64.initialize_iec_bus`), the wire decoder turns LISTEN / OPEN / filename / UNLISTEN into logical `IECBus` calls so the TCP client emits `listen` / `open_channel` / `send_byte` / `unlisten` JSON. Hermetic coverage: ``pytest test/test_iec_tcp_wire_integration.py``. Full BASIC round-trip over TCP may still need more KERNAL edge cases; these fixtures remain regression targets. For a round-trip that works **without** wire decode today, use a **local** auto-spawned drive (no `--tcp-drive`) or only `SAVE` / `LOAD` over TCP. See `docs/drive_emulator.md` § *C64 with ``--tcp-drive``*.

## Example: C64 auto-spawn (no TCP drive process)

Create an empty image on the host, then pass it as the positional ``.d64`` so ``C64.py`` auto-spawns drive 8:

```bash
OUT=/tmp/c64py_roundtrip.d64
rm -f "$OUT"
.venv/bin/python -c 'from c64py.d64 import create_blank_d64; create_blank_d64("RND","01").save_to_file("'"$OUT"'")'
.venv/bin/python C64.py --no-config "$OUT" test/fixtures/disk_host_write.bas \
  --max-cycles 4000000 --interface textual --no-colors
.venv/bin/python C64.py --no-config "$OUT" test/fixtures/disk_host_read.bas \
  --max-cycles 4000000 --interface textual --no-colors
```

## Example: standalone 1541 with ``--new-disk`` (TCP)

Terminal 1 — create **and** serve a new image (file must not exist; refuses overwrite):

```bash
OUT=/tmp/c64py_tcp.d64
rm -f "$OUT"
.venv/bin/python -m c64py.drives.c1541_emulator --interface headless --emulation fast \
  --new-disk "$OUT" --device 8 --port 6408
```

Terminal 2 — C64 with ``--tcp-drive`` and a ``.bas`` positional (petcat on ``PATH``). Set **`C64PY_IEC_WIRE_DECODE=1`** so OPEN/PRINT#/INPUT# can reach the TCP drive over the wire decoder; without it, ``OPEN`` may still **hang** on some KERNAL paths.

```bash
.venv/bin/python C64.py --no-config --tcp-drive 8:127.0.0.1:6408 \
  test/fixtures/disk_host_write.bas \
  --max-cycles 3000000 --interface textual --no-colors
```
