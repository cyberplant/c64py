# C64 Emulator (Python)

A Commodore 64 emulator implemented in Python with a text-based interface. This emulator focuses on text mode operation and can load and run PRG files, dump memory, and communicate via TCP/UDP.

## Features

- **6502 CPU Emulation**: Full 6502 instruction set implementation
- **Memory Management**: Complete C64 memory map with ROM/RAM mapping
- **I/O Devices**: VIC, SID, CIA1, CIA2 emulation
- **Text Mode Interface**: Beautiful textual UI using Rich and Textual libraries
- **Graphics Mode**: Optional pygame window for C64 display output
- **PRG File Loading**: Load and auto-run Commodore 64 programs
- **Server Mode**: TCP/UDP server for remote control
- **Debug Support**: UDP debug logging and detailed debug output
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

Run the emulator with a PRG file (auto-runs the program):
```bash
c64py program.prg
```

Run the emulator without a program (starts at BASIC prompt):
```bash
c64py
```

### Command Line Options

- `prg_file`: Optional PRG file to load and run
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

### Server Mode Commands

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
