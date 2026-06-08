# AGENTS.md

## Cursor Cloud specific instructions

### Project overview

c64py is a Commodore 64 emulator in Python. The single main entry point is `C64.py`. See `README.md` for full usage, `CLOUD.md` for CI/cloud testing guidance, `.github/copilot-instructions.md` for coding conventions, and `docs/config.md` for the TOML config system.

### ROMs (required)

The emulator needs C64 ROM files to run. They are **not** shipped in the repo. In the Cloud Agent VM they are pre-built at `$HOME/roms/` and `C64PY_ROM_DIR` is set in `~/.bashrc`. If ROMs are missing, rebuild them:

```bash
apt-get install -y cc65 libarchive-zip-perl
git clone --depth 1 https://github.com/mist64/c64rom.git /tmp/c64rom
cd /tmp/c64rom && SHELL=/bin/bash make
mkdir -p $HOME/roms
cp /tmp/c64rom/basic.bin $HOME/roms/basic.901226-01.bin
cp /tmp/c64rom/kernal.bin $HOME/roms/kernal.901227-03.bin
```

Note: the c64rom Makefile requires `SHELL=/bin/bash` to be set explicitly, otherwise the `crc32` check fails.

### Running the emulator (headless smoke test)

`--max-cycles` now implies autoquit by default (use `--no-autoquit` to disable). Without the Rust core, you must set `C64PY_USE_RUST_FAST=0` or pass `--vic-emulation fast`:

```bash
C64PY_ROM_DIR=$HOME/roms C64PY_USE_RUST_FAST=0 timeout 30 python C64.py --no-colors --max-cycles 3000000 --vic-emulation fast --no-config
```

Expected output: C64 BASIC V2 boot header and `READY.` prompt.

### Running in TCP server mode

```bash
C64PY_ROM_DIR=$HOME/roms C64PY_USE_RUST_FAST=0 python C64.py --tcp-port 6464 --no-colors --max-cycles 10000000 --vic-emulation fast --no-config
```

Then connect with: `python -c "import socket; s=socket.socket(); s.connect(('localhost',6464)); s.sendall(b'STATUS\n'); print(s.recv(4096).decode()); s.close()"`

### Running tests

```bash
C64PY_ROM_DIR=$HOME/roms C64PY_USE_RUST_FAST=0 python -m pytest test/ -v --ignore=test/test_all_vice.py --ignore=test/test_vice.py
```

Notes on pre-existing test issues:
- VICE compatibility tests (`test/test_all_vice.py`, `test/test_vice.py`) require external VICE test assets fetched via `./scripts/fetch_vice_tests.sh` — skip them in routine runs.
- Tests referencing `test/ark.d64` (in `test_fast_load_rpc.py`, `test_iec_protocol.py`, `test_autospawn.py`) fail because `*.d64` is gitignored. Some tests in `test_drive_status.py` and `test_kernal_load.py` have proper `skipif` guards; others do not.
- Tests in `test_drive1541_stepping.py` fail due to `Drive1541Memory` lacking `_vic_regs` (pre-existing bug in CPU raster advance code).
- Rust parity tests in `test_rust_core_parity.py` and `test_iec_rust_interlock.py` skip unless the optional Rust extension is built.

### TOML config system

The emulator now reads `c64py.toml` (search: cwd → `~/.c64py.toml` → XDG config). CLI flags always override config values. Use `--no-config` for reproducible test runs with pure defaults. See `docs/config.md` for the full schema.

### Building the package

```bash
pip install build
python -m build
```

### No formal linter

No linter (ruff/flake8/pylint) is currently configured. Syntax-check with `python -m py_compile <file>`.

### Gotchas

- `python` may not exist as a command; use `python3` or create a symlink: `ln -sf $(which python3) /usr/local/bin/python`
- The default VIC mode is now `--vic-emulation fast`; `accurate-rust` still requires the optional Rust core.
- Use `--no-config` when running headless/CI to avoid picking up a stale `c64py.toml` from cwd.
- SDL2 libraries (`libsdl2-dev`, etc.) must be installed for `pygame-ce` to work properly.
- The `drives/` subpackage is now separate (`c64py.drives`). Old `drive.py`/`drive1541.py` were removed; use `c64py.drives.drive`, `c64py.drives.c1541_emulator`, etc.
