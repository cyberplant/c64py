# Disk round-trip BAS fixtures

These ASCII listings are meant to be converted with VICE **petcat** (same as any `*.bas` passed to `C64.py`).

| File | Purpose |
|------|---------|
| `disk_host_write.bas` | Prints a line, **`OPEN`** a sequential file on device 8, **`PRINT#`**, **`CLOSE`**, then **`SAVE "HOSTPRG",8`** (SAVE uses the KERNAL fast path → TCP `fast_save`). |
| `disk_host_read.bas` | **`OPEN`** the same sequential file, **`LINE INPUT#`**, **`CLOSE`**. Intended to run after `disk_host_write.bas` on the **same** D64. |

**TCP (`--tcp-drive`) today:** `SAVE` / `LOAD` work via KERNAL hooks and JSON `fast_save` / `fast_load`. **`OPEN` / `PRINT#` / `INPUT#`** still use CIA2 bit-bang IEC; until the **KERNAL → logical `IECBus` bridge** is complete (see `iec_kernal_bridge.py` and `docs/plans/release_blockers_iec_percycle_vic.md`), the TCP drive may not see `listen` / `open_channel` and the guest can **hang** on `OPEN`. These fixtures are kept **on purpose** as regression targets once that bridge lands. For a round-trip that works **now** over TCP, use only the `SAVE` / `LOAD` portions or a **local** auto-spawned drive (no `--tcp-drive`). See `docs/drive_emulator.md` § *C64 with ``--tcp-drive``*.

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

Terminal 2 — C64 with ``--tcp-drive`` and a ``.bas`` positional (petcat on ``PATH``). **Until the KERNAL IEC bridge is done**, this fixture may **hang on ``OPEN``**; use a local drive for a full round-trip, or run only a SAVE-only snippet for TCP smoke.

```bash
.venv/bin/python C64.py --no-config --tcp-drive 8:127.0.0.1:6408 \
  test/fixtures/disk_host_write.bas \
  --max-cycles 3000000 --interface textual --no-colors
```
