# C64 Emulator (Python)

A Commodore 64 emulator in Python with Textual, headless, and pygame interfaces. Run BASIC, load PRG/D64 media, attach drives over TCP, and debug with optional Rust-accelerated CPU/VIC paths.

<p align="center">
  <img src="docs/images/readme/boot_ready.gif" alt="BASIC READY with blinking cursor">
</p>

## Features

- **6502 CPU** with optional **Rust fast batch** (`c64py_rust_core`) — see [docs/rust_core.md](docs/rust_core.md)
- **Memory map**, **VIC-II**, **SID**, **CIA1/CIA2**, **IEC bus**
- **Interfaces**: Textual TUI (default), headless, pygame (`--interface graphics`)
- **Media**: `.prg` autoload, `.d64` attach (auto-spawn drive 8), `.bas` via `petcat`
- **Disk & drives**: local auto-spawn, standalone `c1541_emulator`, remote `--tcp-drive` — see [docs/disk_support.md](docs/disk_support.md)
- **Remote control**: TCP/UDP command server, TCP monitor, host-memory mailbox — see sections below
- **Snapshots**, UDP/VICE tracing, TOML config — see linked docs

## Quick start

### Install

```bash
pip install c64py          # PyPI
# or, from a clone:
pip install -r requirements.txt
```

ROM files are **not** shipped. Point to a directory with C64 ROMs (or let the emulator offer a local install on first interactive run):

```bash
export C64PY_ROM_DIR=~/roms   # optional; auto-detect also checks ./roms, ~/.c64py/roms, VICE paths
c64py --rom-dir ~/roms
```

### Run

```bash
c64py                        # Textual UI, BASIC READY
c64py game.prg               # load + RUN after boot
c64py disk.d64               # attach as drive 8 (auto-spawn headless 1541)
c64py --interface graphics   # pygame window
c64py --interface headless --max-cycles 3000000 --no-colors --no-config
```

Positional `FILE` behavior:

| Extension | Action |
|-----------|--------|
| `.prg` | Load at `$0801` and inject `RUN` after BASIC boot |
| `.d64` | Auto-spawn drive 8 with the image (unless `--tcp-drive` is set) |
| `.bas` | Convert with VICE `petcat`, then load like a PRG |

### Common flags

| Flag | Notes |
|------|--------|
| `--rom-dir DIR` | KERNAL/BASIC/CHAR (and optional DOS) ROM directory |
| `--interface {textual,headless,graphics,...}` | UI mode (default: `textual`) |
| `--max-cycles N` | Stop after N CPU cycles (**implies `--autoquit`**; use `--no-autoquit` to keep running) |
| `--turbo` | No wall-clock throttling |
| `--no-config` | Ignore `c64py.toml` (useful for CI/repro runs) |
| `--vic-emulation {fast,accurate-python,accurate-rust}` | VIC timing tier — [docs/emulation_modes.md](docs/emulation_modes.md) |
| `--video-rendering {per-frame,per-raster,per-cycle}` | Host pixel sampling — [docs/per_cycle_vic.md](docs/per_cycle_vic.md) |
| `--audio-emulation {resid,python-sid,disabled}` | SID backend |
| `--tcp-port` / `--udp-port` | Control server (see below) |
| `--monitor-port` | TCP monitor for stepping — [docs/DEBUGGING.md](docs/DEBUGGING.md) |
| `--host-command-ctrl TX=…,RX=…` | Guest RAM mailbox — [docs/host_memory_command_channel.md](docs/host_memory_command_channel.md) |

Headless smoke test (no Rust core required):

```bash
C64PY_USE_RUST_FAST=0 c64py --no-config --no-colors --interface headless \
  --max-cycles 3000000 --vic-emulation fast
```

## Demos

Captured straight from the emulator with the local capture tool
(cycle-accurate `accurate-rust` VIC + `per-cycle` compositing, real SID audio in
the MP4s). The clips below are short GIF previews — click through for the
full-length video with sound.

