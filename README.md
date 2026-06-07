# C64 Emulator (Python)

A Commodore 64 emulator implemented in Python with both text-based and graphical interfaces. This emulator supports text mode, bitmap graphics modes, sprites, and can load and run PRG files.

## Features

- **6502 CPU Emulation**: Full 6502 instruction set implementation
- **Memory Management**: Complete C64 memory map with ROM/RAM mapping
- **I/O Devices**: VIC-II, SID, CIA1, CIA2 emulation
- **SID Audio Output**: Optional pygame-ce-based SID sound (`--enable-sid`), or higher-accuracy reSID via ctypes (`--enable-resid`; build `resid_c` from `src/resid_wrapper/`)
- **Text Mode Interface**: Beautiful textual UI using Rich and Textual libraries
- **Graphics Modes**: Full VIC-II graphics mode support
  - Standard text mode (40x25 characters)
  - Bitmap mode (320x200 pixels)
  - Multicolor bitmap mode (160x200 pixels)
  - Multicolor text mode
  - Extended color mode
  - Hardware sprites (8 sprites, 24x21 pixels)
- **Dual Rendering**:
  - **--graphics mode**: Full-resolution pygame window with bitmap and sprite support
  - **Text mode**: ASCII art representation of graphics using Unicode block characters
- **PRG File Loading**: Load and auto-run Commodore 64 programs
- **Server Mode**: TCP/UDP server for remote control
- **Debug Support**: UDP debug logging and detailed debug output; archived Bruce Lee loader investigation (historical; runtime env hooks removed): [docs/bruce_lee_loader_investigation.md](docs/bruce_lee_loader_investigation.md) (roadmap: [docs/LOADER_DEBUG_PLAN.md](docs/LOADER_DEBUG_PLAN.md))
- **Memory Dumping**: Export memory state to files
- **PAL/NTSC Support**: Configurable video standard

## Requirements

- Python 3.8 or higher
- See `requirements.txt` for Python dependencies

## Installation

### From PyPI (recommended)

```bash
pip install c64py
```

### From source (development)

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Ensure ROM files are available:
   - By default, the emulator auto-detects ROMs from common locations, including a per-user directory and common VICE install paths.
   - You can always point to ROMs explicitly with `--rom-dir`.
   - If ROMs are not found and you are running interactively, the emulator can offer to install ROMs from a **local** directory or archive into a per-user directory (so future runs work automatically). ROMs are not shipped by default because many ROM binaries are copyrighted.

## Usage

### Basic Usage

Run the emulator with a media file (extension selects behavior):

- `.prg` — load and auto-run
- `.d64` — attach as drive 8 (same as the former `--disk` behavior)
- `.bas` — convert with VICE `petcat` to a temporary PRG, then load (requires `petcat` on `PATH`)

```bash
c64py program.prg
c64py disk.d64
```

Run the emulator without a program (starts at BASIC prompt):
```bash
c64py
```

### Command Line Options

- `FILE` (positional): Optional PRG, D64, or BAS file (see above)
- `--rom-dir DIR`: Directory containing ROM files (default: auto-detect common locations)
- `--tcp-port PORT`: Enable TCP server on specified port
- `--udp-port PORT`: Enable UDP server on specified port
- `--max-cycles N`: Maximum CPU cycles to run (default: unlimited)
- `--dump-memory FILE`: Dump memory to file after execution
- `--debug`: Enable debug output
- `--udp-debug`: Send debug events via UDP
- `--autoquit`: Automatically quit when max cycles is reached
- `--udp-debug-port PORT`: UDP port for debug events (default: 64738)
- `--udp-debug-host HOST`: UDP host for debug events (default: 127.0.0.1)
- `--screen-update-interval SECONDS`: Screen update interval (default: 0.1)
- `--video-standard {pal,ntsc}`: Video standard (default: pal)
- `--no-colors`: Disable ANSI color output
- `--graphics`: Render output in a pygame graphics window
- `--graphics-scale N`: Scale factor for graphics window (default: 2)
- `--graphics-fps N`: Target FPS for graphics window (default: 30)
- `--graphics-border N`: Border size in pixels for graphics window (default: 32)
- `--enable-sid`: Enable SID audio output via pygame-ce
- `--enable-resid`: Enable reSID-based SID audio (requires building/installing `resid_c.so` / `resid_c.dylib`; see `src/resid_wrapper/README.md`)

### Examples

Run with debug output:
```bash
c64py program.prg --debug
```

Run in server mode (TCP):
```bash
c64py --tcp-port 1234
```

