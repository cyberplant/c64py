# Cloud Testing

## ROM setup

The emulator needs these ROM files in a single directory:

- `basic.901226-01.bin`
- `kernal.901227-03.bin`

`characters.901225-01.bin` is only required for `--graphics`.

Set `C64PY_ROM_DIR` to point at that directory.

### Option A: Build from the provided source repo

1. Install the cc65 toolchain (provides `ca65` and `ld65`).
2. Build ROMs:
   - `cd /workspace/c64rom`
   - `make`
3. Copy/rename outputs into your ROM directory:
   - `basic.bin` -> `basic.901226-01.bin`
   - `kernal.bin` -> `kernal.901227-03.bin`
4. You still need `characters.901225-01.bin` (see Option B).

### Option B: Use ROMs from VICE

Install the VICE emulator and locate its ROM directory (it usually includes
the three required files). Point `C64PY_ROM_DIR` to that directory.

## Test command

Run:

- `timeout 30 python C64.py --no-color --max-cycles 3000000 --autoquit`

If `--no-color` is not recognized, use `--no-colors`.

## Expected output

- C64 boot header (e.g. `*** COMMODORE 64 BASIC V2 ***`)
- `READY.` prompt
