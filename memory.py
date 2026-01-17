"""
C64 Memory Map
"""

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from .constants import (
    ROM_BASIC_START, ROM_BASIC_END,
    ROM_KERNAL_START, ROM_KERNAL_END,
    ROM_CHAR_START, ROM_CHAR_END,
    VIC_BASE, SID_BASE, CIA1_BASE, CIA2_BASE,
    SCREEN_MEM, COLOR_MEM
)
from .cpu_state import CIATimer

if TYPE_CHECKING:
    from .debug import UdpDebugLogger


@dataclass
class MemoryMap:
    """C64 memory map"""
    ram: bytearray = field(default_factory=lambda: bytearray(0x10000))
    basic_rom: Optional[bytes] = None
    kernal_rom: Optional[bytes] = None
    char_rom: Optional[bytes] = None
    udp_debug: Optional['UdpDebugLogger'] = None
    cia1_timer_a: CIATimer = field(default_factory=CIATimer)
    cia1_timer_b: CIATimer = field(default_factory=CIATimer)
    cia1_icr: int = 0  # Interrupt Control Register
    pending_irq: bool = False  # Pending IRQ flag
    video_standard: str = "pal"  # "pal" or "ntsc"
    raster_line: int = 300  # Current raster line (start high so it wraps to 0)
    raster_cycles: int = 0  # Cycle counter for raster timing
    vic_interrupt_state: int = 0  # VIC interrupt state for D019
    jiffy_cycles: int = 0  # Cycle counter for jiffy clock
    _vic_regs: bytearray = field(default_factory=lambda: bytearray(0x40))

    def peek_vic(self, reg: int) -> int:
        """Return VIC-II register state, bypassing 6510 banking.

        This reads from the internal VIC-II register array directly and ignores
        the current CPU memory configuration (e.g. CHAREN / I/O mapping). It is
        intended for components such as the video renderer or initialization
        logic that need stable access to VIC state regardless of how memory is
        currently banked from the CPU's point of view.
        """
        return self._read_vic(reg & 0x3F)

    def poke_vic(self, reg: int, value: int) -> None:
        """Update VIC-II register state, bypassing 6510 banking.

        This writes to the internal VIC-II register array directly and ignores
        the current CPU memory configuration. Only the low 6 bits of *reg* are
        used, matching the VIC-II's 64-register mirroring. This helper is
        intended for rendering and initialization code that must modify VIC
        state even when the I/O area is not visible to normal CPU writes.
        """
        self._write_vic(reg & 0x3F, value & 0xFF)

    def read(self, addr: int) -> int:
        """Read from memory, handling ROM/RAM mapping"""
        addr &= 0xFFFF

        # Color RAM ($D800-$DBE7) is a dedicated 4-bit RAM region.
        # In practice it should be readable/writable regardless of ROM banking.
        if COLOR_MEM <= addr < (COLOR_MEM + 1000):
            return self.ram[addr] & 0x0F

        # 6510 processor port ($0001) controls banking.
        # Bits (common simplified model):
        # - bit 0: LORAM
        # - bit 1: HIRAM
        # - bit 2: CHAREN (1 = I/O visible at $D000-$DFFF, 0 = CHAR ROM / RAM)
        port_01 = self.ram[0x01]
        loram = (port_01 & 0x01) != 0
        hiram = (port_01 & 0x02) != 0
        charen = (port_01 & 0x04) != 0

        # I/O area (can be ROM or RAM depending on memory config)
        if ROM_CHAR_START <= addr < ROM_CHAR_END:
            if charen:
                # I/O registers (VIC, SID, CIA, etc.)
                return self._read_io(addr)
            # CHAR ROM is visible when I/O is banked out and HIRAM is set.
            if self.char_rom and hiram:
                return self.char_rom[addr - ROM_CHAR_START]
            return self.ram[addr]

        # BASIC ROM
        if ROM_BASIC_START <= addr < ROM_BASIC_END:
            if loram and hiram:  # BASIC ROM enabled
                if self.basic_rom:
                    return self.basic_rom[addr - ROM_BASIC_START]
            return self.ram[addr]

        # KERNAL ROM
        if ROM_KERNAL_START <= addr < ROM_KERNAL_END:
            if hiram:  # KERNAL ROM enabled
                if self.kernal_rom:
                    return self.kernal_rom[addr - ROM_KERNAL_START]
            return self.ram[addr]

        # RAM
        return self.ram[addr]

    def write(self, addr: int, value: int) -> None:
        """Write to memory (only RAM, ROM writes are ignored)"""
        addr &= 0xFFFF
        value &= 0xFF

        # Color RAM ($D800-$DBE7): dedicated 4-bit writable RAM.
        if COLOR_MEM <= addr < (COLOR_MEM + 1000):
            self.ram[addr] = value & 0x0F
            return

        port_01 = self.ram[0x01]
        charen = (port_01 & 0x04) != 0

        # Log memory writes if UDP debug is enabled (only screen writes to reduce overhead)
        if self.udp_debug and self.udp_debug.enabled:
            # Only log screen writes (most important for seeing output)
            if 0x0400 <= addr < 0x07E8:
                self.udp_debug.send('memory_write', {
                    'addr': addr,
                    'value': value
                })

        # Trigger screen update when screen or color memory changes
        # Note: Screen updates are handled by the emulator's update thread
        # This is just a placeholder for potential future immediate updates

        # ROM areas - writes go to RAM underneath
        if ROM_BASIC_START <= addr < ROM_BASIC_END:
            self.ram[addr] = value
        elif ROM_KERNAL_START <= addr < ROM_KERNAL_END:
            self.ram[addr] = value
        elif ROM_CHAR_START <= addr < ROM_CHAR_END:
            # I/O area
            if charen:  # I/O enabled
                self._write_io(addr, value)
            else:
                self.ram[addr] = value
        else:
            self.ram[addr] = value

    def _read_io(self, addr: int) -> int:
        """Read from I/O registers"""
        # Color RAM is handled in read(); keep this for safety if called directly.
        if COLOR_MEM <= addr < (COLOR_MEM + 1000):
            return self.ram[addr] & 0x0F

        # VIC registers
        if VIC_BASE <= addr < VIC_BASE + 0x40:
            return self._read_vic(addr - VIC_BASE)

        # SID registers
        if SID_BASE <= addr < SID_BASE + 0x20:
            return 0  # SID not implemented yet

        # CIA1
        if CIA1_BASE <= addr < CIA1_BASE + 0x10:
            return self._read_cia1(addr - CIA1_BASE)

        # CIA2
        if CIA2_BASE <= addr < CIA2_BASE + 0x10:
            return self._read_cia2(addr - CIA2_BASE)

        return 0

    def _write_io(self, addr: int, value: int) -> None:
        """Write to I/O registers"""
        # Color RAM is handled in write(); keep this for safety if called directly.
        if COLOR_MEM <= addr < (COLOR_MEM + 1000):
            self.ram[addr] = value & 0x0F
            return

        # VIC registers
        if VIC_BASE <= addr < VIC_BASE + 0x40:
            self._write_vic(addr - VIC_BASE, value)
            return

        # SID registers
        if SID_BASE <= addr < SID_BASE + 0x20:
            return  # SID not implemented yet

        # CIA1
        if CIA1_BASE <= addr < CIA1_BASE + 0x10:
            self._write_cia1(addr - CIA1_BASE, value)
            return

        # CIA2
        if CIA2_BASE <= addr < CIA2_BASE + 0x10:
            self._write_cia2(addr - CIA2_BASE, value)
            return

    def _read_vic(self, reg: int) -> int:
        """Read VIC-II register"""
        if reg == 0x11:  # VIC control register 1
            # Bit 7: Raster MSB
            # Bit 3: 25/24 row mode (1 for 25 rows)
            raster_msb = (self.raster_line >> 8) & 0x01
            return (raster_msb << 7) | (1 << 3)  # 25 rows, raster MSB
        elif reg == 0x12:  # Raster line register
            return self.raster_line & 0xFF
        elif reg == 0x19:  # VIC interrupt register
            # Disable VIC interrupts completely
            return 0x00
        elif reg == 0x20:  # Border color ($D020)
            return (self._vic_regs[0x20] if 0x20 < len(self._vic_regs) else 0x0E) & 0x0F  # Default light blue
        elif reg == 0x21:  # Background color 0 ($D021)
            return (self._vic_regs[0x21] if 0x21 < len(self._vic_regs) else 0x06) & 0x0F  # Default blue
        # Other registers return stored values or 0
        return self._vic_regs[reg] if reg < len(self._vic_regs) else 0

    def _write_vic(self, reg: int, value: int) -> None:
        """Write VIC-II register"""
        # Store VIC register state
        self._vic_regs[reg] = value

        # Handle special register writes
        if reg == 0x19:  # VIC interrupt register
            # Writing to D019 acknowledges interrupts
            # For simulation, reset interrupt state
            self.vic_interrupt_state = 0

    def _read_cia1(self, reg: int) -> int:
        """Read CIA1 register"""
        # Timer A low byte
        if reg == 0x04:
            return self.cia1_timer_a.counter & 0xFF
        # Timer A high byte
        elif reg == 0x05:
            return (self.cia1_timer_a.counter >> 8) & 0xFF
        # Timer B low byte
        elif reg == 0x06:
            return self.cia1_timer_b.counter & 0xFF
        # Timer B high byte
        elif reg == 0x07:
            return (self.cia1_timer_b.counter >> 8) & 0xFF
        # Interrupt Control Register (ICR)
        elif reg == 0x0D:
            # Reading ICR acknowledges interrupts
            result = self.cia1_icr
            self.cia1_icr = 0
            self.pending_irq = False
            return result
        # Control Register A
        elif reg == 0x0E:
            result = 0
            if self.cia1_timer_a.running:
                result |= 0x01
            if self.cia1_timer_a.one_shot:
                result |= 0x08
            if self.cia1_timer_a.input_mode != 0:
                result |= (self.cia1_timer_a.input_mode << 5)
            return result
        # Control Register B
        elif reg == 0x0F:
            result = 0
            if self.cia1_timer_b.running:
                result |= 0x01
            if self.cia1_timer_b.one_shot:
                result |= 0x08
            if self.cia1_timer_b.input_mode != 0:
                result |= (self.cia1_timer_b.input_mode << 5)
            return result
        # Other registers (keyboard, joystick, etc.) - return 0 for now
        return 0

    def _write_cia1(self, reg: int, value: int) -> None:
        """Write CIA1 register"""
        # Timer A latch low byte
        if reg == 0x04:
            self.cia1_timer_a.latch = (self.cia1_timer_a.latch & 0xFF00) | value
            if not self.cia1_timer_a.running:
                self.cia1_timer_a.counter = (self.cia1_timer_a.counter & 0xFF00) | value
        # Timer A latch high byte
        elif reg == 0x05:
            self.cia1_timer_a.latch = (self.cia1_timer_a.latch & 0x00FF) | (value << 8)
            if not self.cia1_timer_a.running:
                self.cia1_timer_a.counter = (self.cia1_timer_a.counter & 0x00FF) | (value << 8)
        # Timer B latch low byte
        elif reg == 0x06:
            self.cia1_timer_b.latch = (self.cia1_timer_b.latch & 0xFF00) | value
            if not self.cia1_timer_b.running:
                self.cia1_timer_b.counter = (self.cia1_timer_b.counter & 0xFF00) | value
        # Timer B latch high byte
        elif reg == 0x07:
            self.cia1_timer_b.latch = (self.cia1_timer_b.latch & 0x00FF) | (value << 8)
            if not self.cia1_timer_b.running:
                self.cia1_timer_b.counter = (self.cia1_timer_b.counter & 0x00FF) | (value << 8)
        # Interrupt Control Register (ICR)
        elif reg == 0x0D:
            if value & 0x80:  # Set bits
                # Enable interrupts for bits set in lower 7 bits
                if value & 0x01:  # Timer A IRQ
                    self.cia1_timer_a.irq_enabled = True
                if value & 0x02:  # Timer B IRQ
                    self.cia1_timer_b.irq_enabled = True
            else:  # Clear bits
                if value & 0x01:  # Timer A IRQ
                    self.cia1_timer_a.irq_enabled = False
                if value & 0x02:  # Timer B IRQ
                    self.cia1_timer_b.irq_enabled = False
        # Control Register A
        elif reg == 0x0E:
            # Bit 0: Start/stop timer
            if value & 0x01:
                if not self.cia1_timer_a.running:
                    self.cia1_timer_a.counter = self.cia1_timer_a.latch
                self.cia1_timer_a.running = True
            else:
                self.cia1_timer_a.running = False
            # Bit 3: One-shot mode
            self.cia1_timer_a.one_shot = (value & 0x08) != 0
            # Bits 5-6: Input mode
            self.cia1_timer_a.input_mode = (value >> 5) & 0x03
        # Control Register B
        elif reg == 0x0F:
            # Bit 0: Start/stop timer
            if value & 0x01:
                if not self.cia1_timer_b.running:
                    self.cia1_timer_b.counter = self.cia1_timer_b.latch
                self.cia1_timer_b.running = True
            else:
                self.cia1_timer_b.running = False
            # Bit 3: One-shot mode
            self.cia1_timer_b.one_shot = (value & 0x08) != 0
            # Bits 5-6: Input mode
            self.cia1_timer_b.input_mode = (value >> 5) & 0x03

    def _read_cia2(self, reg: int) -> int:
        """Read CIA2 register"""
        # Serial bus, etc.
        return 0

    def _write_cia2(self, reg: int, value: int) -> None:
        """Write CIA2 register"""
        pass

    def _scroll_screen_up(self) -> None:
        """Scroll the screen up by one line (optimized)"""
        # Use block copy for speed - move 960 bytes up by 40 bytes
        # Source: SCREEN_MEM + 40 (row 1 start)
        # Dest: SCREEN_MEM (row 0 start)
        # Length: 960 bytes (24 rows * 40 cols)
        src_start = SCREEN_MEM + 40
        dst_start = SCREEN_MEM
        length = 960

        # Block copy
        for i in range(length):
            self.ram[dst_start + i] = self.ram[src_start + i]

        # Clear the bottom line (row 24)
        for col in range(40):
            self.ram[SCREEN_MEM + 24 * 40 + col] = 0x20  # Space

        # Also scroll color RAM alongside screen RAM (same geometry).
        color_src_start = COLOR_MEM + 40
        color_dst_start = COLOR_MEM
        for i in range(length):
            self.ram[color_dst_start + i] = self.ram[color_src_start + i] & 0x0F

        # Clear bottom line colors to current text color (fallback: light blue).
        current_color = self.ram[0x0286] & 0x0F
        for col in range(40):
            self.ram[COLOR_MEM + 24 * 40 + col] = current_color
