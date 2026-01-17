# Cloud Testing

## ROM setup

The emulator needs these ROM files in a single directory:

- `basic.901226-01.bin`
- `kernal.901227-03.bin`

`characters.901225-01.bin` is only required for `--graphics`.

Set `C64PY_ROM_DIR` to point at that directory.

### Option A: Build from the provided source repo

1. Install build prerequisites (Debian/Ubuntu example):
   - `sudo apt-get update && sudo apt-get install -y git cc65 libarchive-zip-perl python-is-python3`
2. Clone the ROM sources:
   - `git clone https://github.com/mist64/c64rom.git /workspace/c64rom`
3. Build ROMs:
   - `cd /workspace/c64rom`
   - `make`
4. Copy/rename outputs into your ROM directory:
   - `basic.bin` -> `basic.901226-01.bin`
   - `kernal.bin` -> `kernal.901227-03.bin`
5. If you plan to run with `--graphics`, you still need `characters.901225-01.bin` (see Option B).

### Option B: Use ROMs from VICE

Install the VICE emulator and locate its ROM directory (it usually includes
the three required files). Point `C64PY_ROM_DIR` to that directory.

## Test command

Run:

- `python -m pip install -e .`
- `timeout 30 python C64.py --no-color --max-cycles 3000000 --autoquit`

If `--no-color` is not recognized, use `--no-colors`.

## Expected output

- C64 boot header (e.g. `*** COMMODORE 64 BASIC V2 ***`)
- `READY.` prompt