Run with UDP debug logging:
```bash
c64py program.prg --udp-debug --udp-debug-port 64738
```

Run with auto-quit after max cycles:
```bash
c64py program.prg --max-cycles 5000000 --autoquit
```

Dump memory after execution:
```bash
c64py program.prg --dump-memory memory.prg
```

Run with graphics window:
```bash
c64py program.prg --graphics --graphics-scale 3
```

## Graphics Mode Support

The emulator supports all VIC-II graphics modes:

### Display Modes

1. **Standard Text Mode** (40x25 characters)
   - Default mode, 8x8 character cells
   - 16 colors per character
   - Controlled by VIC-II registers

2. **Bitmap Mode** (320x200 pixels)
   - Hi-resolution bitmap graphics
   - Each pixel can be one of two colors per 8x8 block
   - Enabled via $D011 bit 5

3. **Multicolor Bitmap Mode** (160x200 pixels)
   - Lower resolution with 4 colors per 4x8 block
   - Enabled via $D011 bit 5 + $D016 bit 4

4. **Extended Color Mode**
   - Text mode with 4 selectable background colors
   - Enabled via $D011 bit 6

5. **Multicolor Text Mode**
   - Text mode with multicolor characters
   - Enabled via $D016 bit 4

### Sprite Support

- 8 hardware sprites (24x21 pixels each)
- Hi-res and multicolor sprite modes
- Sprite positioning and colors
- Sprite enable/disable via $D015
- Sprite data from memory pointers

### Graphics Rendering

**Pygame Window (--graphics mode):**
- Full-resolution rendering (320x200 pixels)
- Proper bitmap and sprite rendering
- Accurate C64 color palette
- Scalable window (--graphics-scale option)

**Text Mode (default):**
- ASCII art representation using Unicode block characters
- Samples bitmap data to create approximate visualization
- Uses characters: ░▒▓█ for different pixel densities
- Maintains color information from C64 palette

### VIC-II Registers

The emulator implements key VIC-II registers:
- `$D011`: Control Register 1 (bitmap mode, extended color mode)
- `$D016`: Control Register 2 (multicolor mode)
- `$D018`: Memory Control (screen/bitmap base addresses)
- `$D020`: Border color
- `$D021-$D024`: Background colors
- `$D015`: Sprite enable
- `$D027-$D02E`: Sprite colors

### Programming Graphics

Example BASIC program to enable bitmap mode:

```basic
10 REM ENABLE BITMAP MODE
20 POKE 53265, PEEK(53265) OR 32
30 POKE 53272, 8
40 REM CLEAR BITMAP
50 FOR I=8192 TO 16191:POKE I,0:NEXT
60 REM SET COLORS
70 FOR I=1024 TO 2023:POKE I,16:NEXT
80 REM DRAW PIXELS
90 POKE 8192,255
```

See `programs/bitmap_test.prg` and `programs/graphics_test.bas` for examples.

## Server Mode Commands

When running in server mode (with `--tcp-port` or `--udp-port`), you can send commands:

- `STATUS`: Get emulator status
- `STEP [N]`: Step N CPU cycles (default: 1)
- `RUN`: Start/resume emulation
- `MEMORY [start] [end]`: Read memory (hex addresses)
- `DUMP [start] [end]`: Dump memory as hex string
- `SCREEN`: Get current screen output
- `LOAD <file>`: Load a PRG file
- `STOP`: Stop emulation
- `QUIT` or `EXIT`: Exit the server

## Textual Interface

The emulator features a modern text-based UI when not in server mode:

- **C64 Display**: Shows the emulated C64 screen
- **Debug Panel**: Real-time debug log with timestamps
- **Status Bar**: Current emulator status

### Keyboard Shortcuts

- `Ctrl+X`: Quit the emulator
- `Ctrl+R`: Fill screen with random characters (debug)
- `Ctrl+K`: Dump screen memory to debug logs

## Error Handling

- If any ROM file fails to load, the emulator will:
  1. Stop the textual UI (if running)
  2. Print an error message
  3. Exit immediately with error code 1

- On automatic exit (e.g., max cycles reached), the emulator will:
  1. Capture the last 20 log messages
  2. Shut down the textual UI
  3. Print the captured logs to the console

## Architecture

The emulator consists of several key components:

- **C64**: Main emulator class
- **CPU6502**: 6502 CPU emulator
- **MemoryMap**: Memory management with ROM/RAM mapping
- **TextualInterface**: Text-based UI using Textual
- **EmulatorServer**: TCP/UDP server for remote control

## License

Licensed under the **BSD 3-Clause License**. See `LICENSE`.