| Demo | Preview | Full video (with SID audio) |
|------|---------|------------------------------|
| **Bruce Lee** — title/menu | ![Bruce Lee menu](docs/showcase/brucelee_menu.gif) | [brucelee_01_menu.mp4](docs/showcase/brucelee_01_menu.mp4) |
| **Bruce Lee** — gameplay (joystick) | ![Bruce Lee gameplay](docs/showcase/brucelee_playing.gif) | [brucelee_03_playing.mp4](docs/showcase/brucelee_03_playing.mp4) |
| **Swinth** — first song/animation | ![Swinth first](docs/showcase/swinth_first.gif) | [swinth_02_first_song.mp4](docs/showcase/swinth_02_first_song.mp4) |
| **Swinth** — second song/animation | ![Swinth second](docs/showcase/swinth_second.gif) | [swinth_04_second_song.mp4](docs/showcase/swinth_04_second_song.mp4) |

> GIFs are silent, downscaled previews. The linked MP4s are full resolution and
> include the SID soundtrack.

Want to capture your own? See **[How to capture screenshots & videos](docs/capturing_screenshots.md)**.

## Configuration

Optional TOML (`c64py.toml` in cwd → `~/.c64py.toml` → XDG config). CLI flags override config.

```bash
c64py --write-config    # emit a populated default
```

See [docs/config.md](docs/config.md) for the full schema.

## Video & VIC

Host rendering (`--video-rendering`) is separate from VIC CPU timing (`--vic-emulation`).

- **Modes & defaults**: [docs/emulation_modes.md](docs/emulation_modes.md)
- **Per-cycle / FLI-style effects**: [docs/per_cycle_vic.md](docs/per_cycle_vic.md)
- **Debugging matrix, monitor, traces**: [docs/DEBUGGING.md](docs/DEBUGGING.md)
- **Rust hybrid VIC/CPU batch**: [docs/rust_core.md](docs/rust_core.md)

Per-scanline splits: `--video-rendering per-raster` with `--vic-emulation accurate-python` or `accurate-rust`. Sub-row effects need `--video-rendering per-cycle`.

## Disk & drive emulation

| Topic | Doc |
|-------|-----|
| D64 attach, tiers, KERNAL hooks | [docs/disk_support.md](docs/disk_support.md) |
| Standalone `c1541_emulator` process | [docs/drive_emulator.md](docs/drive_emulator.md) |
| Remote drive over TCP (`--tcp-drive`) | [docs/tcp_hardware_drive.md](docs/tcp_hardware_drive.md) |
| IEC bus status | [docs/iec_bus.md](docs/iec_bus.md) |

Quick examples:

```bash
# Local auto-spawn (pass .d64 as positional)
c64py mydisk.d64

# Standalone drive server + C64 client
python -m c64py.drives.c1541_emulator --disk game.d64 --device 8 --port 6408
c64py --tcp-drive 8:localhost:6408 game.prg
```

## Remote control (three channels)

These are **different** surfaces — do not confuse the TCP control port with the monitor or the guest mailbox.

### 1. Control server (`--tcp-port` / `--udp-port`)

Line-oriented **emulator control** on localhost. Same grammar as `HELP` in [command_dispatch.py](command_dispatch.py). Examples:

```bash
c64py --tcp-port 6464 --interface headless --max-cycles 10000000
# echo STATUS | nc localhost 6464
```

`STATUS`, `MEMORY`, `WRITE`, `DUMP`, `SCREEN`, `LOAD`, `ATTACH-DISK`, `DETACH-DISKS`, `SEND_KEY`, `INJECT`, `STOP`, `QUIT`, …

`INJECT` injects keys with friendly syntax and a real-hardware option:

```bash
# echo 'INJECT {F1}'            | nc localhost 6464   # single key -> CIA1 matrix (real press)
# echo 'INJECT RUN{return}'     | nc localhost 6464   # multiple keys -> KERNAL buffer
# echo 'INJECT MATRIX {space}'  | nc localhost 6464   # force matrix (one keystroke)
# echo 'INJECT BUFFER HELLO'    | nc localhost 6464   # force KERNAL buffer
# echo 'INJECT JOY 2:up+fire:300ms' | nc localhost 6464   # hold joystick port 2
```

