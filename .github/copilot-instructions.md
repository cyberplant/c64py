# GitHub Copilot Instructions for c64py

## Project Overview

This is a Commodore 64 emulator implemented in Python with a text-based interface. The emulator provides:
- Complete 6502 CPU emulation with all instructions
- C64 memory mapping (64KB RAM with ROM overlays)
- I/O device emulation (VIC-II, SID, CIA1, CIA2)
- Text and graphics mode display
- PRG file loading and execution
- TCP/UDP server for remote control

## Architecture

### Core Components
- **C64** (`C64.py`): Main emulator class and CLI entry point
- **CPU6502** (`cpu.py`): 6502 CPU instruction set implementation
- **MemoryMap** (`memory.py`): Memory management with ROM/RAM banking
- **TextualInterface** (`ui.py`): Text-based UI using the Textual library
- **EmulatorServer** (`server.py`): TCP/UDP server for remote control
- **GraphicsRenderer** (`graphics.py`): Pygame-based graphics window

### Key Files
- `constants.py`: C64-specific constants (memory addresses, timing, etc.)
- `cpu_state.py`: CPU and timer state management
- `roms.py`: ROM file loading and detection
- `debug.py`: UDP debug logging

## Development Guidelines

### Python Version and Dependencies
- **Minimum Python version**: 3.9
- **Core dependencies**: `rich>=13.0.0`, `textual>=0.40.0`, `pygame>=2.6.1`
- Use type hints with `from __future__ import annotations` for all modules

### Code Style

#### Imports
- Always use `from __future__ import annotations` at the top of modules
- Use `TYPE_CHECKING` for import-time circular dependency resolution
- Group imports: standard library, third-party, then local modules
- Use explicit imports from local modules (e.g., `from .cpu import CPU6502`)

Example:
```python
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from .constants import SCREEN_MEM, COLOR_MEM
from .cpu_state import CPUState

if TYPE_CHECKING:
    from .debug import UdpDebugLogger
```

#### Documentation
- Use triple-quoted docstrings (`"""..."""`) for modules, classes, and methods
- Module docstrings should be brief (one line describing the module purpose)
- Class docstrings should describe the class purpose
- Method docstrings should describe what the method does (optional for simple getters/setters)

#### Type Hints
- Use type hints for function parameters and return types
- Use `Optional[T]` for values that can be `None`
- Use dataclasses with type hints for data structures

#### Naming Conventions
- Classes: `PascalCase` (e.g., `CPU6502`, `MemoryMap`)
- Functions/methods: `snake_case` (e.g., `execute_instruction`, `read_memory`)
- Constants: `UPPER_SNAKE_CASE` (e.g., `SCREEN_MEM`, `ROM_KERNAL_START`)
- Private methods/attributes: prefix with single underscore (e.g., `_read_vic`)

### Building and Testing

#### Building
```bash
python -m pip install --upgrade pip
python -m pip install build
python -m build
```

#### Installing for Development
```bash
pip install -r requirements.txt
pip install -e .
```

#### Smoke Testing
```bash
python -m pip install .
python -c "import c64py; import c64py.emulator; print(c64py.__version__)"
```

#### Running the Emulator
```bash
# Basic usage
c64py

# With a PRG file
c64py program.prg

# With debug output
c64py --debug

# In server mode
c64py --tcp-port 1234
```

### Project Structure
- The repository root **is** the Python package directory
- Import modules as `c64py.*` (e.g., `import c64py.emulator`)
- Package configuration uses `package-dir = { c64py = "." }` in `pyproject.toml`

### Memory and CPU Emulation
- Memory is 64KB (0x0000-0xFFFF)
- ROMs overlay RAM at specific addresses (BASIC, KERNAL, Character ROM)
- VIC, SID, CIA registers are memory-mapped I/O
- CPU state includes registers (A, X, Y, SP, PC) and flags (N, V, B, D, I, Z, C)
- Instructions return cycle counts for timing accuracy

### Common Patterns

#### Memory Access
```python
# Direct RAM access
value = memory.ram[address]

# Memory read/write with ROM banking
value = memory.read(address)
memory.write(address, value)

# VIC register access (bypasses banking)
value = memory.peek_vic(reg)
memory.poke_vic(reg, value)
```

#### CPU Execution
```python
# Execute single instruction
cycles = cpu.execute()

# Execute with cycle limit
while total_cycles < max_cycles:
    total_cycles += cpu.execute()
```

### Security Considerations
- Never commit ROM files (they are copyrighted)
- ROMs are loaded from user-specified directories or common VICE paths
- Validate file paths and sizes when loading PRG files
- Sanitize server input commands to prevent injection attacks

### Testing
- Currently, testing is limited to smoke tests (import and version check)
- Manual testing involves running the emulator with various PRG files
- The CI pipeline tests across Python 3.9, 3.10, 3.11, and 3.12

### Compatibility
- Support Python 3.9+ as specified in `pyproject.toml`
- Maintain compatibility with both PAL and NTSC timing standards
- Handle missing ROMs gracefully with clear error messages

## Common Tasks

### Adding a New CPU Instruction
1. Add opcode handling in `cpu.py` in the `execute_opcode()` method
2. Return the correct cycle count
3. Update flags (N, Z, C, V) as appropriate
4. Add comments explaining addressing mode and operation

### Adding a New I/O Register
1. Add constant to `constants.py`
2. Update `MemoryMap` read/write methods in `memory.py`
3. Handle special behavior (e.g., read-clear registers)
4. Document the register's purpose

### Modifying the UI
1. Update `TextualInterface` in `ui.py` for Textual-based UI
2. Update `GraphicsRenderer` in `graphics.py` for pygame window
3. Test with both `--graphics` and without for text mode

## License
This project is licensed under the BSD 3-Clause License. All contributions must be compatible with this license.
