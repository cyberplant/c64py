"""
C64 Memory Map
"""

from dataclasses import dataclass, field
from typing import List, Optional, TYPE_CHECKING, Union

from .constants import (
    ROM_BASIC_START, ROM_BASIC_END,
    ROM_KERNAL_START, ROM_KERNAL_END,
    ROM_CHAR_START, ROM_CHAR_END,
    VIC_BASE, SID_BASE, CIA1_BASE, CIA2_BASE,
    SCREEN_MEM, COLOR_MEM,
    VIC_CONTROL_REG_1, VIC_CONTROL_REG_2,
    VIC_D011_BMM, VIC_D011_ECM, VIC_D016_MCM
)
from .cpu_state import CIATimer

if TYPE_CHECKING:
    from .debug import UdpDebugLogger
    from .iec_bus import IECBus
    from .resid import ReSIDEmulator
    from .sid import SidEmulator


@dataclass
class MemoryMap:
    """C64 memory map"""
    ram: bytearray = field(default_factory=lambda: bytearray(0x10000))
    basic_rom: Optional[bytes] = None
    kernal_rom: Optional[bytes] = None
    char_rom: Optional[bytes] = None
    udp_debug: Optional['UdpDebugLogger'] = None
    sid: Optional[Union['SidEmulator', 'ReSIDEmulator']] = None
    cia1_timer_a: CIATimer = field(default_factory=CIATimer)
    cia1_timer_b: CIATimer = field(default_factory=CIATimer)
    cia1_icr: int = 0  # Interrupt Control Register
    pending_irq: bool = False  # Pending IRQ flag
    video_standard: str = "pal"  # "pal" or "ntsc"
    raster_line: int = 0  # Current raster line
    raster_cycles: int = 0  # Cycle counter for raster timing
    badline_cycles: int = 0  # Extra cycles stolen by VIC on badlines
    vic_badline_triggered_line: int = -1  # Last raster line where badline DMA was triggered
    vic_interrupt_state: int = 0  # VIC interrupt state for D019
    vic_den_latched: bool = False  # DEN latched at start of frame (raster line 0)
    vic_yscroll_latched: int = 0  # YSCROLL latched at start of current raster line
    jiffy_cycles: int = 0  # Cycle counter for jiffy clock
    _vic_regs: bytearray = field(default_factory=lambda: bytearray(0x40))
    # IEC serial bus (optional, for 1541 drive emulation)
    iec_bus: Optional['IECBus'] = None
    # CIA2 Port A state (for IEC bus control)
    cia2_pra: int = 0xFF  # Port A data register
    cia2_ddra: int = 0xFF  # Port A data direction (0=input, 1=output)
    # Scheduled inject-keys: joystick bits to hold low on CIA1 reads (active low, OR mask).
    joy_inject1_clear: int = 0
    joy_inject2_clear: int = 0
    joy_inject1_until: Optional[int] = None
    joy_inject2_until: Optional[int] = None
    # Instruction context for optional debug hooks (UDP, accurate-VIC bus phases).
    debug_last_pc: int = 0
    debug_last_cycles: int = 0
    debug_last_opcode: int = 0
    debug_last_op1: int = 0
    debug_last_op2: int = 0
    # When False, skip VIC render snapshots at frame boundary (headless throughput).
    vic_render_snapshots: bool = True
    # When True, CPU thread calls snapshot_vic_render_state at each emulated frame (raster 0).
    # When False (fast VIC + --graphics), pygame latches once per host present instead — matches
    # pre-snapshot throughput while keeping stable regs for the drawn frame.
    vic_snapshot_each_emulated_frame: bool = True
    # Per-raster-line VIC snapshots for beam-accurate pygame (see docs/DEBUGGING.md).
    beam_render_enabled: bool = False
    beam_vic_lines: Optional[List[bytes]] = None
    beam_cia2_lines: Optional[List[int]] = None
    # Cached 6510 $01 effective value for MemoryMap.read() hot path (invalidated on $00/$01 writes).
    _port01_read_cache_valid: bool = field(default=False, init=False, repr=False)
    _port01_read_cache_value: int = field(default=0, init=False, repr=False)

    def _vic_irq_enabled_pending(self) -> bool:
        """Return True when a VIC IRQ source is both pending and enabled."""
        irq_mask = self._vic_regs[0x1A] & 0x0F
        return (self.vic_interrupt_state & irq_mask) != 0

    def _cpu_port01_effective(self) -> int:
        """Return effective 6510 port value seen at $0001.

        $0000 is DDR, $0001 is output latch. Input bits are pulled high on C64.
        """
        ddr = self.ram[0x0000] & 0xFF
        latch = self.ram[0x0001] & 0xFF
        pullups = 0x17
        return (latch & ddr) | (pullups & (~ddr & 0xFF))

    def invalidate_6510_port_read_cache(self) -> None:
        """Clear cached effective $01 for :meth:`read` (call after direct ``ram[0/1]`` writes)."""
        self._port01_read_cache_valid = False

    def _cpu_port01_effective_for_read(self) -> int:
        if not self._port01_read_cache_valid:
            self._port01_read_cache_value = self._cpu_port01_effective()
            self._port01_read_cache_valid = True
        return self._port01_read_cache_value

    def recompute_pending_irq(self) -> None:
        """Recompute CPU IRQ line from all currently modeled IRQ sources."""
        if self.vic_interrupt_state == 0:
            self.pending_irq = bool(self.cia1_icr & 0x80)
        else:
            self.pending_irq = bool((self.cia1_icr & 0x80) or self._vic_irq_enabled_pending())

    def trigger_vic_irq(self, source_mask: int) -> None:
        """Set VIC interrupt source bits and update IRQ line."""
        self.vic_interrupt_state |= (source_mask & 0x0F)
        self.recompute_pending_irq()

    def peek_vic(self, reg: int) -> int:
        """Return VIC-II register state, bypassing 6510 banking.

        This reads from the internal VIC-II register array directly and ignores
        the current CPU memory configuration (e.g. CHAREN / I/O mapping). It is
        intended for components such as the video renderer or initialization
        logic that need stable access to VIC state regardless of how memory is
        currently banked from the CPU's point of view.
        """
        return self._read_vic(reg & 0x3F)

    def vic_stored_reg(self, reg: int) -> int:
        """CPU-written VIC register value (no raster/composite read side effects).

        Used by the cycle engine to avoid peek_vic per tick for $D011/$D012/$D015 etc.
        """
        r = reg & 0x3F
        return self._vic_regs[r] if r < len(self._vic_regs) else 0

    def vic_stored_regs_d011_d012_d015(self) -> tuple[int, int, int]:
        """Single read of shadow bytes for ViciiCycleEngine (hot path; no composite bits)."""
        m = self._vic_regs
        return (m[0x11], m[0x12], m[0x15] & 0xFF)

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
        if addr == 0x0000:
            return self.ram[0x0000]
        if addr == 0x0001:
            return self._cpu_port01_effective_for_read()

        # Color RAM ($D800-$DBE7): hardware is 4-bit; CPU still uses an 8-bit data path.
        # Store/read full bytes in backing RAM (matches VICE for loaders using $D8xx as linear RAM).
        # Video code masks to 0x0F when sampling color nybbles (see graphics.py).
        if COLOR_MEM <= addr < (COLOR_MEM + 1000):
            return self.ram[addr] & 0xFF

        # Fast path: regions that never depend on 6510 banking (read always from RAM backing).
        if 2 <= addr < ROM_BASIC_START:
            return self.ram[addr]
        if ROM_BASIC_END <= addr < ROM_CHAR_START:
            return self.ram[addr]

        # 6510 processor port ($0001) controls banking.
        # Bits (common simplified model):
        # - bit 0: LORAM
        # - bit 1: HIRAM
        # - bit 2: CHAREN (1 = I/O visible at $D000-$DFFF, 0 = CHAR ROM / RAM)
        port_01 = self._cpu_port01_effective_for_read()
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

        # Color RAM: see read() — keep full byte for CPU; VIC uses low nibble when rendering.
        if COLOR_MEM <= addr < (COLOR_MEM + 1000):
            self.ram[addr] = value & 0xFF
            return

        if addr in (0x0000, 0x0001):
            self.ram[addr] = value
            self.invalidate_6510_port_read_cache()
            return

        port_01 = self._cpu_port01_effective_for_read()
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

    def sid_tick_cpu_cycles(self, n: int) -> None:
        """Advance reSID by *n* C64 clocks when using :class:`resid.ReSIDEmulator`.

        Invoked from the CPU so SID phase matches bus reads (e.g. ``$D41B``).  No-op for
        the simple :class:`sid.SidEmulator` or when SID is disabled.

        When ReSID is in decoupled mode (``_cpu_lockstep`` false / fast VIC), the audio
        thread clocks the chip; skip the per-instruction call entirely to avoid Python
        overhead on every opcode.
        """
        if n <= 0 or not self.sid:
            return
        sid = self.sid
        if getattr(sid, "_cpu_lockstep", True) is False:
            return
        tick = getattr(sid, "tick_cpu_cycles", None)
        if tick is not None:
            tick(n)

    def _read_io(self, addr: int) -> int:
        """Read from I/O registers"""
        # Color RAM is handled in read(); keep this for safety if called directly.
        if COLOR_MEM <= addr < (COLOR_MEM + 1000):
            return self.ram[addr] & 0xFF

        # VIC registers
        if VIC_BASE <= addr < VIC_BASE + 0x40:
            return self._read_vic(addr - VIC_BASE)

        # SID registers
        if SID_BASE <= addr < SID_BASE + 0x20:
            if self.sid:
                return self.sid.read_register(addr - SID_BASE)
            return 0

        # CIA1
        if CIA1_BASE <= addr < CIA1_BASE + 0x10:
            return self._read_cia1(addr - CIA1_BASE)

        # CIA2
        if CIA2_BASE <= addr < CIA2_BASE + 0x10:
            return self._read_cia2(addr - CIA2_BASE)

        # Unmapped I/O window: read from RAM (e.g. loader data at $DAxx with CHAREN=1).
        return self.ram[addr] & 0xFF

    def _write_io(self, addr: int, value: int) -> None:
        """Write to I/O registers"""
        # Color RAM is handled in write(); keep this for safety if called directly.
        if COLOR_MEM <= addr < (COLOR_MEM + 1000):
            self.ram[addr] = value & 0xFF
            return

        # VIC registers
        if VIC_BASE <= addr < VIC_BASE + 0x40:
            self._write_vic(addr - VIC_BASE, value)
            return

        # SID registers
        if SID_BASE <= addr < SID_BASE + 0x20:
            if self.sid:
                self.sid.write_register(addr - SID_BASE, value)
            return

        # CIA1
        if CIA1_BASE <= addr < CIA1_BASE + 0x10:
            self._write_cia1(addr - CIA1_BASE, value)
            return

        # CIA2
        if CIA2_BASE <= addr < CIA2_BASE + 0x10:
            self._write_cia2(addr - CIA2_BASE, value)
            return

        # No chip decodes this $D000-$DFFF address: write lands in RAM (same as VICE/hardware).
        self.ram[addr] = value

    def _read_vic(self, reg: int) -> int:
        """Read VIC-II register"""
        if reg == 0x11:  # VIC control register 1
            # Bit 7 is the current raster MSB on reads.
            # Bits 0-6 reflect the stored register value (DEN/YSCROLL/etc.).
            raster_msb = (self.raster_line >> 8) & 0x01
            return (self._vic_regs[0x11] & 0x7F) | (raster_msb << 7)
        elif reg == 0x12:  # Raster line register
            return self.raster_line & 0xFF
        elif reg == 0x19:  # VIC interrupt register
            value = self.vic_interrupt_state & 0x0F
            if self._vic_irq_enabled_pending():
                value |= 0x80
            return value
        elif reg == 0x1A:  # VIC interrupt enable register
            return self._vic_regs[0x1A] & 0x0F
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
            # Writing 1 clears the corresponding pending source bits.
            self.vic_interrupt_state &= ~(value & 0x0F)
        elif reg == 0x1A:  # VIC interrupt enable register
            # Only lower 4 bits are valid enables.
            self._vic_regs[0x1A] = value & 0x0F
        elif reg == 0x12:  # Raster compare low byte
            self._vic_regs[0x12] = value & 0xFF

        self.recompute_pending_irq()

    def sync_joystick_inject(self, cpu_cycles: int) -> None:
        """Clear expired joystick inject bits (``--inject-keys``)."""
        if self.joy_inject1_until is not None and cpu_cycles >= self.joy_inject1_until:
            self.joy_inject1_clear = 0
            self.joy_inject1_until = None
        if self.joy_inject2_until is not None and cpu_cycles >= self.joy_inject2_until:
            self.joy_inject2_clear = 0
            self.joy_inject2_until = None

    def arm_joystick_inject(self, port: int, clear_mask: int, until_cycle: int) -> None:
        """OR combined directions; extend hold until max(until_cycle)."""
        clear_mask &= 0x1F
        if port == 1:
            self.joy_inject1_clear |= clear_mask
            prev = self.joy_inject1_until
            self.joy_inject1_until = (
                until_cycle if prev is None else max(prev, until_cycle)
            )
        elif port == 2:
            self.joy_inject2_clear |= clear_mask
            prev = self.joy_inject2_until
            self.joy_inject2_until = (
                until_cycle if prev is None else max(prev, until_cycle)
            )

    def _read_cia1(self, reg: int) -> int:
        """Read CIA1 register"""
        # Port A (directly connected to keyboard columns and joystick 2)
        if reg == 0x00:
            return 0xFF & ~self.joy_inject2_clear
        # Port B (keyboard rows and joystick 1)
        elif reg == 0x01:
            return 0xFF & ~self.joy_inject1_clear
        # Timer A low byte
        elif reg == 0x04:
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
            self.recompute_pending_irq()
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

    def apply_cia2_port_a_to_iec_bus(self) -> None:
        """Apply current ``cia2_pra`` to the IEC bus (same as a write to CIA2 port A)."""
        if self.iec_bus is None:
            return
        v = self.cia2_pra & 0xFF
        atn_state = (v & 0x08) != 0
        self.iec_bus.set_atn(atn_state)
        self.iec_bus.set_clk("c64", (v & 0x10) != 0)
        self.iec_bus.set_data("c64", (v & 0x20) != 0)

    def ensure_beam_buffers(self) -> None:
        """Allocate per-line VIC/CIA2 snapshot arrays for the current video standard."""
        n = 312 if self.video_standard == "pal" else 263
        if self.beam_vic_lines is None or len(self.beam_vic_lines) != n:
            self.beam_vic_lines = [bytes(64) for _ in range(n)]
            self.beam_cia2_lines = [0] * n

    def beam_capture_raster_line(self, line: int) -> None:
        """Record VIC + CIA2 PA for *line* (used by beam-accurate rendering)."""
        if not self.beam_render_enabled:
            return
        self.ensure_beam_buffers()
        assert self.beam_vic_lines is not None and self.beam_cia2_lines is not None
        n = len(self.beam_vic_lines)
        line %= n
        self.beam_vic_lines[line] = bytes(self._vic_regs[:0x40])
        self.beam_cia2_lines[line] = self.cia2_pra & 0xFF

    def _read_cia2(self, reg: int) -> int:
        """Read CIA2 register.
        
        CIA2 Port A controls the IEC serial bus:
        - Bit 3: ATN OUT
        - Bit 4: CLK OUT
        - Bit 5: DATA OUT
        - Bit 6: CLK IN
        - Bit 7: DATA IN
        """
        if reg == 0x00:  # Port A (IEC bus control)
            result = self.cia2_pra
            # If IEC bus is attached, read actual bus state
            if self.iec_bus is not None:
                # Bits 6-7 are inputs (CLK IN, DATA IN)
                # Clear input bits
                result &= 0x3F
                # Set based on actual bus state
                if self.iec_bus.clk:  # CLK released (high)
                    result |= 0x40
                if self.iec_bus.data:  # DATA released (high)
                    result |= 0x80
            return result
        elif reg == 0x02:  # Data direction register A
            return self.cia2_ddra
        # Other registers return 0
        return 0

    def _write_cia2(self, reg: int, value: int) -> None:
        """Write CIA2 register.
        
        CIA2 Port A controls the IEC serial bus:
        - Bit 3: ATN OUT
        - Bit 4: CLK OUT
        - Bit 5: DATA OUT
        """
        if reg == 0x00:  # Port A (IEC bus control)
            self.cia2_pra = value
            if self.iec_bus is not None:
                self.apply_cia2_port_a_to_iec_bus()
        elif reg == 0x02:  # Data direction register A
            self.cia2_ddra = value

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

    def _display_mode_from_vic_bytes(self, regb: bytes) -> dict:
        """Build display mode dict from a 64-byte VIC register snapshot (indices 0..0x3F)."""

        def rb(idx: int) -> int:
            return regb[idx] if idx < len(regb) else 0

        d011 = rb(VIC_CONTROL_REG_1)
        d016 = rb(VIC_CONTROL_REG_2)
        d018 = rb(0x18)

        bitmap_mode = (d011 & VIC_D011_BMM) != 0
        extended_color = (d011 & VIC_D011_ECM) != 0
        multicolor = (d016 & VIC_D016_MCM) != 0

        vm = (d018 >> 4) & 0x0F
        cb = (d018 >> 1) & 0x07
        screen_base = vm * 0x0400

        if bitmap_mode:
            bitmap_base = 0x2000 if (d018 & 0x08) else 0x0000
            char_base = 0
        else:
            char_base = cb * 0x0800
            bitmap_base = 0

        if bitmap_mode:
            mode = 'multicolor_bitmap' if multicolor else 'bitmap'
        elif extended_color:
            mode = 'extended_color'
        elif multicolor:
            mode = 'multicolor_text'
        else:
            mode = 'text'

        return {
            'mode': mode,
            'bitmap_mode': bitmap_mode,
            'multicolor': multicolor,
            'extended_color': extended_color,
            'screen_base': screen_base,
            'bitmap_base': bitmap_base,
            'char_base': char_base,
        }

    def get_display_mode(self) -> dict:
        """Current VIC-II mode from live registers (CPU-visible)."""
        return self._display_mode_from_vic_bytes(bytes(self._vic_regs[:0x40]))

    def snapshot_vic_render_state(self) -> None:
        """Latch VIC + CIA2 PA at raster line 0 for stable full-frame graphics.

        Raster IRQ code often toggles $D011/$D016 during the frame. The pygame thread
        samples registers without locking, so the renderer was catching different
        phases and flickering. One snapshot per emulated video frame fixes that.

        Split-screen (different modes on different raster bands) is not modeled;
        one latched mode is still used for the whole bitmap in that frame.
        """
        if not self.vic_render_snapshots:
            return
        self._vic_render_snapshot = (bytes(self._vic_regs[:0x40]), int(self.cia2_pra) & 0xFF)

    def get_render_display_mode(self) -> dict:
        """Display mode from the last render snapshot (falls back to live)."""
        snap = getattr(self, "_vic_render_snapshot", None)
        if snap is None:
            return self.get_display_mode()
        regb, _pra = snap
        return self._display_mode_from_vic_bytes(regb)

    def get_render_vic_bank_base(self) -> int:
        """VIC bank from render snapshot (falls back to live CIA2)."""
        snap = getattr(self, "_vic_render_snapshot", None)
        pra = (snap[1] & 0x03) if snap is not None else (self.cia2_pra & 0x03)
        return (3 - pra) * 0x4000

    def get_vic_bank_base(self) -> int:
        """Physical base of the 16 KiB window the VIC-II addresses.

        CIA-2 Port A bits 0-1 (as stored in ``cia2_pra``) select the bank:
        ``%11`` → ``$0000``, ``%10`` → ``$4000``, ``%01`` → ``$8000``,
        ``%00`` → ``$C000``. Screen matrix, bitmap, and charset pointers
        from ``$D018`` are offsets within this window.
        """
        sel = self.cia2_pra & 0x03
        return (3 - sel) * 0x4000

    def is_sprite_enabled(self, sprite_num: int) -> bool:
        """Check if a sprite is enabled.
        
        Args:
            sprite_num: Sprite number (0-7)
            
        Returns:
            True if the sprite is enabled, False otherwise
        """
        if not 0 <= sprite_num <= 7:
            return False
        sprite_enable_reg = self._vic_regs[0x15] if 0x15 < len(self._vic_regs) else 0
        return (sprite_enable_reg & (1 << sprite_num)) != 0
    
    def _get_sprite_data_from_regs(
        self, sprite_num: int, regb: bytes, vic_bank: int, screen_base: int
    ) -> dict:
        if not 0 <= sprite_num <= 7:
            return {'enabled': False}

        def rb(idx: int) -> int:
            return regb[idx] if idx < len(regb) else 0

        sprite_enable_reg = rb(0x15)
        enabled = (sprite_enable_reg & (1 << sprite_num)) != 0
        if not enabled:
            return {'enabled': False}

        x_low = rb(sprite_num * 2)
        y = rb(sprite_num * 2 + 1)
        x_msb_reg = rb(0x10)
        x_msb = (x_msb_reg & (1 << sprite_num)) != 0
        x = x_low + (256 if x_msb else 0)

        color_reg = 0x27 + sprite_num
        color = rb(color_reg)
        mc_reg = rb(0x1C)
        multicolor = (mc_reg & (1 << sprite_num)) != 0

        pointer_addr = (vic_bank + screen_base + 0x3F8 + sprite_num) & 0xFFFF
        pointer = self.ram[pointer_addr]
        sprite_ram_base = (vic_bank + ((pointer & 0xFF) << 6)) & 0xFFFF

        return {
            'enabled': True,
            'x': x,
            'y': y,
            'color': color & 0x0F,
            'multicolor': multicolor,
            'pointer': pointer,
            'sprite_ram_base': sprite_ram_base,
        }

    def get_sprite_data(self, sprite_num: int, *, for_render: bool = False) -> dict:
        """Get sprite data for rendering.

        Use ``for_render=True`` from the pygame rasterizer so sprite regs match the
        same latched frame as ``get_render_display_mode``.
        """
        if for_render:
            snap = getattr(self, "_vic_render_snapshot", None)
            if snap is not None:
                regb, pra = snap
                vic_bank = (3 - (pra & 0x03)) * 0x4000
                screen_base = self._display_mode_from_vic_bytes(regb)['screen_base']
                return self._get_sprite_data_from_regs(sprite_num, regb, vic_bank, screen_base)

        regb = bytes(self._vic_regs[:0x40])
        mode_info = self.get_display_mode()
        return self._get_sprite_data_from_regs(
            sprite_num, regb, self.get_vic_bank_base(), mode_info['screen_base']
        )