Auto mode picks **matrix** for a single keystroke (so games scanning `$DC00`/`$DC01`
directly — including F-keys like `{F1}` — see a real press) and the **KERNAL buffer**
for multi-key strings. `--inject-keys` follows the same rule. `SEND_KEY`/`SEND_KEYS`
remain buffer-only (raw PETSCII codes).

### 2. Monitor server (`--monitor-port`)

Lightweight **CPU debugger** (step, breakpoints, memory dump) — not VICE protocol. See [docs/DEBUGGING.md](docs/DEBUGGING.md) §6.

```bash
c64py --monitor-port 6510 --interface headless
```

### 3. Host-memory command channel (`--host-command-ctrl`)

Guest program pokes requests into C64 RAM mailboxes; host polls and dispatches the **same command grammar** as the TCP server. Off by default. See [docs/host_memory_command_channel.md](docs/host_memory_command_channel.md).

## Textual interface

Default UI when not using `--interface headless|graphics` and not relying solely on server ports.

- **C64 panel**: colored 40×25 screen
- **Debug log** and **status bar**
- **Shortcuts**: `Ctrl+X` quit · `F10` turbo · `F11` layout · `F12` save text snapshot to `snapshots/textual_screen_*.txt`

![Textual UI overview](docs/images/readme/textual_ui.png)

## Architecture

| Component | Location |
|-----------|----------|
| Entry point / CLI | `C64.py` |
| Emulator loop, IEC, KERNAL hooks | `emulator.py` |
| 6502 CPU | `cpu.py` (+ optional `c64py_rust_core`) |
| Memory, VIC snapshots | `memory.py` |
| pygame presenter | `graphics.py`, `presenter.py` |
| Textual UI | `ui.py` |
| Control server | `server.py`, `command_dispatch.py` |
| Monitor TCP | `monitor_tcp.py` |
| Drive stack | `drives/` — `drive.py`, `c1541_emulator.py`, `tcp_drive_client.py`, … |
| Snapshots | `snapshot.py` — [docs/snapshots.md](docs/snapshots.md) |

## Documentation index

| Doc | Contents |
|-----|----------|
| [docs/config.md](docs/config.md) | TOML config schema |
| [docs/emulation_modes.md](docs/emulation_modes.md) | VIC & audio tiers |
| [docs/per_cycle_vic.md](docs/per_cycle_vic.md) | Per-cycle / per-raster rendering |
| [docs/DEBUGGING.md](docs/DEBUGGING.md) | Traces, monitor, repro matrix |
| [docs/rust_core.md](docs/rust_core.md) | Optional Rust extension |
| [docs/disk_support.md](docs/disk_support.md) | D64, tiers, KERNAL LOAD path |
| [docs/drive_emulator.md](docs/drive_emulator.md) | Standalone 1541 server |
| [docs/tcp_hardware_drive.md](docs/tcp_hardware_drive.md) | TCP-attached drives |
| [docs/iec_bus.md](docs/iec_bus.md) | IEC bus implementation status |
| [docs/host_memory_command_channel.md](docs/host_memory_command_channel.md) | Guest→host mailbox |
| [docs/snapshots.md](docs/snapshots.md) | Save/load emulator state |
| [docs/performance.md](docs/performance.md) | Benchmarks & profiling |
| [docs/input_ux.md](docs/input_ux.md) | Keyboard/joystick UX |
| [docs/capturing_screenshots.md](docs/capturing_screenshots.md) | Capture screenshots/videos (dev tooling) |
| [AGENTS.md](AGENTS.md) | Agent/CI notes |

## Requirements

- Python 3.8+
- Runtime: `requirements.txt`
- README screenshot tooling only: `requirements-docs.txt`

## License

BSD 3-Clause — see [LICENSE](LICENSE).
