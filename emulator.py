"""
C64 Emulator Main Class
"""

from __future__ import annotations

import os
import struct
import sys
import threading
import time
from typing import Dict, Optional, Tuple, Union

from rich.console import Console
from rich.text import Text

from .constants import (
    COLOR_MEM,
    BLNSW,
    BLNCT,
    BORDER_WIDTH,
    BORDER_HEIGHT,
    CURSOR_COL_ADDR,
    CURSOR_ROW_ADDR,
    KEYBOARD_BUFFER_BASE,
    KEYBOARD_BUFFER_LEN_ADDR,
    ROM_KERNAL_START,
    SCREEN_MEM,
    SCREEN_COLS,
    SCREEN_ROWS,
)
from .cpu import CPU6502
from .debug import UdpDebugLogger
from .memory import MemoryMap
from .roms import REQUIRED_ROMS
from .ui import TextualInterface

class C64:
    """Main C64 emulator"""

    # C64 16-color palette (RGB), Pepto/VICE-like approximation.
    # Index matches C64 color codes (0-15) used by BASIC/VIC.
    _C64_PALETTE_RGB: Tuple[Tuple[int, int, int], ...] = (
        (0x00, 0x00, 0x00),  # 0  black
        (0xFF, 0xFF, 0xFF),  # 1  white
        (0x88, 0x00, 0x00),  # 2  red
        (0xAA, 0xFF, 0xEE),  # 3  cyan
        (0xCC, 0x44, 0xCC),  # 4  purple
        (0x00, 0xCC, 0x55),  # 5  green
        (0x00, 0x00, 0xAA),  # 6  blue
        (0xEE, 0xEE, 0x77),  # 7  yellow
        (0xDD, 0x88, 0x55),  # 8  orange
        (0x66, 0x44, 0x00),  # 9  brown
        (0xFF, 0x77, 0x77),  # 10 light red
        (0x33, 0x33, 0x33),  # 11 dark gray
        (0x77, 0x77, 0x77),  # 12 gray
        (0xAA, 0xFF, 0x66),  # 13 light green
        (0x00, 0x88, 0xFF),  # 14 light blue
        (0xBB, 0xBB, 0xBB),  # 15 light gray
    )

    def __init__(self, interface_factory=None):
        self.memory = MemoryMap()
        if interface_factory is None:
            self.interface = TextualInterface(self)
        else:
            self.interface = interface_factory(self)

        # Create CPU with interface reference
        self.cpu = CPU6502(self.memory, self.interface)

        self.running = False
        self.text_screen = [[' '] * 40 for _ in range(25)]
        self.text_colors = [[7] * 40 for _ in range(25)]  # Default: yellow on blue
        self.debug = False
        self.no_colors = False  # ANSI color output enabled by default
        self.udp_debug = None  # Will be set if UDP debugging is enabled
        self.screen_update_thread = None
        self.screen_update_interval = 0.1  # Update screen every 100ms
        self.screen_lock = threading.Lock()
        self.current_cycles = 0  # Track current cycle count
        self.program_loaded = False  # Track if a program was loaded via command line
        self.prg_file_path = None  # Store PRG file path to load after BASIC is ready

        # Backward compatibility
        self.rich_interface = self.interface

    def load_roms(self, rom_dir: str) -> None:
        """Load C64 ROM files

        Args:
            rom_dir: Absolute path to directory containing ROM files
        """
        import os

        def _read_rom_file(filename: str) -> bytes:
            """
            Read a ROM file from rom_dir.

            Supports both c64py's canonical dot-names and common VICE dash-names.
            """
            # Build name_candidates from REQUIRED_ROMS to maintain single source of truth
            name_candidates = (filename,)
            for spec in REQUIRED_ROMS:
                if spec.filename == filename:
                    name_candidates = (spec.filename, *spec.aliases)
                    break

            tried_paths = []
            for name in name_candidates:
                path = os.path.join(rom_dir, name)
                tried_paths.append(path)
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        return f.read()
            tried_paths_str = ", ".join(tried_paths) if tried_paths else "<no paths constructed>"
            raise FileNotFoundError(
                f"ROM not found. Tried candidate names {list(name_candidates)} at paths: {tried_paths_str}"
            )

        try:
            # Load BASIC ROM
            self.memory.basic_rom = _read_rom_file("basic.901226-01.bin")
            if self.rich_interface:
                self.rich_interface.add_debug_log(f"💾 Loaded BASIC ROM: {len(self.memory.basic_rom)} bytes")

            # Load KERNAL ROM
            self.memory.kernal_rom = _read_rom_file("kernal.901227-03.bin")
            if self.rich_interface:
                self.rich_interface.add_debug_log(f"💾 Loaded KERNAL ROM: {len(self.memory.kernal_rom)} bytes")

            # Set reset vector in RAM (KERNAL ROM has it at $FFFC-$FFFD)
            if self.memory.kernal_rom and len(self.memory.kernal_rom) >= (0x10000 - ROM_KERNAL_START):
                reset_offset = 0xFFFC - ROM_KERNAL_START
                reset_low = self.memory.kernal_rom[reset_offset]
                reset_high = self.memory.kernal_rom[reset_offset + 1]
                self.memory.ram[0xFFFC] = reset_low
                self.memory.ram[0xFFFD] = reset_high
                if self.rich_interface:
                    self.rich_interface.add_debug_log(f"🔄 Reset vector: ${reset_high:02X}{reset_low:02X}")

            # Load Character ROM
            self.memory.char_rom = _read_rom_file("characters.901225-01.bin")
            if self.rich_interface:
                self.rich_interface.add_debug_log(f"💾 Loaded Character ROM: {len(self.memory.char_rom)} bytes")
        except Exception:
            # Stop textual UI if it exists so error is visible to user.
            if hasattr(self, "interface") and hasattr(self.interface, "exit"):
                try:
                    self.interface.exit()
                except Exception as exit_err:
                    # Best-effort cleanup: log failure to exit interface but do not mask the original error.
                    sys.stderr.write(f"Failed to cleanly exit interface: {exit_err}\n")
            raise

        # Initialize C64 state (sets memory config $01 = 0x37)
        self._initialize_c64()

        # Set CPU PC from reset vector (after ROMs are loaded and memory is initialized)
        # Use _read_word to ensure we read from KERNAL ROM correctly
        reset_addr = self.cpu._read_word(0xFFFC)
        self.cpu.state.pc = reset_addr
        if self.rich_interface:
            self.rich_interface.add_debug_log(f"🔄 CPU reset vector: ${reset_addr:04X}")

    def _initialize_c64(self) -> None:
        """Initialize C64 to a known state"""
        # Initialize RAM with C64-like pattern (real C64 DRAM has pattern: half $00, half $FF in 64-byte blocks)
        # For debugging, zero $0002-$03FF as per RAMTAS at $FD50
        # Pattern: 64 bytes of $00, then 64 bytes of $FF, repeating
        for addr in range(0x0002, 0x0400):
            # RAMTAS zeros this area
            self.memory.ram[addr] = 0x00

        # Initialize rest of RAM with pattern (real C64 DRAM characteristic)
        # Pattern: 64 bytes of $00, then 64 bytes of $FF, repeating
        for addr in range(0x0400, 0x10000):
            if addr < 0x0800 or (addr >= 0xA000 and addr < 0xC000) or addr >= 0xE000:
                # Skip screen/color memory ($0400-$07FF) and ROM areas
                # Apply pattern to other RAM areas
                block = (addr // 64) % 2
                if block == 0:
                    self.memory.ram[addr] = 0x00
                else:
                    self.memory.ram[addr] = 0xFF

        # Write to $0000 during reset (as JSC64 does)
        # This is part of the 6510 processor port initialization
        self.memory.ram[0x00] = 0x2F

        # Memory configuration register ($01)
        # Bits 0-2: Memory configuration
        # 0x37 = %00110111 = BASIC ROM + KERNAL ROM + I/O enabled
        self.memory.ram[0x01] = 0x37

        # Initialize screen memory with spaces (don't pre-fill - let KERNAL/BASIC do it)
        # The C64 typically clears screen during initialization
        for addr in range(SCREEN_MEM, SCREEN_MEM + 1000):
            self.memory.ram[addr] = 0x20  # Space character

        # Initialize color memory (default: light blue = 14, but we'll use white = 1)
        for addr in range(COLOR_MEM, COLOR_MEM + 1000):
            # Default C64 power-on text is light blue on blue.
            # Color RAM is 4-bit and lives in I/O space; it is handled by MemoryMap.
            self.memory.ram[addr] = 0x0E  # Light blue

        # Initialize VIC registers (simplified)
        # VIC register $D018: Screen and character memory
        # Bit 1-3: Screen memory (default $0400 = %000 = 0)
        # Bit 4-7: Character memory (default $1000 = %010 = 2)
        # So $D018 = %00010000 = $10
        # Seed key VIC state so early frames render like a real C64, even before ROM init.
        # Use memory-mapped I/O writes so the values land in the VIC register model.
        # Seed VIC state directly (independent of banking).
        self.memory.poke_vic(0x18, 0x10)  # Screen at $0400, chars at $1000
        self.memory.poke_vic(0x20, 0x0E)  # Border: light blue
        self.memory.poke_vic(0x21, 0x06)  # Background: blue

        # Initialize stack pointer
        self.cpu.state.sp = 0xFF

        # Initialize zero-page variables used by KERNAL
        # $C3-$C4: Temporary pointer used by vector copy routine
        # Typically initialized to point to RAM vector area (0x0314)
        self.memory.ram[0xC3] = 0x14  # Temporary pointer (low)
        self.memory.ram[0xC4] = 0x03  # Temporary pointer (high) - points to $0314

        # Initialize some zero-page variables
        self.memory.ram[0x0288] = 0x0E  # Cursor color (light blue)
        self.memory.ram[0x0286] = 0x0E  # Current text color (light blue)
        # Cursor blink (machine-controlled; UI should follow this)
        # bit0 = enabled, bit7 = visible
        self.memory.ram[BLNSW] = 0x81
        self.memory.ram[BLNCT] = 0

        # Initialize cursor position (points to screen start)
        # $D1/$D2 store the cursor address (low/high bytes)
        self.memory.ram[0xD1] = SCREEN_MEM & 0xFF  # Cursor address low byte
        self.memory.ram[0xD2] = (SCREEN_MEM >> 8) & 0xFF  # Cursor address high byte
        # Also initialize cursor row/col variables
        self.memory.ram[0xD3] = 0  # Cursor row (0-24)
        self.memory.ram[0xD8] = 0  # Cursor column (0-39)

        # Initialize KERNAL reset vector at $8000-$8001 to point to BASIC cold start
        # The KERNAL does JMP ($8000) to jump to BASIC after initialization
        # BASIC cold start is typically at $A483 (standard C64 BASIC entry point)
        basic_cold_start = 0xA483
        self.memory.ram[0x8000] = basic_cold_start & 0xFF
        self.memory.ram[0x8001] = (basic_cold_start >> 8) & 0xFF

        # Initialize BASIC pointers for empty program
        basic_start = 0x0801
        # $2B/$2C: Start of BASIC program (VARPTR)
        self.memory.ram[0x002B] = basic_start & 0xFF
        self.memory.ram[0x002C] = (basic_start >> 8) & 0xFF
        # $2D/$2E: End of BASIC program (VAREND)
        self.memory.ram[0x002D] = basic_start & 0xFF
        self.memory.ram[0x002E] = (basic_start >> 8) & 0xFF
        # $2F/$30: Start of BASIC arrays (ARRPTR)
        self.memory.ram[0x002F] = basic_start & 0xFF
        self.memory.ram[0x0030] = (basic_start >> 8) & 0xFF
        # $31/$32: End of BASIC arrays (ARREND)
        self.memory.ram[0x0031] = basic_start & 0xFF
        self.memory.ram[0x0032] = (basic_start >> 8) & 0xFF
        # $33/$34: End of free memory (STREND) - should point to end of available memory
        # This will be set by MEMTOP routine, but initialize to a safe value
        # MEMTOP typically returns $9FFF for 64K system
        memtop = 0x9FFF  # Default top of BASIC RAM
        self.memory.ram[0x0033] = memtop & 0xFF
        self.memory.ram[0x0034] = (memtop >> 8) & 0xFF

        # Mark end of BASIC program (empty program marker)
        # $0801-$0802: Link to next line ($00 $00 = end of program)
        # This is CRITICAL - if this is not $00 $00, BASIC will try to execute garbage as a program
        # The link pointer must be $00 $00 to indicate no program
        # NOTE: This will be overwritten when a PRG file is loaded at $0801
        self.memory.ram[0x0801] = 0x00
        self.memory.ram[0x0802] = 0x00

        # Also ensure $0803+ is cleared to prevent garbage being interpreted as tokens
        # Clear a reasonable amount of BASIC program area
        # NOTE: This will be overwritten when a PRG file is loaded
        for addr in range(0x0803, 0x0900):
            self.memory.ram[addr] = 0x00

        # Initialize current line number for direct mode
        # $39/$3A: Current line number (low/high)
        # $3A = $FF means direct mode (no line number)
        self.memory.ram[0x0039] = 0x00  # Low byte
        self.memory.ram[0x003A] = 0xFF  # High byte = $FF means direct mode

        # Initialize keyboard buffer (for GETIN)
        self.memory.ram[0xC6] = 0  # Number of characters in keyboard buffer
        # Clear keyboard buffer area ($0277-$0280)
        for i in range(10):
            self.memory.ram[0x0277 + i] = 0

        # Initialize BASIC input buffer (for CHRIN keyboard input)
        # $0200-$0258: BASIC input buffer (89 bytes)
        # $029B: Input buffer read pointer (0 = empty, >0 = chars available)
        # $029C: Line editing length counter (temporary, during line editing)
        self.memory.ram[0x029B] = 0  # Input buffer pointer (0 = empty)
        self.memory.ram[0x029C] = 0  # Line editing length (0 = no line being edited)
        # Clear BASIC input buffer
        for i in range(89):
            self.memory.ram[0x0200 + i] = 0

        # Initialize zero-page status register $6C (used by KERNAL error handler)
        # This is typically initialized to 0 on boot
        # The KERNAL checks this at $FE6E with SBC $6C - if result is 0, it halts
        self.memory.ram[0x6C] = 0  # Status register (typically 0 = no error)

        # Initialize KERNAL vectors to defaults
        # These are copied from KERNAL ROM during RESTOR routine
        # We initialize them here to prevent crashes during boot

        # KERNAL RAM vectors ($0300-$0334)
        # These should match the default values from KERNAL ROM
        kernal_vectors = {
            0x0300: 0xE45B,  # CINT - Initialize screen editor
            0x0302: 0xFE4C,  # IOINIT - Initialize I/O
            0x0304: 0xFDA3,  # RAMTAS - Initialize RAM
            0x0306: 0xED50,  # RESTOR - Restore KERNAL vectors
            0x0308: 0xFD4C,  # VECTOR - Change KERNAL vectors
            0x030A: 0x15FD,  # SETMSG - Set system error display
            0x030C: 0xED1A,  # LSTNSA - Send LIST to serial bus
            0x030E: 0xFD4C,  # TALKSA - Send TALK to serial bus
            0x0310: 0x18FE,  # MEMTOP - Set top of memory
            0x0312: 0x4CB9,  # MEMBOT - Set bottom of memory
            0x0314: 0xEA31,  # IRQ - IRQ handler
            0x0316: 0xFE66,  # BRK - BRK handler
            0x0318: 0xFE47,  # NMI - NMI handler
            0x031A: 0xFE4C,  # OPEN - Open file
            0x031C: 0x34FE,  # CLOSE - Close file
            0x031E: 0x4C87,  # CHKIN - Set input channel
            0x0320: 0xEA4C,  # CHKOUT - Set output channel
            0x0322: 0x21FE,  # CLRCHN - Clear channels
            0x0324: 0x4C13,  # CHRIN - Input character ($FFCF)
            0x0326: 0xEE4C,  # CHROUT - Output character
            0x0328: 0xDDED,  # STOP - Check stop key
            0x032A: 0x4CEF,  # GETIN - Get character from keyboard
            0x032C: 0xED4C,  # CLALL - Clear file table
            0x032E: 0xFEED,  # UDTIM - Update clock
            0x0330: 0x4C0C,  # SCREEN - Get screen size
            0x0332: 0xED4C,  # PLOT - Set cursor position
            0x0334: 0x09ED,  # IOBASE - Get I/O base address
        }

        for addr, value in kernal_vectors.items():
            self.memory.ram[addr] = value & 0xFF
            self.memory.ram[addr + 1] = (value >> 8) & 0xFF

        # Initialize CIA1 timers (typical C64 boot values)
        # Timer A is used for jiffy clock (exactly 60Hz)
        # PAL C64: ~1.022727 MHz CPU, so 60Hz = 17045.45 cycles
        # We use 17045 for accuracy
        if self.memory.video_standard == "pal":
            cpu_hz = 1022727  # PAL C64 CPU frequency
        else:
            cpu_hz = 985248   # NTSC C64 CPU frequency

        jiffy_cycles = cpu_hz // 60  # Exact 60Hz timing
        self.memory.cia1_timer_a.latch = jiffy_cycles
        self.memory.cia1_timer_a.counter = jiffy_cycles
        self.memory.cia1_timer_a.running = True   # Enable jiffy clock
        self.memory.cia1_timer_a.irq_enabled = True

        # Timer B can be used for other purposes
        self.memory.cia1_timer_b.latch = 0xFFFF
        self.memory.cia1_timer_b.counter = 0xFFFF

        if self.rich_interface:
            self.rich_interface.add_debug_log("🎮 C64 initialized")

    def load_prg(self, prg_path: str) -> None:
        """Load a PRG file into memory"""
        with open(prg_path, "rb") as f:
            data = f.read()

        if len(data) < 2:
            raise ValueError("PRG file too small")

        load_addr = data[0] | (data[1] << 8)
        prg_data = data[2:]

        # Write PRG data to memory
        for i, byte_val in enumerate(prg_data):
            addr = (load_addr + i) & 0xFFFF
            self.memory.write(addr, byte_val)

        self.program_loaded = True
        end_addr = load_addr + len(prg_data)
        print(f"Loaded PRG: {len(prg_data)} bytes at ${load_addr:04X}, end at ${end_addr:04X}")

        # If loaded at $0801 (BASIC), set up BASIC pointers
        if load_addr == 0x0801:
            # Set BASIC start pointer ($2B/$2C) - points to start of program
            self.memory.ram[0x002B] = 0x01
            self.memory.ram[0x002C] = 0x08

            # Set BASIC end pointer ($2D/$2E) - points to end of program
            # This should point to the address AFTER the $00 $00 end marker
            self.memory.ram[0x002D] = end_addr & 0xFF
            self.memory.ram[0x002E] = (end_addr >> 8) & 0xFF

            # Debug: Log the BASIC pointers
            if self.interface:
                self.interface.add_debug_log(f"📝 BASIC start: ${self.memory.ram[0x002B] | (self.memory.ram[0x002C] << 8):04X}")
                self.interface.add_debug_log(f"📝 BASIC end: ${self.memory.ram[0x002D] | (self.memory.ram[0x002E] << 8):04X}")
                # Check if program has proper end marker
                if end_addr >= 2:
                    end_marker_low = self.memory.read(end_addr - 2)
                    end_marker_high = self.memory.read(end_addr - 1)
                    if end_marker_low == 0x00 and end_marker_high == 0x00:
                        self.interface.add_debug_log("✅ Program has proper $00 $00 end marker")
                    else:
                        self.interface.add_debug_log(f"⚠️ Program end marker: ${end_marker_low:02X} ${end_marker_high:02X} (expected $00 $00)")
                # Show first few bytes of program
                first_bytes = [f"${self.memory.read(0x0801 + i):02X}" for i in range(min(16, len(prg_data)))]
                self.interface.add_debug_log(f"📝 First bytes at $0801: {', '.join(first_bytes)}")

    def _screen_update_worker(self) -> None:
        """Worker thread that periodically updates the screen"""
        update_count = 0
        while self.running:
            try:
                self._update_text_screen()
                update_count += 1

                # Textual interface updates screen automatically, no manual updates needed

                # Show screen summary periodically when debug is enabled
                if hasattr(self, 'debug') and self.debug and update_count % 10 == 0:
                    # Count non-space characters to see if there's content
                    non_spaces = 0
                    for row in self.text_screen:
                        for char in row:
                            if char != ' ':
                                non_spaces += 1

                    debug_msg = f"📺 Screen update #{update_count}: {non_spaces} non-space characters"
                    if self.interface:
                        self.interface.add_debug_log(debug_msg)

                    # Show first line if there's content
                    if non_spaces > 0:
                        first_line = ''.join(self.text_screen[0]).rstrip()
                        if first_line:
                            line_msg = f"📝 First line: '{first_line}'"
                            if self.interface:
                                self.interface.add_debug_log(line_msg)

                    # Show raw screen memory sample
                    screen_sample = []
                    for i in range(16):
                        screen_sample.append(f"{self.memory.read(0x0400 + i):02X}")
                    mem_msg = f"💾 Screen mem ($0400): {' '.join(screen_sample)}"
                    if self.interface:
                        self.interface.add_debug_log(mem_msg)

                # Update Textual debug panel (updates happen automatically in Textual)

                time.sleep(self.screen_update_interval)
            except Exception as e:
                error_msg = f"❌ Screen update error: {e}"
                if self.interface:
                    self.interface.add_debug_log(error_msg)
                else:
                    print(error_msg)

    def run(self, max_cycles: Optional[int] = None) -> None:
        """Run the emulator"""
        self.running = True
        cycles = 0
        last_pc = None
        stuck_count = 0
        pc_history = []  # Track recent PCs for debugging

        # Start screen update thread
        self.screen_update_thread = threading.Thread(target=self._screen_update_worker, daemon=True)
        self.screen_update_thread.start()

        # Log start of execution
        if self.udp_debug and self.udp_debug.enabled:
            self.udp_debug.send('execution_start', {
                'max_cycles': max_cycles,
                'initial_pc': self.cpu.state.pc,
                'initial_pc_hex': f'${self.cpu.state.pc:04X}'
            })

        # Main CPU emulation loop (runs as fast as possible)
        last_time = time.time()
        last_cycle_check = 0

        while self.running:
            pc = self.cpu.state.pc

            # Load program if pending (after BASIC boot completes)
            if self.prg_file_path and not hasattr(self, '_program_loaded_after_boot'):
                # BASIC is ready - load the program now (after boot has completed)
                # Wait until we're past boot sequence (cycles > 2020000)
                if cycles > 2020000:
                    try:
                        self.load_prg(self.prg_file_path)
                        self.prg_file_path = None  # Clear path after loading
                        self._program_loaded_after_boot = True
                        if self.interface:
                            self.interface.add_debug_log("💾 Program loaded after BASIC boot completed")
                    except Exception as e:
                        if self.interface:
                            self.interface.add_debug_log(f"❌ Failed to load program: {e}")
                        self.prg_file_path = None  # Clear path even on error

            step_cycles = self.cpu.step(self.udp_debug, cycles)
            cycles += step_cycles
            self.current_cycles = cycles

            # Check if we've reached max cycles
            if max_cycles is not None and cycles >= max_cycles:
                if hasattr(self, 'autoquit') and self.autoquit:
                    self.running = False
                    stop_reason = "max_cycles_autoquit"
                else:
                    stop_reason = "max_cycles_reached"
                break

            # Textual interface updates automatically, no manual updates needed

            # Calculate cycles per second periodically
            if cycles - last_cycle_check >= 100000:
                current_time = time.time()
                elapsed = current_time - last_time
                if elapsed > 0:
                    self.cycles_per_second = (cycles - last_cycle_check) / elapsed
                last_time = current_time
                last_cycle_check = cycles

            # Detect if we're stuck (but ignore if CPU is stopped - that's expected)
            if self.cpu.state.stopped:
                # CPU is stopped (KIL instruction) - this is expected, just break
                if self.debug:
                    debug_msg = f"🛑 CPU stopped at PC=${self.cpu.state.pc:04X} (KIL instruction)"
                    if self.rich_interface:
                        self.rich_interface.add_debug_log(debug_msg)
                break
            elif self.cpu.state.pc == last_pc:
                # CHRIN ($FFCF) blocks when keyboard buffer is empty - this is expected behavior
                # Don't count it as stuck
                if self.cpu.state.pc != 0xFFCF:
                    stuck_count += 1
                    if stuck_count > 1000:
                        if self.debug:
                            opcode = self.memory.read(self.cpu.state.pc)
                            debug_msg1 = f"⚠️ PC stuck at ${self.cpu.state.pc:04X} (opcode ${opcode:02X}) for {stuck_count} steps"
                            debug_msg2 = "  This usually means an opcode is not implemented or not advancing PC correctly"
                            if self.rich_interface:
                                self.rich_interface.add_debug_log(debug_msg1)
                                self.rich_interface.add_debug_log(debug_msg2)
                        # Don't try to advance - this masks the real problem
                        # Instead, stop execution to prevent infinite loops
                        self.running = False
                        break
                else:
                    # PC is at CHRIN - reset stuck count since blocking is expected
                    stuck_count = 0
            else:
                stuck_count = 0
            last_pc = self.cpu.state.pc
            pc_history.append(self.cpu.state.pc)
            if len(pc_history) > 20:  # Keep last 20 PCs
                pc_history.pop(0)

            # Periodic status logging (less frequent to avoid overhead)
            if self.debug and cycles % 100000 == 0:
                state = self.get_cpu_state()
                debug_msg = f"🔄 Cycles: {cycles}, PC=${state['pc']:04X}, A=${state['a']:02X}"
                if self.rich_interface:
                    self.rich_interface.add_debug_log(debug_msg)

            # Log periodic status if UDP debug is enabled (less frequent)
            if self.udp_debug and self.udp_debug.enabled and cycles % 100000 == 0:
                state = self.get_cpu_state()
                self.udp_debug.send('status', {
                    'cycles': cycles,
                    'pc': state['pc'],
                    'pc_hex': f'${state["pc"]:04X}',
                    'a': state['a'],
                    'x': state['x'],
                    'y': state['y'],
                    'sp': state['sp'],
                    'p': state['p']
                })

            # Debug: Log when entering key boot routines
            if self.debug and pc in [0xFDA3, 0xFD50, 0xFD15, 0xFF5B]:
                routine_name = {
                    0xFDA3: "IOINIT",
                    0xFD50: "RAMTAS",
                    0xFD15: "RESTOR",
                    0xFF5B: "CINT"
                }.get(pc, "UNKNOWN")
                if self.rich_interface:
                    self.rich_interface.add_debug_log(f"🔧 ENTERING {routine_name} at PC=${pc:04X}")
                else:
                    print(f"🔧 ENTERING {routine_name} at cycle {cycles}, PC=${pc:04X}")
                if pc == 0xFD15:  # RESTOR
                    # Check stack contents
                    sp = self.cpu.state.sp
                    if sp < 0xFF:
                        ret_low = self.memory.read(0x100 + ((sp + 1) & 0xFF))
                        ret_high = self.memory.read(0x100 + ((sp + 2) & 0xFF))
                        return_addr = ret_low | (ret_high << 8)
                        debug_msg = f"   Stack SP=${sp:02X}, return addr=${return_addr:04X}"
                        if self.rich_interface:
                            self.rich_interface.add_debug_log(debug_msg)
                        print(debug_msg)
                elif pc == 0xFF5B:  # CINT - log opcodes it executes
                    print(f"   CINT will execute opcodes...")

            # Debug: Show raster line during CINT
            if self.debug and pc >= 0xFF5B and pc <= 0xFFFF:
                if cycles % 10000 == 0:  # Log every 10k cycles during CINT
                    raster = self.memory.raster_line
                    print(f"📺 CINT: raster=${raster:03X}, cycle={cycles}")

            # Debug: Log when PC reaches dangerous areas
            if self.debug and pc == 0x0000:
                debug_msg = f"🚨 DANGER: PC reached $0000"
                if self.rich_interface:
                    self.rich_interface.add_debug_log(debug_msg)
                print(f"{debug_msg} at cycle {cycles}")
                # Show recent PC history
                history_msg = f"Recent PCs: {[f'${p:04X}' for p in pc_history[-10:]]}"
                if self.rich_interface:
                    self.rich_interface.add_debug_log(history_msg)
                print(f"   {history_msg}")

            # Debug: Log RTS from boot routines
            if self.debug and pc == 0x60 and last_pc in [0xFDA3, 0xFD50, 0xFD15, 0xFF5B]:  # RTS
                routine_name = {
                    0xFDA3: "IOINIT",
                    0xFD50: "RAMTAS",
                    0xFD15: "RESTOR",
                    0xFF5B: "CINT"
                }.get(last_pc, "UNKNOWN")
                if self.rich_interface:
                    self.rich_interface.add_debug_log(f"✅ COMPLETED {routine_name}")
                print(f"✅ COMPLETED {routine_name} at cycle {cycles}")

            # Debug: Log post-boot sequence
            if pc == 0xFCFE:  # CLI
                print(f"🔓 CLI (enable interrupts) at cycle {cycles}")
                print(f"   Next PC should be FCFF, I flag was {self.cpu.state.p & 0x04}")
            elif pc == 0xFCFF:  # JMP ($A000)
                a000_low = self.memory.read(0xA000)
                a000_high = self.memory.read(0xA001)
                jump_target = a000_low | (a000_high << 8)
                print(f"🏃 JMP (\\$A000) -> \\${jump_target:04X} at cycle {cycles}")
                if jump_target == 0xFCF8:
                    print(f"   🚨 DANGER: Jump target is boot start! Infinite loop!")
                elif jump_target == 0:
                    print(f"   🚨 ERROR: Jump target is 0! Invalid BASIC entry point!")
                # Log that we're about to jump
                print(f"   About to set PC to \\${jump_target:04X}")
                print(f"   A000 content: \\${a000_low:02X} \\${a000_high:02X}")
            elif pc >= 0xFCFE and pc <= 0xFD02:  # Log all instructions in boot cleanup
                if not self.rich_interface:  # Only print if Rich interface is not active
                    print(f"📍 Boot cleanup: PC=\\${pc:04X}, opcode=\\${self.memory.read(pc):02X}, cycle {cycles}")

            # Debug: Track entry to BASIC
            if pc == 0xE394:  # BASIC cold start entry point
                print(f"📚 Entered BASIC cold start at \\${pc:04X} (cycle {cycles})")

            # Debug: Track execution in BASIC ROM
            if 0xA000 <= pc <= 0xBFFF and cycles > 2020000:  # In BASIC ROM
                if cycles % 50000 == 0:  # Log occasionally
                    print(f"📖 BASIC executing at \\${pc:04X} (cycle {cycles})")

            # Debug: Why is RESTOR called repeatedly?
            if pc == 0xFD15 and cycles > 2010000:  # RESTOR called after boot should be done
                print(f"🔄 RESTOR called again at cycle {cycles} - investigating...")
                # Check stack to see who called it
                sp = self.cpu.state.sp
                if sp < 0xFF:
                    ret_low = self.memory.read(0x100 + ((sp + 1) & 0xFF))
                    ret_high = self.memory.read(0x100 + ((sp + 2) & 0xFF))
                    return_addr = ret_low | (ret_high << 8)
                    print(f"   Return address on stack: \\${return_addr:04X}")
                    if return_addr == 0xFCFB:
                        print(f"   ✅ Called from boot sequence (FCFB)")
                    else:
                        print(f"   ❓ Called from unexpected address \\${return_addr:04X}")

        # Determine stop reason
        stop_reason = "unknown"
        if self.cpu.state.stopped:
            stop_reason = "cpu_stopped"
        elif max_cycles is not None and cycles >= max_cycles:
            stop_reason = "max_cycles_reached"
        elif not self.running:
            stop_reason = "stuck_pc"

        # Log end of execution
        if self.udp_debug and self.udp_debug.enabled:
            self.udp_debug.send('execution_end', {
                'total_cycles': cycles,
                'final_pc': self.cpu.state.pc,
                'final_pc_hex': f'${self.cpu.state.pc:04X}',
                'stop_reason': stop_reason,
                'cpu_stopped': self.cpu.state.stopped,
                'max_cycles': max_cycles,
                'running': self.running
            })

        # Final screen update
        self._update_text_screen()

    def _petscii_to_screen_code(self, petscii_char: int) -> int:
        """Convert PETSCII character to C64 screen code"""
        if petscii_char < 32:
            # Control characters and symbols
            return petscii_char
        elif petscii_char < 64:
            # A-Z, symbols
            return petscii_char
        elif petscii_char < 96:
            # a-z (convert to screen codes 33-58)
            return petscii_char - 64
        elif petscii_char < 128:
            # More symbols and graphics
            return petscii_char - 32
        elif petscii_char < 160:
            # Reverse graphics
            return petscii_char - 128
        elif petscii_char < 192:
            # More symbols
            return petscii_char - 64
        else:
            # Uppercase graphics
            return petscii_char - 128

    def _update_text_screen(self) -> None:
        """Update text screen from screen memory (thread-safe)"""
        screen_base = SCREEN_MEM
        color_base = COLOR_MEM

        # Debug: screen update
        #if hasattr(self, 'interface') and self.interface:
            #self.interface.add_debug_log("🎨 Updating text screen from memory")

        # Use lock to ensure thread-safe access
        with self.screen_lock:
            for row in range(25):
                for col in range(40):
                    addr = screen_base + row * 40 + col
                    char_code = self.memory.read(addr)
                    color_code = self.memory.read(color_base + row * 40 + col) & 0x0F

                    # Convert C64 screen codes to ASCII
                    # C64 screen codes: PETSCII screen codes
                    if char_code == 0x00:
                        char = '@'
                    elif 0x01 <= char_code <= 0x1A:
                        char = chr(ord('A') + char_code - 1)
                    elif 0x1B <= char_code <= 0x1F:
                        char = chr(ord('[') + char_code - 0x1B)  # [\]^_
                    elif char_code == 0x20:
                        char = ' '
                    elif 0x21 <= char_code <= 0x2F:
                        # Punctuation: ! " # $ % & ' ( ) * + , - . /
                        punct = '!\"#$%&\'()*+,-./'
                        if char_code <= 0x20 + len(punct) - 1:
                            char = punct[char_code - 0x21]
                        else:
                            char = chr(char_code)
                    elif 0x30 <= char_code <= 0x39:
                        char = chr(ord('0') + char_code - 0x30)
                    elif 0x3A <= char_code <= 0x40:
                        char = chr(char_code)  # : ; < = > ? @
                    elif 0x41 <= char_code <= 0x5A:
                        char = chr(char_code)  # A-Z
                    elif 0x5B <= char_code <= 0x5F:
                        char = chr(ord('[') + char_code - 0x5B)  # [\]^_
                    elif char_code >= 0x60 and char_code <= 0x7E:
                        char = chr(char_code - 0x60) if char_code - 0x60 <= 0x1F else chr(char_code)
                    elif char_code == 0x7F:
                        char = chr(0x7F)  # DEL
                    else:
                        char = ' '

                    self.text_screen[row][col] = char
                    self.text_colors[row][col] = color_code

    @classmethod
    def _c64_color_to_rich_rgb(cls, color_code: int) -> str:
        """Convert a C64 color code (0-15) to a Rich rgb(...) string."""
        r, g, b = cls._C64_PALETTE_RGB[color_code & 0x0F]
        return f"rgb({r},{g},{b})"

    def _render_text_screen_rich(self) -> Text:
        """Render text screen as a Rich Text renderable with C64 colors."""
        # VIC-II background color (applies to the whole screen in standard text mode)
        # IMPORTANT: render should reflect VIC state, not CPU-visible banking.
        background_color = self.memory.peek_vic(0x21) & 0x0F
        bg_style = self._c64_color_to_rich_rgb(background_color)
        border_color = self.memory.peek_vic(0x20) & 0x0F
        border_style = self._c64_color_to_rich_rgb(border_color)
        border_cell_style = f"{border_style} on {border_style}"

        with self.screen_lock:
            screen_text = Text()
            # Draw a simple 1-character border around the 40x25 screen.
            # C64 border is a solid color region; we emulate it with spaces.
            full_cols = SCREEN_COLS + BORDER_WIDTH * 2

            # Top border
            for _ in range(BORDER_HEIGHT):
                screen_text.append(" " * full_cols, style=border_cell_style)
                screen_text.append("\n")

            for row in range(SCREEN_ROWS):
                # Left border
                screen_text.append(" " * BORDER_WIDTH, style=border_cell_style)
                for col in range(SCREEN_COLS):
                    char = self.text_screen[row][col]
                    fg = self.text_colors[row][col] & 0x0F
                    fg_style = self._c64_color_to_rich_rgb(fg)
                    screen_text.append(char, style=f"{fg_style} on {bg_style}")
                # Right border
                screen_text.append(" " * BORDER_WIDTH, style=border_cell_style)
                if row < (SCREEN_ROWS - 1):
                    screen_text.append("\n")

            # Bottom border
            screen_text.append("\n")
            for i in range(BORDER_HEIGHT):
                screen_text.append(" " * full_cols, style=border_cell_style)
                if i < BORDER_HEIGHT - 1:
                    screen_text.append("\n")
            return screen_text

    def render_text_screen(self, no_colors: bool = False) -> Union[str, Text]:
        """Render the current text screen.

        - If `no_colors` is True, returns plain text (for server/CLI).
        - Otherwise returns a Rich `Text` renderable with C64 BASIC/VIC colors.
        """
        if no_colors:
            with self.screen_lock:
                return "\n".join("".join(self.text_screen[row]) for row in range(25))
        return self._render_text_screen_rich()

    def get_cursor_position(self) -> Tuple[int, int, int]:
        """Return cursor row, column, and absolute address."""
        row = self.memory.read(CURSOR_ROW_ADDR)
        col = self.memory.read(CURSOR_COL_ADDR)
        row = max(0, min(row, 24))
        col = max(0, min(col, 39))
        cursor_addr = SCREEN_MEM + row * 40 + col
        return row, col, cursor_addr

    def read_screen_line_codes(self, row: int) -> List[int]:
        """Read raw screen codes for a given row."""
        row = max(0, min(row, 24))
        line_start = SCREEN_MEM + row * 40
        return [self.memory.read(line_start + col) for col in range(40)]

    def extract_line_codes(self, row: int) -> List[int]:
        """Extract a line with trailing spaces removed."""
        codes = self.read_screen_line_codes(row)
        last_non_space = -1
        for i in range(39, -1, -1):
            if codes[i] != 0x20:
                last_non_space = i
                break
        if last_non_space == -1:
            return []
        return codes[:last_non_space + 1]

    def get_current_line(self) -> Tuple[int, int, List[int]]:
        """Get cursor position and the current screen line codes."""
        row, col, _ = self.get_cursor_position()
        line_codes = self.extract_line_codes(row)
        return row, col, line_codes

    def _enqueue_keyboard_buffer(self, petscii_code: int) -> bool:
        """Enqueue a PETSCII code into the KERNAL keyboard buffer."""
        kb_buf_base = KEYBOARD_BUFFER_BASE
        kb_buf_len = self.memory.read(KEYBOARD_BUFFER_LEN_ADDR)
        if kb_buf_len >= 10:
            return False

        self.memory.write(kb_buf_base + kb_buf_len, petscii_code & 0xFF)
        kb_buf_len += 1
        self.memory.write(KEYBOARD_BUFFER_LEN_ADDR, kb_buf_len)
        return True

    def send_petscii(self, petscii_code: int) -> None:
        """Send a PETSCII key to the emulator input path."""
        if self.interface and hasattr(self.interface, "handle_petscii_input"):
            self.interface.handle_petscii_input(petscii_code & 0xFF)
            return
        self._enqueue_keyboard_buffer(petscii_code & 0xFF)

    def send_petscii_sequence(self, codes: List[int]) -> None:
        """Send multiple PETSCII codes to the emulator input path."""
        for code in codes:
            self.send_petscii(code)

    def _render_with_rich(self) -> str:
        """Render screen using Rich library for better formatting"""

        # Read C64 colors from memory
        background_color = self.memory.peek_vic(0x21) & 0x0F  # Background color
        border_color = self.memory.peek_vic(0x20) & 0x0F      # Border color

        # C64 color to ANSI 256 color mapping (better color approximation)
        c64_to_ansi256 = {
            0: 0,     # Black
            1: 15,    # White
            2: 196,   # Red
            3: 51,    # Cyan
            4: 129,   # Purple
            5: 46,    # Green
            6: 21,    # Blue
            7: 226,   # Yellow
            8: 208,   # Orange
            9: 94,    # Brown
            10: 201,  # Pink
            11: 240,  # Dark grey
            12: 250,  # Grey
            13: 118,  # Light green
            14: 39,   # Light blue
            15: 252   # Light grey
        }

        # Get ANSI color codes
        bg_ansi = c64_to_ansi256.get(background_color, 0)
        border_ansi = c64_to_ansi256.get(border_color, 15)

        # C64 color to Rich color mapping (fallback)
        c64_colors = {
            0: "black",      # Black
            1: "white",      # White
            2: "red",        # Red
            3: "cyan",       # Cyan
            4: "purple",     # Purple
            5: "green",      # Green
            6: "blue",       # Blue
            7: "yellow",     # Yellow
            8: "bright_red", # Orange
            9: "bright_magenta",  # Brown
            10: "bright_magenta", # Pink
            11: "bright_cyan",    # Dark gray
            12: "bright_white",   # Medium gray
            13: "bright_green",   # Light green
            14: "bright_blue",    # Light blue
            15: "bright_white"    # Light gray
        }

        console = Console(legacy_windows=False)
        with self.screen_lock:
            # Create a text object for the entire screen
            screen_text = Text()

            for row in range(25):
                for col in range(40):
                    char = self.text_screen[row][col]
                    color = self.text_colors[row][col]

                    # Get Rich color name
                    rich_color = c64_colors.get(color, "white")

                    # Add character with color
                    screen_text.append(char, style=f"bold {rich_color}")

                # Add newline at end of row
                if row < 24:  # Don't add newline after last row
                    screen_text.append("\n")

            # Render to string
            with console.capture() as capture:
                console.print(screen_text)
            return capture.get()

    def _render_with_ansi(self, no_colors: bool = False) -> str:
        """Render text screen with ANSI colors (fallback)"""

        # Read C64 colors from memory
        background_color = self.memory.peek_vic(0x21) & 0x0F  # Background color
        border_color = self.memory.peek_vic(0x20) & 0x0F      # Border color

        # C64 color to ANSI 256 color mapping
        c64_to_ansi256 = {
            0: 0,     # Black
            1: 15,    # White
            2: 196,   # Red
            3: 51,    # Cyan
            4: 129,   # Purple
            5: 46,    # Green
            6: 21,    # Blue
            7: 226,   # Yellow
            8: 208,   # Orange
            9: 94,    # Brown
            10: 201,  # Pink
            11: 240,  # Dark grey
            12: 250,  # Grey
            13: 118,  # Light green
            14: 39,   # Light blue
            15: 252   # Light grey
        }

        # Get ANSI 256 color codes
        bg_ansi = c64_to_ansi256.get(background_color, 0)
        border_ansi = c64_to_ansi256.get(border_color, 15)

        # Fallback ANSI color mapping for foreground
        c64_colors = {
            0: 30,   # Black
            1: 37,   # White
            2: 31,   # Red
            3: 36,   # Cyan
            4: 35,   # Purple (magenta)
            5: 32,   # Green
            6: 34,   # Blue
            7: 33,   # Yellow
            8: 31,   # Orange (red)
            9: 35,   # Brown (magenta)
            10: 35,  # Pink (magenta)
            11: 90,  # Dark gray
            12: 37,  # Medium gray (white)
            13: 92,  # Light green
            14: 94,  # Light blue
            15: 97   # Light gray
        }

        with self.screen_lock:
            lines = []
            # Add border/background color to entire screen
            bg_escape = f'\033[48;5;{bg_ansi}m' if not no_colors else ''
            reset = '\033[0m' if not no_colors else ''

            for row in range(25):
                line = []
                if not no_colors:
                    line.append(bg_escape)  # Background color for entire line

                for col in range(40):
                    char = self.text_screen[row][col]

                    if no_colors:
                        line.append(char)
                    else:
                        color = self.text_colors[row][col]
                        # Apply ANSI 256 foreground color
                        fg_ansi = c64_to_ansi256.get(color, 15)
                        colored_char = f'\033[38;5;{fg_ansi}m{char}'
                        line.append(colored_char)

                if not no_colors:
                    line.append(reset)  # Reset colors at end of line

                lines.append(''.join(line))
            return '\n'.join(lines)

    def dump_memory(self, start: int = 0x0000, end: int = 0x10000) -> bytes:
        """Dump memory range as bytes"""
        return bytes(self.memory.ram[start:end])

    def get_cpu_state(self) -> Dict:
        """Get current CPU state"""
        return {
            'pc': self.cpu.state.pc,
            'a': self.cpu.state.a,
            'x': self.cpu.state.x,
            'y': self.cpu.state.y,
            'sp': self.cpu.state.sp,
            'p': self.cpu.state.p,
            'cycles': self.cpu.state.cycles
        }

    def set_cpu_state(self, state: Dict) -> None:
        """Set CPU state"""
        if 'pc' in state:
            self.cpu.state.pc = state['pc'] & 0xFFFF
        if 'a' in state:
            self.cpu.state.a = state['a'] & 0xFF
        if 'x' in state:
            self.cpu.state.x = state['x'] & 0xFF
        if 'y' in state:
            self.cpu.state.y = state['y'] & 0xFF
        if 'sp' in state:
            self.cpu.state.sp = state['sp'] & 0xFF
        if 'p' in state:
            self.cpu.state.p = state['p'] & 0xFF


