"""
6502 CPU Emulator
"""

from __future__ import annotations

import os
import sys

from typing import List, Optional, TYPE_CHECKING, Sequence, Union

from .vicii_cycle import ViciiCycleEngine

try:
    from .debug import OPCODE_SIZES, ViceTraceLogger
except ImportError:
    from debug import OPCODE_SIZES, ViceTraceLogger

from .constants import (
    SCREEN_MEM,
    COLOR_MEM,
    IRQ_VECTOR_HW,
    IRQ_VECTOR_SW,
    BLNSW,
    BLNCT,
    CURSOR_BLINK_TICKS,
    CURSOR_PTR_LOW,
    CURSOR_PTR_HIGH,
    CURSOR_ROW_ADDR,
    CURSOR_COL_ADDR,
)
from .cpu_state import CPUState
from .memory import MemoryMap

# Per 6502 instruction cycle: bus activity for BA vs CPU (VICE-style: stall only on reads).
_BUS_INTERNAL = 0
_BUS_READ = 1
_BUS_WRITE = 2

# 6502 relative branch opcodes (all bus cycles are reads; 2/3/4 cycles).
_BRANCH_OPCODES = frozenset({0x10, 0x30, 0x50, 0x70, 0x90, 0xB0, 0xD0, 0xF0})

# Indexed read / ALU path (not RMW, not store): every cycle is a bus read.
_ABS_X_READ = frozenset({
    0x1D, 0x3D, 0x5D, 0x7D, 0xBD, 0xDD, 0xFD, 0xBC,
})  # ORA/AND/EOR/ADC/LDA/CMP/SBC abs,X ; LDY abs,X
_ABS_Y_READ = frozenset({
    0x19, 0x39, 0x59, 0x79, 0xB9, 0xD9, 0xF9, 0xBE,
})  # ORA/AND/EOR/ADC/LDA/CMP/SBC abs,Y ; LDX abs,Y
_IND_Y_READ = frozenset({0x11, 0x31, 0x51, 0x71, 0xB1, 0xD1, 0xF1})
_IND_X_READ = frozenset({0x01, 0x21, 0x41, 0x61, 0xA1, 0xC1, 0xE1})

if TYPE_CHECKING:
    from .debug import UdpDebugLogger

class CPU6502:
    """6502 CPU emulator"""

    def __init__(
        self,
        memory: MemoryMap,
        interface=None,
        accurate_vic: bool = False,
        rust_hybrid_vic: bool = False,
    ):
        self.memory = memory
        self.interface = interface
        self.accurate_vic = accurate_vic
        # PAL Rust VIC cycle engine during fast batch (see --vic-emulation accurate-rust).
        self.rust_hybrid_vic = bool(rust_hybrid_vic)
        self.state = CPUState()
        # PC will be set from reset vector after ROMs are loaded
        # Don't read it here as ROMs might not be loaded yet
        self.state.pc = 0x0000
        self.chrout_count = 0
        self.trace_enabled = False
        self.trace_size = 0
        self.trace_buffer = []
        self.trace_index = 0
        self.trace_count = 0
        self.jiffy_clock = 0  # Initialize jiffy_clock as an attribute of CPU6502
        self._trace_sync_pc: Optional[int] = None
        try:
            v = os.environ.get("C64PY_TRACE_SYNC_PC")
            self._trace_sync_pc = int(v, 16) if v else None
        except Exception:
            self._trace_sync_pc = None
        # Optional one-shot poke (--debug-inject-at-cycle / --debug-inject-map in C64.py).
        self.debug_inject_at_cycle: Optional[int] = None
        # (addr, val) for RAM poke, or (reg_name, val) for reg_name in a,x,y,p
        self.debug_inject_writes: list[tuple[Union[int, str], int]] = []
        self.debug_inject_done: bool = False
        # When True, Rust batch stops at $FFD5/$FFD8 and the emulator applies Python KERNAL disk hooks.
        # Set False when IEC 1541 emulation is active so the real KERNAL vectors run.
        self.kernal_disk_hook_vectors: bool = True
        # Hot-path caches (invalidated when key changes; see _rust_delegate_stop_pcs / _python_only_step_pcs).
        self._rust_delegate_stop_pcs_key: Optional[tuple] = None
        self._rust_delegate_stop_pcs_cached: tuple[int, ...] = ()
        self._python_only_stop_key: Optional[int] = None
        self._python_only_stop_cache: Optional[frozenset[int]] = None

        # VICE-aligned VIC-II cycle engine (PAL 6569R3 / NTSC 6567R8 cycle tables).
        self.vic = ViciiCycleEngine()
        # Last (D011, D012, D015) shadow tuple applied to ViciiCycleEngine (hot path).
        self._vic_shadow_tuple: Optional[tuple[int, int, int]] = None
        self._vic_sprite_shadow_tuple: Optional[tuple[int, ...]] = None
        self.apply_video_standard_geometry()

    def apply_video_standard_geometry(self) -> None:
        """Set VIC cycle engine line length and raster height for PAL vs NTSC."""
        std = (self.memory.video_standard or "pal").lower()
        self.vic.video_standard = std
        if std == "ntsc":
            self.vic.cycles_per_line = 65
            self.vic.num_raster_lines = 263
        else:
            self.vic.cycles_per_line = 63
            self.vic.num_raster_lines = 312

    def _rust_hybrid_vic_effective(self) -> bool:
        """True when Rust batch should drive PAL VIC stepping (optional env opt-out)."""
        if not self.rust_hybrid_vic:
            return False
        v = os.environ.get("C64PY_RUST_HYBRID_VIC", "").strip().lower()
        if v in ("0", "no", "false", "off"):
            return False
        return True

    def _vic_sync_engine_shadow_regs(self) -> None:
        """Apply MemoryMap VIC shadow regs to ViciiCycleEngine only when they change."""
        t = self.memory.vic_stored_regs_d011_d012_d015()
        if t != self._vic_shadow_tuple:
            self._vic_shadow_tuple = t
            r11, r12, sp = t
            self.vic.set_d011(r11, 0)
            self.vic.set_d012(r12)
            self.vic.sprite_enable_mask = sp
        # Sprite state (Y positions + $D017) feeds the VICE-style sprite_dma
        # state machine. Sync lazily via tuple comparison to stay O(1) on the
        # hot path.
        st = self.memory.vic_stored_sprite_state()
        if st != self._vic_sprite_shadow_tuple:
            self._vic_sprite_shadow_tuple = st
            self.vic.sprite_y_expand_mask = st[0]
            ys = self.vic.sprite_y
            ys[0] = st[1]; ys[1] = st[2]; ys[2] = st[3]; ys[3] = st[4]
            ys[4] = st[5]; ys[5] = st[6]; ys[6] = st[7]; ys[7] = st[8]

    def _advance_raster(self, cycles: int) -> None:
        """Coarse raster advance used by fast VIC mode."""
        raster_max = 312 if self.memory.video_standard == "pal" else 263
        cycles_per_line = 63 if self.memory.video_standard == "pal" else 65
        step_cycles = max(1, cycles)
        self.memory.raster_cycles += step_cycles
        # Compute raster IRQ compare line from stored VIC registers
        raster_irq_line = (self.memory._vic_regs[0x12] & 0xFF) | ((self.memory._vic_regs[0x11] & 0x80) << 1)
        while self.memory.raster_cycles >= cycles_per_line:
            self.memory.raster_cycles -= cycles_per_line
            self.memory.raster_line = (self.memory.raster_line + 1) % raster_max
            self.memory.beam_capture_raster_line(self.memory.raster_line)
            # Trigger raster IRQ when line advances to the compare line
            if self.memory.raster_line == raster_irq_line:
                self.memory.trigger_vic_irq(0x01)
            if self.memory.raster_line == 0 and self.memory.vic_snapshot_each_emulated_frame:
                self.memory.snapshot_vic_render_state()

    def _vic_tick_one(self) -> tuple[bool, bool, bool]:
        """Advance VIC by one CPU cycle. Returns (ba_low, ba_blocks_cpu, raster_irq_edge)."""
        self._vic_sync_engine_shadow_regs()

        ba_low, ba_blocks_cpu, irq_edge = self.vic.tick()

        # Mirror VIC raster state into MemoryMap for $D011/$D012 reads.
        prev_line = self.memory.raster_line
        self.memory.raster_line = self.vic.raster_line
        self.memory.raster_cycles = self.vic.raster_cycle
        if self.memory.raster_line != prev_line:
            self.memory.beam_capture_raster_line(self.memory.raster_line)

        if irq_edge:
            self.memory.trigger_vic_irq(0x01)

        if self.vic.raster_line == 0 and self.vic.raster_cycle == 0:
            if self.memory.vic_snapshot_each_emulated_frame:
                self.memory.snapshot_vic_render_state()

        self.memory.per_cycle_capture_vic_sample()

        return ba_low, ba_blocks_cpu, irq_edge

    @staticmethod
    def _branch_condition(opcode: int, p: int) -> bool:
        """Whether a relative branch would be taken, from P before the instruction."""
        c = p & 0x01
        z = p & 0x02
        n = p & 0x80
        v = p & 0x40
        if opcode == 0x90:  # BCC
            return c == 0
        if opcode == 0xB0:  # BCS
            return c != 0
        if opcode == 0xF0:  # BEQ
            return z != 0
        if opcode == 0xD0:  # BNE
            return z == 0
        if opcode == 0x10:  # BPL
            return n == 0
        if opcode == 0x30:  # BMI
            return n != 0
        if opcode == 0x50:  # BVC
            return v == 0
        if opcode == 0x70:  # BVS
            return v != 0
        return False

    def _bus_cycle_phases(
        self, opcode: int, cycles: int, pc0: int, p0: int, x0: int, y0: int, op1: int, op2: int
    ) -> list[int]:
        """Per-cycle bus phase for BA-aware stalling: INTERNAL / READ / WRITE.

        `pc0`, `p0`, `x0`, `y0` are CPU state *before* the instruction executes (used for
        branch/indexed refinements). `op1`/`op2` are the bytes at PC+1 and PC+2 as read
        before execute (avoids duplicate bus reads for phase tables). Unknown opcodes
        default to all READ (conservative).
        """
        ph = [_BUS_READ] * max(0, cycles)

        # Relative branches: every cycle is a memory read (2 not taken, 3 taken, +1 page cross).
        if opcode in _BRANCH_OPCODES and cycles in (2, 3, 4):
            off = op1 & 0xFF
            rel = off - 256 if (off & 0x80) else off
            npc = (pc0 + 2) & 0xFFFF
            if self._branch_condition(opcode, p0):
                dest = (npc + rel) & 0xFFFF
                expect = 4 if (npc & 0xFF00) != (dest & 0xFF00) else 3
            else:
                expect = 2
            if expect != cycles:
                pass  # Executor cycle count is authoritative (IRQ / self-modify).
            return [_BUS_READ] * cycles

        # ORA/AND/EOR/ADC/LDA/CMP/SBC/LDY abs,X ; LDX abs,Y ; (zp),Y ; (zp,X) — all reads.
        if opcode in _ABS_X_READ and cycles in (4, 5):
            base = op1 | (op2 << 8)
            page_cross = (base & 0xFF00) != ((base + x0) & 0xFF00)
            expect = 5 if page_cross else 4
            if expect != cycles:
                pass
            return [_BUS_READ] * cycles
        if opcode in _ABS_Y_READ and cycles in (4, 5):
            base = op1 | (op2 << 8)
            page_cross = (base & 0xFF00) != ((base + y0) & 0xFF00)
            expect = 5 if page_cross else 4
            if expect != cycles:
                pass
            return [_BUS_READ] * cycles
        if opcode in _IND_Y_READ and cycles in (5, 6):
            zp = op1 & 0xFF
            lo = self.memory.read(zp & 0xFFFF)
            hi = self.memory.read((zp + 1) & 0xFFFF)
            base = lo | (hi << 8)
            page_cross = (base & 0xFF00) != ((base + y0) & 0xFF00)
            expect = 6 if page_cross else 5
            if expect != cycles:
                pass
            return [_BUS_READ] * cycles
        if opcode in _IND_X_READ and cycles == 6:
            return [_BUS_READ] * cycles

        # Stores: last cycle is a write (simplified).
        store_opcodes = {
            0x85, 0x95, 0x8D, 0x9D, 0x99, 0x81, 0x91,  # STA
            0x86, 0x8E, 0x96,  # STX
            0x84, 0x8C, 0x94,  # STY
        }
        # RMW: last two cycles are writes on 6502 (simplified).
        rmw_opcodes = {
            0x06, 0x16, 0x0E, 0x1E,  # ASL
            0x26, 0x36, 0x2E, 0x3E,  # ROL
            0x46, 0x56, 0x4E, 0x5E,  # LSR
            0x66, 0x76, 0x6E, 0x7E,  # ROR
            0xC6, 0xD6, 0xCE, 0xDE,  # DEC
            0xE6, 0xF6, 0xEE, 0xFE,  # INC
        }

        if opcode in store_opcodes and cycles >= 1:
            ph[-1] = _BUS_WRITE
        elif opcode in rmw_opcodes and cycles >= 2:
            ph[-1] = _BUS_WRITE
            ph[-2] = _BUS_WRITE

        # Implied/accumulator 2-cycle ops: second cycle is internal (no bus).
        implied_internal_2 = {
            0xCA, 0x88, 0xE8, 0xC8,  # DEX/DEY/INX/INY
            0x18, 0x38, 0x58, 0x78,  # CLC/SEC/CLI/SEI
            0xB8, 0xD8, 0xF8,        # CLV/CLD/SED
            0xEA,                    # NOP
            0xAA, 0xA8, 0x8A, 0x98,  # TAX/TAY/TXA/TYA
            0xBA, 0x9A,              # TSX/TXS
        }
        acc_shift_rotate = {0x0A, 0x2A, 0x4A, 0x6A}  # ASL/ROL/LSR/ROR A
        if cycles == 2 and (opcode in implied_internal_2 or opcode in acc_shift_rotate):
            ph[1] = _BUS_INTERNAL

        # JSR: cycles 4-5 are writes (push return address).
        if opcode == 0x20 and cycles >= 6:
            ph[3] = _BUS_WRITE
            ph[4] = _BUS_WRITE

        # RTS: first 3 cycles are reads, then reads; no writes (leave all-read).
        if opcode == 0x60 and cycles >= 6:
            # One internal cycle during return address increment.
            ph[4] = _BUS_INTERNAL

        # RTI: pulls P/PC (reads).
        if opcode == 0x40 and cycles >= 6:
            # One internal cycle after pulling PC.
            ph[4] = _BUS_INTERNAL

        # BRK: pushes PC/P (writes) on cycles 3-5 (7 cycles total).
        if opcode == 0x00 and cycles >= 7:
            ph[2] = _BUS_WRITE
            ph[3] = _BUS_WRITE
            ph[4] = _BUS_WRITE

        # PHA/PHP: last cycle write.
        if opcode in (0x48, 0x08) and cycles >= 3:
            ph[1] = _BUS_INTERNAL
            ph[-1] = _BUS_WRITE

        # PLA/PLP: 4 cycles, with internal cycles around the stack read.
        if opcode in (0x68, 0x28) and cycles >= 4:
            ph[1] = _BUS_INTERNAL
            ph[3] = _BUS_INTERNAL

        return ph

    def enable_trace(self, size: int = 1024) -> None:
        self.trace_enabled = True
        self.trace_size = max(1, size)
        self.trace_buffer = [None] * self.trace_size
        self.trace_index = 0
        self.trace_count = 0

    def _record_trace(self, pc: int, opcode: int) -> None:
        if not self.trace_enabled:
            return
        op1 = self.memory.read((pc + 1) & 0xFFFF)
        op2 = self.memory.read((pc + 2) & 0xFFFF)
        self.trace_buffer[self.trace_index] = {
            "pc": pc,
            "opcode": opcode,
            "op1": op1,
            "op2": op2,
            "a": self.state.a,
            "x": self.state.x,
            "y": self.state.y,
            "sp": self.state.sp,
            "p": self.state.p,
            "cycles": self.state.cycles,
        }
        self.trace_index = (self.trace_index + 1) % self.trace_size
        self.trace_count = min(self.trace_count + 1, self.trace_size)

    def get_trace(self) -> list[dict]:
        if not self.trace_count:
            return []
        start = (self.trace_index - self.trace_count) % self.trace_size
        entries = []
        for i in range(self.trace_count):
            idx = (start + i) % self.trace_size
            entry = self.trace_buffer[idx]
            if entry is not None:
                entries.append(entry)
        return entries

    def _mr(self, addr: int) -> int:
        """CPU bus read.

        **Fast VIC:** plain ``memory.read``; raster/CIA/SID advance once per instruction in
        :meth:`step` (matches historical throughput; per-access stepping was a major regression).

        **Accurate VIC:** no time advance here — the bus-phase loop in :meth:`step` drives cycles.
        """
        return self.memory.read(addr)

    def _mw(self, addr: int, value: int) -> None:
        """CPU bus write (see :meth:`_mr` for fast vs accurate timing)."""
        self.memory.write(addr, value)

    def _read_word(self, addr: int) -> int:
        """Read 16-bit word (little-endian)"""
        low = self._mr(addr)
        high = self._mr((addr + 1) & 0xFFFF)
        return low | (high << 8)

    def _get_flag(self, flag: int) -> bool:
        """Get processor flag"""
        return (self.state.p & flag) != 0

    def _set_flag(self, flag: int, value: bool) -> None:
        """Set processor flag"""
        if value:
            self.state.p |= flag
        else:
            self.state.p &= ~flag

    def _clear_flag(self, flag: int) -> None:
        """Clear processor flag"""
        self.state.p &= ~flag

    def _update_flags(self, value: int) -> None:
        """Update Z and N flags based on value"""
        value &= 0xFF
        self._set_flag(0x02, value == 0)  # Z flag
        self._set_flag(0x80, (value & 0x80) != 0)  # N flag

    def _page_crossed(self, base: int, offset: int) -> bool:
        """Check if adding offset to base crosses a page boundary"""
        return (base & 0xFF00) != ((base + offset) & 0xFF00)

    def _adc_finish(self, old_a: int, value: int, wide_result: int) -> None:
        """Set C, V, Z, N and A from ADC wide sum (old A + memory + carry-in)."""
        self._set_flag(0x01, wide_result > 0xFF)
        r = wide_result & 0xFF
        self._set_flag(0x40, ((~(old_a ^ value)) & (old_a ^ r)) & 0x80)
        self.state.a = r
        self._update_flags(self.state.a)

    def _irq_should_dispatch(self) -> bool:
        """Post-instruction IRQ poll with canonical CLI/SEI/PLP 1-cycle delay.

        Real 6502 polls the IRQ line **during** each instruction (around the
        penultimate cycle). CLI/SEI/PLP modify the I flag in their *last*
        cycle, so the poll that happens inside those instructions still uses
        the *old* I value. Net effect: the flag change is observable by IRQ
        logic only on the following instruction.

        We approximate this at instruction granularity: when an opcode has
        set :attr:`CPUState.cli_sei_delay`, the next poll uses
        :attr:`CPUState.pre_i_flag` (the I bit as it was *before* the
        opcode) and then consumes the delay. Critical for raster-IRQ
        dispatch where games place a CLI immediately before a $D012
        write to a value the beam has just passed.
        """
        if self.state.cli_sei_delay:
            i_masked = (self.state.pre_i_flag & 0x04) != 0
            self.state.cli_sei_delay = False
        else:
            i_masked = (self.state.p & 0x04) != 0
        return self.memory.pending_irq and not i_masked

    def _advance_time(self, cycles: int, udp_debug: Optional['UdpDebugLogger'] = None) -> None:
        """Advance timers/video/IRQs even if CPU is 'blocked'."""
        if self.accurate_vic:
            for _ in range(max(0, cycles)):
                self._vic_tick_one()
                self.state.cycles += 1
                self._update_cia_timers(1, recompute_irq=False)
                self.memory.sid_tick_cpu_cycles(1)
        else:
            self.memory.sid_tick_cpu_cycles(cycles)
            self.state.cycles += cycles
            self._update_cia_timers(cycles)
            self._advance_raster(cycles)
        self.memory.recompute_pending_irq()

        if self._irq_should_dispatch():
            self._handle_irq()  # Let KERNAL handle IRQ (cursor blink, keyboard, etc.)

    def _step_cycles(self, cycles: int) -> None:
        """Advance emulated time by *cycles* CPU cycles (VIC-driven)."""
        if self.accurate_vic:
            for _ in range(max(0, cycles)):
                self._vic_tick_one()
                self.state.cycles += 1
                self._update_cia_timers(1, recompute_irq=False)
                self.memory.sid_tick_cpu_cycles(1)
        else:
            self.memory.sid_tick_cpu_cycles(cycles)
            self.state.cycles += cycles
            self._update_cia_timers(cycles)
            self._advance_raster(cycles)
        self.memory.recompute_pending_irq()
        if self._irq_should_dispatch():
            self._handle_irq()

    def _maybe_apply_debug_inject(self) -> None:
        """Apply debug_inject_writes once, on first step() where cycles >= inject cycle."""
        if self.debug_inject_done or self.debug_inject_at_cycle is None:
            return
        if not self.debug_inject_writes:
            return
        if self.state.cycles < self.debug_inject_at_cycle:
            return
        self.debug_inject_done = True
        parts: list[str] = []
        for target, val in self.debug_inject_writes:
            val &= 0xFF
            if isinstance(target, str):
                reg = target.lower()
                if reg == "a":
                    old, self.state.a = self.state.a, val
                    parts.append(f"A=${val:02X}(was${old:02X})")
                elif reg == "x":
                    old, self.state.x = self.state.x, val
                    parts.append(f"X=${val:02X}(was${old:02X})")
                elif reg == "y":
                    old, self.state.y = self.state.y, val
                    parts.append(f"Y=${val:02X}(was${old:02X})")
                elif reg in ("p", "flags"):
                    old, self.state.p = self.state.p, val
                    parts.append(f"P=${val:02X}(was${old:02X})")
                else:
                    parts.append(f"?{target}=ignored")
                continue
            addr = int(target) & 0xFFFF
            old = self.memory.ram[addr] if addr < len(self.memory.ram) else 0
            self.memory.write(addr, val)
            parts.append(f"${addr:04X}=${val:02X}(was${old:02X})")
        msg = (
            f"DEBUG_INJECT cyc={self.state.cycles} pc=${self.state.pc & 0xFFFF:04X} "
            + " ".join(parts)
        )
        print(msg, file=sys.stderr)

    def _chrout_petscii_screen_effect(self, char: int) -> None:
        """Update screen/cursor for one PETSCII character (CHROUT semantics, no RTS)."""
        self.memory.write(0xD0, 0)
        cursor_low = self.memory.read(0xD1)
        cursor_high = self.memory.read(0xD2)
        cursor_addr = cursor_low | (cursor_high << 8)
        if cursor_addr < SCREEN_MEM or cursor_addr >= SCREEN_MEM + 1000:
            cursor_addr = SCREEN_MEM
        self.last_chrout_char = char
        if char == 0x0D:
            row = (cursor_addr - SCREEN_MEM) // 40
            if row < 24:
                cursor_addr = SCREEN_MEM + (row + 1) * 40
            else:
                self.memory._scroll_screen_up()
                cursor_addr = SCREEN_MEM + 24 * 40
        elif char == 0x0A:
            pass
        elif char == 0x14:
            if cursor_addr > SCREEN_MEM:
                cursor_addr -= 1
                if SCREEN_MEM <= cursor_addr < SCREEN_MEM + 1000:
                    self.memory.write(cursor_addr, 0x20)
                    current_color = self.memory.read(0x0286) & 0x0F
                    self.memory.write(COLOR_MEM + (cursor_addr - SCREEN_MEM), current_color)
        elif char == 0x93:
            for addr in range(SCREEN_MEM, SCREEN_MEM + 1000):
                self.memory.write(addr, 0x20)
            current_color = self.memory.read(0x0286) & 0x0F
            for addr in range(COLOR_MEM, COLOR_MEM + 1000):
                self.memory.write(addr, current_color)
            cursor_addr = SCREEN_MEM
        else:
            if SCREEN_MEM <= cursor_addr < SCREEN_MEM + 1000:
                # Convert PETSCII to C64 screen code before storing in screen RAM.
                # Standard mapping (matches KERNAL BSOUT at $E716):
                #   $20-$3F → $20-$3F  (space, punctuation, digits — identity)
                #   $40-$5F → $00-$1F  (@, A-Z, [\]↑← — subtract $40)
                #   $60-$7F → $40-$5F  (subtract $20)
                #   $A0-$BF → $60-$7F  (subtract $40)
                #   $C0-$FE → $80-$BE  (subtract $40)
                #   $FF     → $5E
                c = char & 0xFF
                if 0x40 <= c <= 0x5F:
                    screen_code = c - 0x40
                elif 0x60 <= c <= 0x7F:
                    screen_code = c - 0x20
                elif 0xA0 <= c <= 0xBF:
                    screen_code = c - 0x40
                elif 0xC0 <= c <= 0xFE:
                    screen_code = c - 0x40
                elif c == 0xFF:
                    screen_code = 0x5E
                else:
                    screen_code = c
                self.memory.write(cursor_addr, screen_code)
                current_color = self.memory.read(0x0286) & 0x0F
                self.memory.write(COLOR_MEM + (cursor_addr - SCREEN_MEM), current_color)
                cursor_addr += 1
                if cursor_addr >= SCREEN_MEM + 1000:
                    self.memory._scroll_screen_up()
                    cursor_addr = SCREEN_MEM + 24 * 40
        self.memory.write(0xD1, cursor_addr & 0xFF)
        self.memory.write(0xD2, (cursor_addr >> 8) & 0xFF)
        row = (cursor_addr - SCREEN_MEM) // 40
        col = (cursor_addr - SCREEN_MEM) % 40
        self.memory.write(CURSOR_ROW_ADDR, row)
        self.memory.write(CURSOR_COL_ADDR, col)

    def apply_chrout_petscii(self, char: int) -> None:
        """Emit one PETSCII character using the same screen rules as CHROUT (no JSR/RTS)."""
        self._chrout_petscii_screen_effect(char & 0xFF)

    def step(self, udp_debug: Optional['UdpDebugLogger'] = None, current_cycles: int = 0,
             vice_trace=None) -> int:
        """Execute one instruction, return cycles"""
        self.current_cycles = current_cycles
        if self.state.stopped:
            # If CPU is stopped (KIL), don't execute anything
            # Return 1 cycle to prevent infinite loops in the run loop
            self._step_cycles(1)
            return 1

        self._maybe_apply_debug_inject()

        pc = self.state.pc
        opcode = self.memory.read(pc)
        # Bus-phase / debug context: only used when accurate_vic refines per-cycle access.
        if self.accurate_vic:
            self.memory.debug_last_pc = pc
            self.memory.debug_last_cycles = self.state.cycles
            self.memory.debug_last_opcode = opcode
            self.memory.debug_last_op1 = self.memory.read((pc + 1) & 0xFFFF)
            self.memory.debug_last_op2 = self.memory.read((pc + 2) & 0xFFFF)
        self._record_trace(pc, opcode)

        # Trace-only aid: force VIC raster phase to a known point at the sync PC so
        # drift analysis focuses on badline/IRQ logic rather than boot-time phase.
        if self._trace_sync_pc is not None and pc == self._trace_sync_pc:
            if self.accurate_vic:
                self.vic = ViciiCycleEngine()
                self.apply_video_standard_geometry()
            self.memory.raster_line = 0
            self.memory.raster_cycles = 0

        # Log instruction execution if UDP debug is enabled
        # Note: cycles haven't been incremented yet, so we log the current cycle count
        # The actual cycles for this instruction will be returned and added later
        if udp_debug and udp_debug.enabled:
            # Sample logging to avoid queue overflow (log every 1000 cycles or important events)
            should_log = (self.state.cycles % 1000 == 0) or (opcode == 0x00)  # Log BRK instructions

            if should_log:
                # Minimal data to reduce JSON/serialization overhead
                udp_debug.send('cpu_step', {
                    'pc': pc,
                    'opcode': opcode,
                    'cycles': self.state.cycles
                })
        
        # Log to VICE-compatible trace file if enabled
        if vice_trace and vice_trace.enabled:
            size = OPCODE_SIZES.get(opcode, 1)
            operand_bytes = [self.memory.read(pc + i) for i in range(1, size)]
            vice_trace.log_instruction(
                pc, opcode, operand_bytes,
                self.state.a, self.state.x, self.state.y, self.state.sp,
                self.state.p, self.state.cycles
            )

        # Special handling for CINT when no KERNAL ROM is loaded.
        # If the ROM is present, let the KERNAL initialize its own editor state.
        # Python-only path; keep :meth:`_python_only_step_pcs` and Rust stop-PC list in sync.
        if pc == 0xFF5B and self.memory.kernal_rom is None:  # Start of CINT
            if self.interface:
                self.interface.add_debug_log("🎯 CINT: Fast-path init (screen + default colors)")
            # CINT is supposed to:
            # 1. Clear screen memory
            # 2. Detect PAL/NTSC by timing
            # 3. Set up VIC registers
            # For emulator, we skip timing and assume configured standard

            # Restore default C64 look so SYS 64738 behaves like a reboot:
            # border light blue, background blue, text light blue.
            # Use VIC register model directly so it works regardless of banking.
            try:
                self.memory.poke_vic(0x20, 0x0E)  # border
                self.memory.poke_vic(0x21, 0x06)  # background
                self.memory.poke_vic(0x18, 0x15)  # screen at $0400, chars at char ROM ($1000)
            except Exception:
                # If VIC helpers aren't available for some reason, fall back to I/O writes.
                self.memory.write(0xD020, 0x0E)
                self.memory.write(0xD021, 0x06)
                self.memory.write(0xD018, 0x15)

            # Current text/cursor color (POKE 646 and cursor color)
            self.memory.write(0x0286, 0x0E)
            self.memory.write(0x0288, 0x0E)

            # Clear screen and set color RAM to current text color.
            for addr in range(SCREEN_MEM, SCREEN_MEM + 1000):
                self.memory.write(addr, 0x20)
            for addr in range(COLOR_MEM, COLOR_MEM + 1000):
                self.memory.write(addr, 0x0E)

            # Reset cursor position to top-left.
            self.memory.write(CURSOR_PTR_LOW, SCREEN_MEM & 0xFF)
            self.memory.write(CURSOR_PTR_HIGH, (SCREEN_MEM >> 8) & 0xFF)
            self.memory.write(CURSOR_ROW_ADDR, 0)
            self.memory.write(CURSOR_COL_ADDR, 0)

            # Reset machine-controlled cursor blink state.
            # bit0 = enabled, bit7 = visible
            self.memory.write(BLNSW, 0x81)
            self.memory.write(BLNCT, 0)

            # Simulate CINT completing by setting PC to FCFE, adjust stack
            self.state.pc = 0xFCFE  # Return to CLI instruction
            self.state.sp += 2  # Pop the return address from stack
            self._step_cycles(1)
            return 1  # Minimal cycles


        # Check if we're at a KERNAL vector that needs handling.
        # These fallbacks are only used when the KERNAL ROM is missing.
        # Python-only; keep :meth:`_python_only_step_pcs` and Rust stop-PC list in sync.
        # CHRIN ($FFCF) - Input character from keyboard
        if pc == 0xFFCF and self.memory.kernal_rom is None:
            # CHRIN - return character from input/keyboard buffers
            char_ready = False
            char = 0

            # Check BASIC input buffer ($0200) first (line editing)
            line_len = self.memory.read(0x029C)
            line_idx = self.memory.read(0x029B)
            if line_len > 0:
                if line_idx >= line_len:
                    # Reset invalid pointers
                    self.memory.write(0x029B, 0)
                    self.memory.write(0x029C, 0)
                else:
                    char = self.memory.read(0x0200 + line_idx)
                    line_idx += 1
                    self.memory.write(0x029B, line_idx)
                    if line_idx >= line_len:
                        self.memory.write(0x029B, 0)
                        self.memory.write(0x029C, 0)
                    char_ready = True

            if not char_ready:
                # Keyboard buffer is at $0277-$0280 (10 bytes)
                # $C6 contains the number of characters in buffer
                kb_buf_len = self.memory.read(0xC6)  # Number of chars in buffer
                # Clamp buffer length to valid range (0-10)
                if kb_buf_len > 10:
                    kb_buf_len = 10
                    self.memory.write(0xC6, kb_buf_len)

                if kb_buf_len > 0:
                    # Read first character from buffer (at $0277)
                    kb_buf_base = 0x0277
                    char = self.memory.read(kb_buf_base)

                    # Shift remaining characters down (C64 KERNAL behavior)
                    for i in range(kb_buf_len - 1):
                        next_char = self.memory.read(kb_buf_base + i + 1)
                        self.memory.write(kb_buf_base + i, next_char)

                    # Clear the last position
                    self.memory.write(kb_buf_base + kb_buf_len - 1, 0)

                    # Decrement buffer length
                    kb_buf_len = (kb_buf_len - 1) & 0xFF
                    self.memory.write(0xC6, kb_buf_len)

                    char_ready = True
                else:
                    # CHRIN should BLOCK when keyboard buffer is empty
                    # On real C64, CHRIN waits for screen editor to collect input line
                    # We should NOT return 0 - instead, don't advance PC (block)
                    # However, for emulation, we need to handle RUN injection

                    # Inject "RUN" command if program was loaded (only once)
                    emu = self.interface.emulator if self.interface and hasattr(self.interface, 'emulator') else None
                    if emu and emu.program_loaded:
                        if not hasattr(self, '_run_injected'):
                            self._run_injected = True
                            run_command = b"RUN\x0D"  # RUN + carriage return
                            # Put RUN command into keyboard buffer at correct position
                            kb_buf_base = 0x0277
                            # Clear buffer first
                            for i in range(10):
                                self.memory.write(kb_buf_base + i, 0)
                            # Write command
                            for i, run_char in enumerate(run_command):
                                if i < 10:  # Buffer is only 10 bytes
                                    self.memory.write(kb_buf_base + i, run_char)
                            self.memory.write(0xC6, min(len(run_command), 10))  # Set buffer length (max 10)
                            if self.interface:
                                self.interface.add_debug_log("💾 Injected 'RUN' command into keyboard buffer")
                            # After injection, retry reading from buffer
                            kb_buf_len = self.memory.read(0xC6)
                            if kb_buf_len > 0:
                                # Buffer now has data, read it
                                char = self.memory.read(kb_buf_base)
                                # Shift buffer
                                for i in range(kb_buf_len - 1):
                                    next_char = self.memory.read(kb_buf_base + i + 1)
                                    self.memory.write(kb_buf_base + i, next_char)
                                self.memory.write(kb_buf_base + kb_buf_len - 1, 0)
                                kb_buf_len = (kb_buf_len - 1) & 0xFF
                                self.memory.write(0xC6, kb_buf_len)
                                char_ready = True
                            else:
                                # Still empty after injection (shouldn't happen)
                                # Block by not advancing PC, but still advance timers/IRQs.
                                self._advance_time(1, udp_debug=udp_debug)
                                return 1  # PC stays at CHRIN
                        else:
                            # Already injected, buffer still empty - block
                            # Don't advance PC, return minimal cycles
                            self._advance_time(1, udp_debug=udp_debug)
                            return 1  # Block: PC stays at $FFCF
                    else:
                        # No program loaded, buffer empty - block
                        # Don't advance PC, return minimal cycles
                        self._advance_time(1, udp_debug=udp_debug)
                        return 1  # Block: PC stays at $FFCF

            if not char_ready:
                self._advance_time(1, udp_debug=udp_debug)
                return 1

            self.state.a = char

            # Return from JSR (RTS behavior) - only if we actually returned a character
            # If we're blocking (returned early), don't do RTS - PC stays at CHRIN
            # Stack grows downward, so we pop by incrementing SP
            # JSR pushed (return_address - 1) with high byte first, then low byte
            # So we pop low byte first, then high byte
            self.state.sp = (self.state.sp + 1) & 0xFF
            pc_low = self.memory.read(0x100 + self.state.sp)
            self.state.sp = (self.state.sp + 1) & 0xFF
            pc_high = self.memory.read(0x100 + self.state.sp)
            # Reconstruct return address with correct carry semantics.
            self.state.pc = (((pc_high << 8) | pc_low) + 1) & 0xFFFF

            # Safety check: if return address is invalid (e.g., $0000), something is wrong
            if self.state.pc == 0x0000:
                if udp_debug and udp_debug.enabled:
                    udp_debug.send('chrin_error', {
                        'error': 'Invalid return address $0000',
                        'sp': self.state.sp,
                        'stack_ff': self.memory.read(0x01FF),
                        'stack_fe': self.memory.read(0x01FE)
                    })
                # Don't jump to $0000 - instead stop CPU or use a safe address
                self.state.stopped = True
                self._step_cycles(20)
                return 20

            if udp_debug and udp_debug.enabled:
                kb_buf_len = self.memory.read(0xC6)
                udp_debug.send('chrin', {
                    'char': self.state.a,
                    'kb_buf_len': kb_buf_len
                })

            self._step_cycles(20)
            return 20  # Approximate cycles for CHRIN

        # CHROUT ($FFD2) - Output character to screen
        # Python-only shortcut; Rust fast batch must stop before executing here
        # (:meth:`_python_only_step_pcs`, :meth:`_rust_delegate_stop_pcs`).
        # Keep a compatibility implementation so screen output works even when
        # the ROM screen editor path is not fully supported by the CPU core.
        if pc == 0xFFD2 and self.memory.kernal_shortcuts_enabled:
            char = self.state.a
            self.chrout_count += 1

            if udp_debug and udp_debug.enabled:
                udp_debug.send('chrout_entry', {
                    'char': char,
                    'ascii': chr(char),
                    'pc': pc,
                    'sp': self.state.sp,
                    'cycles': getattr(self, 'current_cycles', 0)
                })

            self._chrout_petscii_screen_effect(char)

            self._clear_flag(0x01)  # Clear carry flag (bit 0)

            sp_before = self.state.sp
            self.state.sp = (self.state.sp + 1) & 0xFF
            pc_low = self.memory.read(0x100 + self.state.sp)
            self.state.sp = (self.state.sp + 1) & 0xFF
            pc_high = self.memory.read(0x100 + self.state.sp)
            return_addr = ((pc_high << 8) | pc_low) + 1
            self.state.pc = return_addr & 0xFFFF

            if udp_debug and udp_debug.enabled:
                udp_debug.send('chrout_rts', {
                    'sp_before': sp_before,
                    'sp_after': self.state.sp,
                    'pc_low': pc_low,
                    'pc_high': pc_high,
                    'return_addr': f'${return_addr:04X}',
                    'new_pc': f'${self.state.pc:04X}'
                })

            if self.state.pc == 0x0000:
                if udp_debug and udp_debug.enabled:
                    udp_debug.send('chrout_error', {
                        'error': 'Invalid return address $0000',
                        'sp_before': (self.state.sp - 2) & 0xFF,
                        'sp_after': self.state.sp,
                        'stack_low': pc_low,
                        'stack_high': pc_high
                    })
                self.state.stopped = True
                self._step_cycles(20)
                return 20

            if udp_debug and udp_debug.enabled:
                cursor_low = self.memory.read(0xD1)
                cursor_high = self.memory.read(0xD2)
                cursor_addr = cursor_low | (cursor_high << 8)
                udp_debug.send('chrout', {
                    'char': char,
                    'char_hex': f'${char:02X}',
                    'cursor_addr': cursor_addr,
                    'screen_addr': SCREEN_MEM,
                    'cycles': getattr(self, 'current_cycles', 0),
                    'pc': self.state.pc
                })

            self._step_cycles(20)
            return 20  # Approximate cycles for CHROUT

        # Pre-instruction state for bus-phase refinement (branches, indexed modes).
        pc0 = pc
        p0 = self.state.p
        x0 = self.state.x
        y0 = self.state.y

        cycles = self._execute_opcode(opcode)
        if self.accurate_vic:
            op1 = self.memory.debug_last_op1
            op2 = self.memory.debug_last_op2
            pattern = self._bus_cycle_phases(opcode, cycles, pc0, p0, x0, y0, op1, op2)
            elapsed = 0
            vic_tick_one = self._vic_tick_one
            update_cia = self._update_cia_timers
            st = self.state
            mem_sid = self.memory.sid_tick_cpu_cycles
            pat = pattern
            patlen = len(pat)
            for i in range(cycles):
                bus_phase = pat[i] if i < patlen else _BUS_READ
                while True:
                    _ba_low, ba_blocks_cpu, _irq_edge = vic_tick_one()
                    st.cycles += 1
                    update_cia(1, recompute_irq=False)
                    mem_sid(1)
                    elapsed += 1
                    # Stall CPU only on read cycles while BA blocks (VICE behavior).
                    if not (ba_blocks_cpu and bus_phase == _BUS_READ):
                        break

            self.memory.recompute_pending_irq()
            if self._irq_should_dispatch():
                self._handle_irq()
            return elapsed

        # Fast/coarse mode: one batched advance per instruction (same idea as pre-042ed1a / e19c832).
        if not self.accurate_vic:
            self._advance_raster(cycles)
            if self.memory.badline_cycles > 0:
                cycles += self.memory.badline_cycles
                self.memory.badline_cycles = 0
            self.state.cycles += cycles
            self.memory.sid_tick_cpu_cycles(cycles)
            self._update_cia_timers(cycles, recompute_irq=False)
        self.memory.recompute_pending_irq()
        if self._irq_should_dispatch():
            # Coarse mode: follow historical behavior and service CIA-driven IRQ path.
            if self.memory.cia1_icr & 0x80:
                self._handle_irq()
        return cycles

    def _rust_fast_batch_usable(self) -> bool:
        if os.environ.get("C64PY_USE_RUST_FAST", "1").strip().lower() in ("0", "no", "false"):
            return False
        try:
            from . import _core
        except ImportError:
            return False
        if not _core.is_available:
            return False
        hybrid_vic = self._rust_hybrid_vic_effective()
        if self.accurate_vic and not hybrid_vic:
            return False
        # Note: trace_enabled no longer blocks Rust batch - Rust handles tracing internally
        if self._trace_sync_pc is not None:
            return False
        if self.debug_inject_at_cycle is not None or self.debug_inject_writes:
            return False
        sid = self.memory.sid
        if sid is not None:
            resid_ok = (
                os.environ.get("C64PY_RUST_RESID_LOCKSTEP", "1").strip().lower() not in ("0", "no", "false")
                and hasattr(sid, "rust_batch_sid_ptr")
                and hasattr(sid, "find_resid_lib")
            )
            if not resid_ok:
                return False
        if not isinstance(self.memory.ram, bytearray):
            return False
        return True

    def _rust_delegate_stop_pcs(self) -> tuple[int, ...]:
        """PCs where a Rust batch must hand off to Python (hooks + ``step()`` shortcuts).

        Includes LOAD/SAVE vectors when :attr:`kernal_disk_hook_vectors` is True so ``C64``
        KERNAL hooks run between batches. When IEC is active but :attr:`MemoryMap.iec_disk_full_impl`
        is False, those vectors are still included so :meth:`emulator.C64Emulator.run_cpu_instruction_quantum`
        can run the Python IEC stub (or hooks) before executing at those PCs.

        Result is cached: this runs once per emulated instruction quantum in the hot path.
        """
        iec_stub = (
            self.memory.iec_bus is not None
            and not getattr(self.memory, "iec_disk_full_impl", False)
        )
        key = (
            self.kernal_disk_hook_vectors,
            iec_stub,
            self.memory.kernal_rom is None,
        )
        if self._rust_delegate_stop_pcs_key != key:
            pcs = [0xFFD2]
            if self.kernal_disk_hook_vectors or iec_stub:
                pcs.extend((0xFFD5, 0xFFD8))
            if self.memory.kernal_rom is None:
                pcs.extend((0xFF5B, 0xFFCF))
            self._rust_delegate_stop_pcs_cached = tuple(sorted(set(pcs)))
            self._rust_delegate_stop_pcs_key = key
        return self._rust_delegate_stop_pcs_cached

    def _python_only_step_pcs(self) -> frozenset[int]:
        """PCs handled in :meth:`step` before ``_execute_opcode``; must match delegate stops where applicable."""
        rom = self.memory.kernal_rom
        key = id(rom)
        if self._python_only_stop_key != key or self._python_only_stop_cache is None:
            s = {0xFFD2}
            if rom is None:
                s.add(0xFF5B)
                s.add(0xFFCF)
            self._python_only_stop_cache = frozenset(s)
            self._python_only_stop_key = key
        return self._python_only_stop_cache

    def step_fast_batch(
        self, max_instructions: int, stop_pcs: Optional[Sequence[int]] = None, trace_path: Optional[str] = None
    ) -> tuple[int, int]:
        """Run up to ``max_instructions`` instructions.

        Uses the optional Rust core when :meth:`_rust_fast_batch_usable` is true; otherwise
        falls back to repeated :meth:`step`. ``stop_pcs`` defaults to
        :meth:`_rust_delegate_stop_pcs` (Rust exits the batch before executing at those PCs).

        Returns ``(instructions_executed, cycles_emulated)``.
        """
        if max_instructions <= 0:
            return 0, 0
        if self.state.stopped:
            return 0, 0
        if not self._rust_fast_batch_usable():
            ins = 0
            cyc = 0
            for _ in range(max_instructions):
                if self.state.stopped:
                    break
                cyc += self.step()
                ins += 1
            return ins, cyc

        from . import _core

        stops = self._rust_delegate_stop_pcs() if stop_pcs is None else stop_pcs
        hybrid_vic = self.accurate_vic and self._rust_hybrid_vic_effective()
        sid = self.memory.sid
        use_rust_resid = False
        resid_lib_path = None
        resid_ptr = None
        if (
            sid is not None
            and os.environ.get("C64PY_RUST_RESID_LOCKSTEP", "1").strip().lower() not in ("0", "no", "false")
            and hasattr(sid, "rust_batch_sid_ptr")
            and hasattr(sid, "find_resid_lib")
        ):
            resid_ptr = int(sid.rust_batch_sid_ptr())
            resid_lib_path = sid.find_resid_lib()
            use_rust_resid = bool(resid_ptr and resid_lib_path)

        def _run_batch(trace_path: Optional[str] = None):
            return _core.run_fast_batch(
                self.memory,
                max_instructions=max_instructions,
                pc=self.state.pc,
                a=self.state.a,
                x=self.state.x,
                y=self.state.y,
                sp=self.state.sp,
                p=self.state.p,
                cycles=self.state.cycles,
                stopped=self.state.stopped,
                basic_rom=self.memory.basic_rom,
                kernal_rom=self.memory.kernal_rom,
                char_rom=self.memory.char_rom,
                stop_pcs=stops,
                hybrid_vic_pal=hybrid_vic,
                vic_engine=self.vic if hybrid_vic else None,
                resid_lib_path=resid_lib_path if use_rust_resid else None,
                resid_ptr=resid_ptr if use_rust_resid else None,
                trace_path=trace_path,
            )

        ins, cyc, opc, oa, ox, oy, osp, op, ocycles, ostopped = _run_batch(trace_path=trace_path)
        self.state.pc = opc
        self.state.a = oa
        self.state.x = ox
        self.state.y = oy
        self.state.sp = osp
        self.state.p = op
        self.state.cycles = ocycles
        self.state.stopped = ostopped
        if not use_rust_resid:
            self.memory.sid_tick_cpu_cycles(cyc)
        return ins, cyc

    def _rust_vice_trace_path(self) -> Optional[str]:
        p = os.environ.get("C64PY_RUST_VICE_TRACE", "").strip()
        return p or None

    def _ensure_rust_vice_trace_logger(self) -> Optional[ViceTraceLogger]:
        path = self._rust_vice_trace_path()
        if not path:
            return None
        lg: Optional[ViceTraceLogger] = getattr(self, "_rust_vice_trace_logger_obj", None)
        if lg is None:
            lg = ViceTraceLogger(path)
            lg.enable()
            self._rust_vice_trace_logger_obj = lg
        return lg

    def _fetch_instr_bytes_for_trace(self, pc: int) -> tuple[int, List[int]]:
        """Opcode and operand bytes at *pc* before executing the instruction (for trace lines)."""
        pc &= 0xFFFF
        opcode = self.memory.read(pc)
        size = OPCODE_SIZES.get(opcode, 1)
        operand_bytes = [self.memory.read((pc + i) & 0xFFFF) for i in range(1, size)]
        return opcode, operand_bytes

    def cpu_step_quantum(
        self,
        udp_debug: Optional['UdpDebugLogger'],
        vice_trace,
        current_cycles: int,
        max_cycles: Optional[int] = None,
    ) -> int:
        """One logical instruction: Rust batch when safe, else :meth:`step`."""
        if udp_debug and udp_debug.enabled:
            return self.step(udp_debug, current_cycles, vice_trace)
        # When vice_trace is enabled, use Rust batch with internal tracing (keeps batch performance)
        # rather than falling back to Python step mode which kills performance.
        if (self.state.pc & 0xFFFF) in self._python_only_step_pcs():
            return self.step(udp_debug, current_cycles, vice_trace)
        # When full IEC + 1541 stepping is active we must run in lockstep with
        # the Python interpreter so every $DD00 write is pushed to IECBus and
        # the drive's VIA1 sees each bit-level edge. The Rust core only
        # snapshots peer IEC state at batch start, so batching here would
        # starve the drive mid-handshake. Disk operations are infrequent
        # enough that the per-instruction overhead is acceptable for the
        # opt-in accurate disk tier path.
        if getattr(self.memory, "iec_disk_full_impl", False):
            return self.step(udp_debug, current_cycles, vice_trace)
        if not self._rust_fast_batch_usable():
            # Only fall back to Python step if Rust is truly unavailable
            return self.step(udp_debug, current_cycles, vice_trace)
        # Determine trace path: prefer --vice-trace FILE over C64PY_RUST_VICE_TRACE env var
        # Note: we use the filename even if not yet enabled - Rust will write directly
        rust_trace_path: Optional[str] = None
        if vice_trace and vice_trace.filename:
            rust_trace_path = vice_trace.filename
        else:
            rust_trace_path = self._rust_vice_trace_path()
        

        try:
            batch_n = int(os.environ.get("C64PY_RUST_BATCH", "64"))
        except ValueError:
            batch_n = 64
        batch_n = max(1, min(batch_n, 10_000))
        if getattr(self, "_monitor_force_single", False):
            batch_n = 1
        # Avoid running a full Rust batch past max_cycles: that can overshoot by dozens of
        # instructions vs Python, shifting IRQ/jiffy/stack (see vic snapshot diffs).
        if max_cycles is not None:
            remaining = max_cycles - int(current_cycles)
            if remaining > 0:
                # Worst-case cycles per opcode on the Rust path (no BA steals); keep a margin.
                _worst = 8
                cap = max(1, remaining // _worst)
                batch_n = min(batch_n, cap)
        # KERNAL wire decode + TcpDrive: Rust snapshots peer CLK/DATA only at the
        # start of each ``run_fast_batch`` call. Listener-ready DATA is applied on
        # the Python ``IECBus`` during CIA2 replay after a batch; a multi-instruction
        # batch can spin on $DD00 polls before that replay. Cap at one instruction
        # per batch while the tap's wire decoder is active (see ``_core.run_fast_batch``).
        if (
            getattr(self.memory, "iec_bus", None) is not None
            and getattr(
                getattr(self.memory, "iec_kernal_tap", None), "_wire_decoder", None
            )
            is not None
        ):
            batch_n = 1

        stops = self._rust_delegate_stop_pcs()
        # Flush Python's buffered trace writes before Rust appends to the same file,
        # otherwise Python's deferred writes end up after Rust's entries.
        if rust_trace_path and vice_trace and vice_trace.file:
            vice_trace.file.flush()
        ins, cyc = self.step_fast_batch(batch_n, stop_pcs=stops, trace_path=rust_trace_path)
        if ins == 0:
            return self.step(udp_debug, current_cycles, vice_trace)
        return cyc

    def _update_cia_timers(self, cycles: int, recompute_irq: bool = True) -> None:
        """Update CIA timers and optionally recompute pending IRQ (defer in hot inner loops)."""
        mem = self.memory
        tA = mem.cia1_timer_a
        tB = mem.cia1_timer_b
        if not recompute_irq and (not tA.running) and (not tB.running):
            return
        # Update Timer A
        if tA.update(cycles):
            if tA.irq_enabled:
                mem.cia1_icr |= 0x01  # Timer A interrupt
                mem.cia1_icr |= 0x80  # IRQ flag
            tA.reset()

        # Update Timer B (can be clocked by Timer A underflow)
        timer_a_underflow = False
        if tA.counter <= 0 and tA.running:
            timer_a_underflow = True

        if tB.input_mode == 2:  # Timer A underflow mode
            if timer_a_underflow:
                if tB.update(1):  # Count by 1
                    mem.cia1_icr |= 0x02  # Timer B interrupt
                    mem.cia1_icr |= 0x80  # IRQ flag
                    tB.reset()
        else:
            if tB.update(cycles):
                mem.cia1_icr |= 0x02  # Timer B interrupt
                mem.cia1_icr |= 0x80  # IRQ flag
                tB.reset()

        if recompute_irq:
            mem.recompute_pending_irq()

    def _handle_cia_interrupt(self) -> None:
        """Handle CIA interrupts directly (bypass KERNAL for stability)"""
        # This is a simplified handler - the real C64 uses KERNAL IRQ handler at $EA31
        # which includes keyboard scanning (SCNKEY). For now, we just update jiffy clock.

        # Check what CIA interrupt occurred
        icr = self.memory.cia1_icr

        if icr & 0x01:  # Timer A interrupt
            # Increment jiffy clock (C64 standard locations $A0-$A2)
            jiffy_low = self.memory.read(0xA0)
            jiffy_mid = self.memory.read(0xA1)
            jiffy_high = self.memory.read(0xA2)

            jiffy = jiffy_low | (jiffy_mid << 8) | (jiffy_high << 16)
            jiffy += 1

            self.memory.write(0xA0, jiffy & 0xFF)
            self.memory.write(0xA1, (jiffy >> 8) & 0xFF)
            self.memory.write(0xA2, (jiffy >> 16) & 0xFF)

        # Clear IRQ state (we're bypassing the real KERNAL handler).
        self.memory.cia1_icr = 0
        self.memory.pending_irq = False

    def _handle_irq(self, udp_debug: Optional['UdpDebugLogger'] = None) -> None:
        """Handle IRQ interrupt - let KERNAL handle everything including cursor blink"""
        if not self.accurate_vic:
            # Coarse/fast mode IRQ entry (historical behavior before BA-accurate IRQ sequencing).
            self.memory.pending_irq = False
            pc = self.state.pc
            self.memory.write(0x100 + self.state.sp, (pc >> 8) & 0xFF)
            self.state.sp = (self.state.sp - 1) & 0xFF
            self.memory.write(0x100 + self.state.sp, pc & 0xFF)
            self.state.sp = (self.state.sp - 1) & 0xFF
            self.memory.write(0x100 + self.state.sp, (self.state.p | 0x20) & ~0x10)
            self.state.sp = (self.state.sp - 1) & 0xFF
            self._set_flag(0x04, True)
            irq_addr = self._read_word(IRQ_VECTOR_HW)
            self.state.pc = irq_addr
            if udp_debug and udp_debug.enabled:
                udp_debug.send('irq', {
                    'irq_addr': irq_addr,
                    'irq_addr_hex': f'${irq_addr:04X}',
                    'old_pc': pc,
                    'old_pc_hex': f'${pc:04X}'
                })
            return

        # Clear pending IRQ flag but NOT cia1_icr - KERNAL reads $DC0D to acknowledge
        self.memory.recompute_pending_irq()

        # Cycle-accurate-ish IRQ entry with BA-aware stalls:
        # 7 cycles total: dummy read, dummy read, push PCH, push PCL, push P, fetch vector low, fetch vector high.
        pc = self.state.pc
        pch = (pc >> 8) & 0xFF
        pcl = pc & 0xFF
        status = (self.state.p | 0x20) & ~0x10  # B clear, bit 5 set

        vector = {"lo": 0, "hi": 0}

        def _irq_cycle(bus_phase: int, do_bus) -> None:
            while True:
                _ba_low, ba_blocks_cpu, _irq_edge = self._vic_tick_one()
                self.state.cycles += 1
                self._update_cia_timers(1, recompute_irq=False)
                if not (ba_blocks_cpu and bus_phase == _BUS_READ):
                    break
            do_bus()

        # 1-2: dummy opcode fetches (we don't care what is read)
        _irq_cycle(_BUS_READ, lambda: self.memory.read(self.state.pc))
        _irq_cycle(_BUS_READ, lambda: self.memory.read(self.state.pc))

        # 3-5: stack pushes (writes are not stalled by BA in our model)
        _irq_cycle(_BUS_WRITE, lambda: self.memory.write(0x100 + self.state.sp, pch))
        self.state.sp = (self.state.sp - 1) & 0xFF
        _irq_cycle(_BUS_WRITE, lambda: self.memory.write(0x100 + self.state.sp, pcl))
        self.state.sp = (self.state.sp - 1) & 0xFF
        _irq_cycle(_BUS_WRITE, lambda: self.memory.write(0x100 + self.state.sp, status))
        self.state.sp = (self.state.sp - 1) & 0xFF

        # Set interrupt disable flag (effective after pushing P on real 6502; close enough here)
        self._set_flag(0x04, True)

        # 6-7: vector fetch
        def _read_vec_lo() -> None:
            vector["lo"] = self.memory.read(IRQ_VECTOR_HW)

        def _read_vec_hi() -> None:
            vector["hi"] = self.memory.read((IRQ_VECTOR_HW + 1) & 0xFFFF)

        _irq_cycle(_BUS_READ, _read_vec_lo)
        _irq_cycle(_BUS_READ, _read_vec_hi)

        irq_addr = vector["lo"] | (vector["hi"] << 8)
        self.state.pc = irq_addr
        self.memory.recompute_pending_irq()

        if udp_debug and udp_debug.enabled:
            udp_debug.send('irq', {
                'irq_addr': irq_addr,
                'irq_addr_hex': f'${irq_addr:04X}',
                'old_pc': pc,
                'old_pc_hex': f'${pc:04X}'
            })

    def _execute_opcode(self, opcode: int) -> int:
        """Execute opcode, return cycles"""
        # Complete 6502 opcode implementation

        # Load/Store instructions
        if opcode == 0xA9:  # LDA imm
            return self._lda_imm()
        elif opcode == 0xA5:  # LDA zp
            return self._lda_zp()
        elif opcode == 0xB5:  # LDA zpx
            return self._lda_zpx()
        elif opcode == 0xAD:  # LDA abs
            return self._lda_abs()
        elif opcode == 0xBD:  # LDA absx
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.x) & 0xFFFF
            self.state.a = self._mr(addr)
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 5 if self._page_crossed(base, self.state.x) else 4
        elif opcode == 0xB9:  # LDA absy
            return self._lda_absy()
        elif opcode == 0xA1:  # LDA indx
            return self._lda_indx()
        elif opcode == 0xB1:  # LDA indy
            return self._lda_indy()
        elif opcode == 0xA2:  # LDX imm
            return self._ldx_imm()
        elif opcode == 0xA6:  # LDX zp
            return self._ldx_zp()
        elif opcode == 0xAE:  # LDX abs
            return self._ldx_abs()
        elif opcode == 0xB6:  # LDX zpy
            zp_addr = (self._mr(self.state.pc + 1) + self.state.y) & 0xFF
            self.state.x = self._mr(zp_addr)
            self._update_flags(self.state.x)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 4
        elif opcode == 0xBE:  # LDX absy
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.y) & 0xFFFF
            self.state.x = self._mr(addr)
            self._update_flags(self.state.x)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 5 if self._page_crossed(base, self.state.y) else 4
        elif opcode == 0xA0:  # LDY imm
            return self._ldy_imm()
        elif opcode == 0xA4:  # LDY zp
            return self._ldy_zp()
        elif opcode == 0xAC:  # LDY abs
            return self._ldy_abs()
        elif opcode == 0xBC:  # LDY abs,X
            return self._ldy_absx()
        elif opcode == 0xB4:  # LDY zp,X (undocumented)
            return self._ldy_zpx()
        elif opcode == 0x85:  # STA zp
            return self._sta_zp()
        elif opcode == 0x95:  # STA zpx
            return self._sta_zpx()
        elif opcode == 0x8D:  # STA abs
            return self._sta_abs()
        elif opcode == 0x9D:  # STA absx
            return self._sta_absx()
        elif opcode == 0x99:  # STA absy
            return self._sta_absy()
        elif opcode == 0x81:  # STA indx
            return self._sta_indx()
        elif opcode == 0x91:  # STA indy
            return self._sta_indy()
        elif opcode == 0x86:  # STX zp
            return self._stx_zp()
        elif opcode == 0x8E:  # STX abs
            return self._stx_abs()
        elif opcode == 0x96:  # STX zp,Y
            return self._stx_zpy()
        elif opcode == 0x84:  # STY zp
            return self._sty_zp()
        elif opcode == 0x8C:  # STY abs
            return self._sty_abs()
        elif opcode == 0x94:  # STY zp,X (undocumented)
            return self._sty_zpx()
        elif opcode == 0x87:  # SAX zp (undocumented - A & X -> memory)
            zp_addr = self._mr(self.state.pc + 1)
            self._mw(zp_addr, self.state.a & self.state.x)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 3
        elif opcode == 0xA3:  # LAX (indirect,X) (undocumented - LDA + TAX)
            zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
            addr = self._mr(zp_addr) | (self._mr((zp_addr + 1) & 0xFF) << 8)
            self.state.a = self._mr(addr)
            self.state.x = self.state.a
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 6
        elif opcode == 0xC7:  # DCP zp (undocumented - DEC then CMP)
            zp_addr = self._mr(self.state.pc + 1)
            value = (self._mr(zp_addr) - 1) & 0xFF
            self._mw(zp_addr, value)
            # CMP part
            result = self.state.a - value
            self._set_flag(0x01, result >= 0)  # Carry
            self._set_flag(0x02, result == 0)  # Zero
            self._set_flag(0x80, (result & 0x80) != 0)  # Negative
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 5

        # Arithmetic
        elif opcode == 0x69:  # ADC imm
            return self._adc_imm()
        elif opcode == 0x61:  # ADC (zp,X)
            return self._adc_indx()
        elif opcode == 0x65:  # ADC zp
            return self._adc_zp()
        elif opcode == 0x75:  # ADC zp,X
            return self._adc_zpx()
        elif opcode == 0x6D:  # ADC abs
            return self._adc_abs()
        elif opcode == 0x79:  # ADC abs,Y
            return self._adc_absy()
        elif opcode == 0x7D:  # ADC abs,X
            return self._adc_absx()
        elif opcode == 0x71:  # ADC (zp),Y
            return self._adc_indy()
        elif opcode == 0xE9:  # SBC imm
            return self._sbc_imm()
        elif opcode == 0xE5:  # SBC zp
            return self._sbc_zp()
        elif opcode == 0xF5:  # SBC zpx
            zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
            value = self._mr(zp_addr)
            carry = 1 if self._get_flag(0x01) else 0
            result = self.state.a - value - (1 - carry)
            self._set_flag(0x01, result >= 0)
            # Set overflow flag
            self._set_flag(0x40, ((self.state.a ^ value) & 0x80) != 0 and ((self.state.a ^ result) & 0x80) != 0)
            self.state.a = result & 0xFF
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 4
        elif opcode == 0xE1:  # SBC indx
            zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
            addr_low = self._mr(zp_addr)
            addr_high = self._mr((zp_addr + 1) & 0xFF)
            addr = addr_low | (addr_high << 8)
            value = self._mr(addr)
            carry = 1 if self._get_flag(0x01) else 0
            result = self.state.a - value - (1 - carry)
            self._set_flag(0x01, result >= 0)
            self._set_flag(0x40, ((self.state.a ^ value) & 0x80) != 0 and ((self.state.a ^ result) & 0x80) != 0)
            self.state.a = result & 0xFF
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 6
        elif opcode == 0xF1:  # SBC indy (SBC ($nn),Y)
            zp_ptr = self._mr(self.state.pc + 1)
            addr_low = self._mr(zp_ptr)
            addr_high = self._mr((zp_ptr + 1) & 0xFF)
            base = addr_low | (addr_high << 8)
            addr = (base + self.state.y) & 0xFFFF
            value = self._mr(addr)
            carry = 1 if self._get_flag(0x01) else 0
            result = self.state.a - value - (1 - carry)
            self._set_flag(0x01, result >= 0)
            self._set_flag(0x40, ((self.state.a ^ value) & 0x80) != 0 and ((self.state.a ^ result) & 0x80) != 0)
            self.state.a = result & 0xFF
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 6 if self._page_crossed(base, self.state.y) else 5
        elif opcode == 0xED:  # SBC abs
            return self._sbc_abs()
        elif opcode == 0xFD:  # SBC absx
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.x) & 0xFFFF
            value = self._mr(addr)
            carry = 1 if self._get_flag(0x01) else 0
            result = self.state.a - value - (1 - carry)
            self._set_flag(0x01, result >= 0)
            self._set_flag(0x40, ((self.state.a ^ value) & 0x80) != 0 and ((self.state.a ^ result) & 0x80) != 0)
            self.state.a = result & 0xFF
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 5 if self._page_crossed(base, self.state.x) else 4
        elif opcode == 0xF9:  # SBC absy
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.y) & 0xFFFF
            value = self._mr(addr)
            carry = 1 if self._get_flag(0x01) else 0
            result = self.state.a - value - (1 - carry)
            self._set_flag(0x01, result >= 0)
            self._set_flag(0x40, ((self.state.a ^ value) & 0x80) != 0 and ((self.state.a ^ result) & 0x80) != 0)
            self.state.a = result & 0xFF
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 5 if self._page_crossed(base, self.state.y) else 4

        # Logic
        elif opcode == 0x29:  # AND imm
            return self._and_imm()
        elif opcode == 0x25:  # AND zp
            return self._and_zp()
        elif opcode == 0x35:  # AND zp,X
            return self._and_zpx()
        elif opcode == 0x2D:  # AND abs
            return self._and_abs()
        elif opcode == 0x3D:  # AND abs,X
            return self._and_absx()
        elif opcode == 0x39:  # AND abs,Y
            return self._and_absy()
        elif opcode == 0x21:  # AND (zp,X)
            return self._and_indx()
        elif opcode == 0x31:  # AND (zp),Y
            return self._and_indy()
        elif opcode == 0x09:  # ORA imm
            return self._ora_imm()
        elif opcode == 0x05:  # ORA zp
            return self._ora_zp()
        elif opcode == 0x15:  # ORA zp,X
            return self._ora_zpx()
        elif opcode == 0x0D:  # ORA abs
            return self._ora_abs()
        elif opcode == 0x1D:  # ORA abs,X
            return self._ora_absx()
        elif opcode == 0x19:  # ORA abs,Y
            return self._ora_absy()
        elif opcode == 0x01:  # ORA (zp,X)
            return self._ora_indx()
        elif opcode == 0x11:  # ORA (zp),Y
            return self._ora_indy()
        elif opcode == 0x49:  # EOR imm
            return self._eor_imm()
        elif opcode == 0x45:  # EOR zp
            return self._eor_zp()
        elif opcode == 0x55:  # EOR zp,X
            return self._eor_zpx()
        elif opcode == 0x4D:  # EOR abs
            return self._eor_abs()
        elif opcode == 0x5D:  # EOR abs,X
            return self._eor_absx()
        elif opcode == 0x59:  # EOR abs,Y
            return self._eor_absy()
        elif opcode == 0x41:  # EOR (zp,X)
            return self._eor_indx()
        elif opcode == 0x51:  # EOR (zp),Y
            return self._eor_indy()

        # Compare
        elif opcode == 0xC9:  # CMP imm
            return self._cmp_imm()
        elif opcode == 0xC5:  # CMP zp
            return self._cmp_zp()
        elif opcode == 0xCD:  # CMP abs
            return self._cmp_abs()
        elif opcode == 0xDD:  # CMP absx
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.x) & 0xFFFF
            value = self._mr(addr)
            result = (self.state.a - value) & 0xFF
            self._set_flag(0x01, self.state.a >= value)
            self._update_flags(result)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 5 if self._page_crossed(base, self.state.x) else 4
        elif opcode == 0xD9:  # CMP absy
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.y) & 0xFFFF
            value = self._mr(addr)
            result = (self.state.a - value) & 0xFF
            self._set_flag(0x01, self.state.a >= value)
            self._update_flags(result)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            # Add 1 cycle if page boundary crossed
            if (base & 0xFF00) != (addr & 0xFF00):
                return 5
            return 4
        elif opcode == 0xE0:  # CPX imm
            return self._cpx_imm()
        elif opcode == 0xE4:  # CPX zp
            return self._cpx_zp()
        elif opcode == 0xEC:  # CPX abs
            return self._cpx_abs()
        elif opcode == 0xC0:  # CPY imm
            return self._cpy_imm()
        elif opcode == 0xC4:  # CPY zp
            return self._cpy_zp()
        elif opcode == 0xCC:  # CPY abs
            return self._cpy_abs()
        elif opcode == 0xC1:  # CMP indx
            zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
            addr = self._mr(zp_addr) | (self._mr((zp_addr + 1) & 0xFF) << 8)
            value = self._mr(addr)
            result = (self.state.a - value) & 0xFF
            self._set_flag(0x01, self.state.a >= value)
            self._update_flags(result)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 6
        elif opcode == 0xD1:  # CMP indy
            zp_addr = self._mr(self.state.pc + 1)
            base = self._mr(zp_addr) | (self._mr((zp_addr + 1) & 0xFF) << 8)
            addr = (base + self.state.y) & 0xFFFF
            value = self._mr(addr)
            result = (self.state.a - value) & 0xFF
            self._set_flag(0x01, self.state.a >= value)
            self._update_flags(result)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 6 if self._page_crossed(base, self.state.y) else 5
        elif opcode == 0xD5:  # CMP zp,X
            zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
            value = self._mr(zp_addr)
            result = (self.state.a - value) & 0xFF
            self._set_flag(0x01, self.state.a >= value)
            self._update_flags(result)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 4

        # Increment/Decrement
        elif opcode == 0xD6:  # DEC zp,X
            zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
            old = self._mr(zp_addr)
            self._rmw_dummy_write_6510(zp_addr, old)
            value = (old - 1) & 0xFF
            self._mw(zp_addr, value)
            self._update_flags(value)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 6
        elif opcode == 0xE6:  # INC zp
            return self._inc_zp()
        elif opcode == 0xF6:  # INC zp,X
            zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
            old = self._mr(zp_addr)
            self._rmw_dummy_write_6510(zp_addr, old)
            value = (old + 1) & 0xFF
            self._mw(zp_addr, value)
            self._update_flags(value)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 6
        elif opcode == 0xEE:  # INC abs
            return self._inc_abs()
        elif opcode == 0xC6:  # DEC zp
            return self._dec_zp()
        elif opcode == 0xCE:  # DEC abs
            return self._dec_abs()
        elif opcode == 0xDE:  # DEC abs,X
            return self._dec_absx()
        elif opcode == 0xE8:  # INX
            return self._inx()
        elif opcode == 0xC8:  # INY
            return self._iny()
        elif opcode == 0xCA:  # DEX
            return self._dex()
        elif opcode == 0x88:  # DEY
            return self._dey()

        # Shifts
        elif opcode == 0x0A:  # ASL acc
            return self._asl_acc()
        elif opcode == 0x06:  # ASL zp
            return self._asl_zp()
        elif opcode == 0x16:  # ASL zp,X
            return self._asl_zpx()
        elif opcode == 0x0E:  # ASL abs
            return self._asl_abs()
        elif opcode == 0x1E:  # ASL abs,X
            return self._asl_absx()
        elif opcode == 0x4A:  # LSR acc
            return self._lsr_acc()
        elif opcode == 0x46:  # LSR zp
            return self._lsr_zp()
        elif opcode == 0x56:  # LSR zp,X
            return self._lsr_zpx()
        elif opcode == 0x4E:  # LSR abs
            return self._lsr_abs()
        elif opcode == 0x5E:  # LSR abs,X
            return self._lsr_absx()
        elif opcode == 0x2A:  # ROL acc
            return self._rol_acc()
        elif opcode == 0x26:  # ROL zp
            return self._rol_zp()
        elif opcode == 0x36:  # ROL zp,X
            return self._rol_zpx()
        elif opcode == 0x2E:  # ROL abs
            return self._rol_abs()
        elif opcode == 0x3E:  # ROL abs,X
            return self._rol_absx()
        elif opcode == 0x6A:  # ROR acc
            return self._ror_acc()
        elif opcode == 0x66:  # ROR zp
            return self._ror_zp()
        elif opcode == 0x76:  # ROR zp,X
            return self._ror_zpx()
        elif opcode == 0x6E:  # ROR abs
            return self._ror_abs()
        elif opcode == 0x7E:  # ROR abs,X
            return self._ror_absx()
        elif opcode == 0xFE:  # INC absx
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.x) & 0xFFFF
            old = self._mr(addr)
            self._rmw_dummy_write_6510(addr, old)
            value = (old + 1) & 0xFF
            self._mw(addr, value)
            self._update_flags(value)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 7

        # Branches
        elif opcode == 0x90:  # BCC
            return self._bcc()
        elif opcode == 0xB0:  # BCS
            return self._bcs()
        elif opcode == 0xF0:  # BEQ
            return self._beq()
        elif opcode == 0xD0:  # BNE
            return self._bne()
        elif opcode == 0x10:  # BPL
            return self._bpl()
        elif opcode == 0x30:  # BMI
            return self._bmi()
        elif opcode == 0x50:  # BVC
            return self._bvc()
        elif opcode == 0x70:  # BVS
            return self._bvs()

        # Jumps and Subroutines
        elif opcode == 0x4C:  # JMP abs
            return self._jmp_abs()
        elif opcode == 0x6C:  # JMP ind
            return self._jmp_ind()
        elif opcode == 0x20:  # JSR abs
            return self._jsr_abs()
        elif opcode == 0x60:  # RTS
            return self._rts()
        elif opcode == 0x40:  # RTI
            return self._rti()

        # Stack
        elif opcode == 0x48:  # PHA
            return self._pha()
        elif opcode == 0x68:  # PLA
            return self._pla()
        elif opcode == 0x08:  # PHP
            return self._php()
        elif opcode == 0x28:  # PLP
            return self._plp()
        elif opcode == 0x7A:  # PLY (undocumented - pull Y from stack)
            self.state.sp = (self.state.sp + 1) & 0xFF
            self.state.y = self._mr(0x100 + self.state.sp)
            self._update_flags(self.state.y)
            self.state.pc = (self.state.pc + 1) & 0xFFFF
            return 4
        elif opcode == 0x7F:  # RRA absx (undocumented - ROR + ADC)
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.x) & 0xFFFF
            value = self._mr(addr)
            carry = 1 if self._get_flag(0x01) else 0
            new_carry = (value & 0x01) != 0
            value = ((value >> 1) | (carry << 7)) & 0xFF
            self._mw(addr, value)
            self._set_flag(0x01, new_carry)
            # ADC part
            carry = 1 if self._get_flag(0x01) else 0
            result = self.state.a + value + carry
            self._set_flag(0x01, result > 0xFF)
            self.state.a = result & 0xFF
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 7
        elif opcode == 0xA7:  # LAX zp (undocumented - LDA + TAX)
            zp_addr = self._mr(self.state.pc + 1)
            self.state.a = self._mr(zp_addr)
            self.state.x = self.state.a
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 3
        elif opcode == 0xAF:  # LAX abs (undocumented - LDA + TAX)
            addr = self._read_word(self.state.pc + 1)
            self.state.a = self._mr(addr)
            self.state.x = self.state.a
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 4
        elif opcode == 0xBF:  # LAX absy (undocumented - LDA + TAX)
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.y) & 0xFFFF
            self.state.a = self._mr(addr)
            self.state.x = self.state.a
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 5 if self._page_crossed(base, self.state.y) else 4
        elif opcode == 0xFF:  # ISC absx (undocumented - increment memory, then subtract with carry)
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.x) & 0xFFFF
            value = (self._mr(addr) + 1) & 0xFF
            self._mw(addr, value)
            # SBC part
            carry = 1 if self._get_flag(0x01) else 0
            result = self.state.a - value - (1 - carry)
            self._set_flag(0x01, result >= 0)
            self._set_flag(0x40, ((self.state.a ^ value) & 0x80) != 0 and ((self.state.a ^ result) & 0x80) != 0)
            self.state.a = result & 0xFF
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 7

        # Transfers
        elif opcode == 0xAA:  # TAX
            return self._tax()
        elif opcode == 0xA8:  # TAY
            return self._tay()
        elif opcode == 0x8A:  # TXA
            return self._txa()
        elif opcode == 0x98:  # TYA
            return self._tya()
        elif opcode == 0xBA:  # TSX
            return self._tsx()
        elif opcode == 0x9A:  # TXS
            self.state.sp = self.state.x
            self.state.pc = (self.state.pc + 1) & 0xFFFF
            return 2

        # Flags
        elif opcode == 0x18:  # CLC
            self._set_flag(0x01, False)
            self.state.pc = (self.state.pc + 1) & 0xFFFF
            return 2
        elif opcode == 0x38:  # SEC
            self._set_flag(0x01, True)
            self.state.pc = (self.state.pc + 1) & 0xFFFF
            return 2
        elif opcode == 0x58:  # CLI
            # Canonical 6502: clearing I takes effect at end of CLI, but the
            # IRQ poll inside CLI still uses the old I=1 value — a pending IRQ
            # is dispatched only AFTER the following instruction completes.
            self.state.pre_i_flag = self.state.p & 0x04
            self._set_flag(0x04, False)
            self.state.cli_sei_delay = True
            self.state.pc = (self.state.pc + 1) & 0xFFFF
            return 2
        elif opcode == 0x78:  # SEI
            # Canonical 6502: setting I takes effect at end of SEI. An IRQ
            # pending when SEI began is still serviced on the next
            # instruction boundary (a.k.a. the "SEI delay" behavior).
            self.state.pre_i_flag = self.state.p & 0x04
            self._set_flag(0x04, True)
            self.state.cli_sei_delay = True
            self.state.pc = (self.state.pc + 1) & 0xFFFF
            return 2
        elif opcode == 0xD8:  # CLD
            self._set_flag(0x08, False)
            self.state.pc = (self.state.pc + 1) & 0xFFFF
            return 2
        elif opcode == 0xF8:  # SED
            self._set_flag(0x08, True)
            self.state.pc = (self.state.pc + 1) & 0xFFFF
            return 2
        elif opcode == 0xB8:  # CLV
            self._set_flag(0x40, False)
            self.state.pc = (self.state.pc + 1) & 0xFFFF
            return 2

        # Other
        elif opcode == 0x00:  # BRK
            return self._brk()
        elif opcode == 0x02:  # KIL (undocumented - kill processor, halts CPU)
            # KIL halts the processor - set stopped flag
            self.state.stopped = True
            self.state.pc = (self.state.pc + 1) & 0xFFFF
            return 0
        elif opcode == 0xEA:  # NOP
            self.state.pc = (self.state.pc + 1) & 0xFFFF
            return 2
        # NOP variants (documented and undocumented)
        elif opcode in [0x80, 0x82, 0x89, 0xC2, 0xE2]:  # NOP #imm (same bus as LDA # — discard result)
            self._mr((self.state.pc + 1) & 0xFFFF)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 2
        elif opcode in [0x04, 0x44, 0x64]:  # NOP zp (undocumented - consume 1 byte operand)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 3
        elif opcode in [0x14, 0x1C, 0x3C, 0x5C, 0x7C, 0xDC, 0xFC]:  # NOP absx (undocumented - consume 2 byte operand)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 4
        elif opcode == 0x24:  # BIT zp
            return self._bit_zp()
        elif opcode == 0x2C:  # BIT abs
            return self._bit_abs()
        # Handle common undocumented opcodes as NOPs
        elif opcode in [0x02, 0x03, 0x07, 0x0B, 0x0F, 0x12, 0x13, 0x17, 0x1A, 0x1B, 0x1C, 0x1F, 0x22, 0x27, 0x2F, 0x32, 0x33, 0x34, 0x37, 0x3A, 0x3B, 0x3C, 0x3F, 0x42, 0x43, 0x47, 0x4B, 0x4F, 0x52, 0x53, 0x54, 0x57, 0x5A, 0x5B, 0x5C, 0x5F, 0x62, 0x63, 0x64, 0x67, 0x6B, 0x6F, 0x72, 0x73, 0x74, 0x77, 0x7A, 0x7B, 0x7C, 0x7F, 0x80, 0x82, 0x83, 0x87, 0x8B, 0x8F, 0x92, 0x93, 0x97, 0x9B, 0x9C, 0x9E, 0x9F, 0xA3, 0xA7, 0xAB, 0xAF, 0xB2, 0xB3, 0xB7, 0xBB, 0xBF, 0xC2, 0xC3, 0xC7, 0xCB, 0xCF, 0xD2, 0xD3, 0xD4, 0xD7, 0xDA, 0xDB, 0xDC, 0xDF, 0xE2, 0xE3, 0xE7, 0xEB, 0xEF, 0xF2, 0xF3, 0xF4, 0xF7, 0xFA, 0xFB, 0xFC, 0xFF]:
            # Undocumented opcode - treat as multi-byte NOP for compatibility
            # Most undocumented opcodes are 2-3 bytes
            self.state.pc = (self.state.pc + 2) & 0xFFFF  # Assume 2-byte for safety
            return 3
        else:
            # Unknown opcode - halt CPU (like VICE does)
            halt_msg = f"🛑 CPU halted: Unknown opcode ${opcode:02X} at PC=${self.state.pc:04X}"
            # Check location
            if 0xA000 <= self.state.pc <= 0xBFFF:
                halt_msg += " (BASIC ROM)"
            elif 0xE000 <= self.state.pc <= 0xFFFF:
                halt_msg += " (KERNAL ROM)"
            elif 0xFF5B <= self.state.pc <= 0xFFFF:
                halt_msg += " (CINT/KERNAL execution)"

            # Send to interface if available
            if self.interface:
                self.interface.add_debug_log(halt_msg)
            else:
                print(halt_msg)  # Fallback to stdout if no interface

            self.state.stopped = True
            return 0

    def _brk(self) -> int:
        """BRK instruction"""
        # Push PC+2 and P onto stack
        pc_high = (self.state.pc + 2) >> 8
        pc_low = (self.state.pc + 2) & 0xFF
        self._mw(0x100 + self.state.sp, pc_high)
        self.state.sp = (self.state.sp - 1) & 0xFF
        self._mw(0x100 + self.state.sp, pc_low)
        self.state.sp = (self.state.sp - 1) & 0xFF
        self._mw(0x100 + self.state.sp, (self.state.p | 0x30) & 0xFF)  # B+bit5 set on stack (BRK)
        self.state.sp = (self.state.sp - 1) & 0xFF
        self._set_flag(0x04, True)  # Set I flag
        self.state.pc = self._read_word(0xFFFE)  # IRQ vector
        return 7

    def _jmp_abs(self) -> int:
        """JMP absolute"""
        addr = self._read_word(self.state.pc + 1)
        self.state.pc = addr
        return 3

    def _jsr_abs(self) -> int:
        """JSR absolute"""
        addr = self._read_word(self.state.pc + 1)
        # Push return address (PC + 2) onto stack (address of next instruction - 1)
        return_addr = (self.state.pc + 2) & 0xFFFF
        pc_high = return_addr >> 8
        pc_low = return_addr & 0xFF
        self._mw(0x100 + self.state.sp, pc_high)
        self.state.sp = (self.state.sp - 1) & 0xFF
        self._mw(0x100 + self.state.sp, pc_low)
        self.state.sp = (self.state.sp - 1) & 0xFF
        self.state.pc = addr
        return 6

    def _rts(self) -> int:
        """RTS"""
        self.state.sp = (self.state.sp + 1) & 0xFF
        pc_low = self._mr(0x100 + self.state.sp)
        self.state.sp = (self.state.sp + 1) & 0xFF
        pc_high = self._mr(0x100 + self.state.sp)
        ret = ((pc_high << 8) | pc_low)
        self.state.pc = (ret + 1) & 0xFFFF
        return 6

    def _lda_imm(self) -> int:
        """LDA immediate"""
        self.state.a = self._mr(self.state.pc + 1)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _lda_zp(self) -> int:
        """LDA zero page"""
        zp_addr = self._mr(self.state.pc + 1)
        self.state.a = self._mr(zp_addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _lda_abs(self) -> int:
        """LDA absolute"""
        addr = self._read_word(self.state.pc + 1)
        self.state.a = self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _sta_zp(self) -> int:
        """STA zero page"""
        zp_addr = self._mr(self.state.pc + 1)
        self._mw(zp_addr, self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _sta_abs(self) -> int:
        """STA absolute"""
        addr = self._read_word(self.state.pc + 1)
        self._mw(addr, self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    # Additional opcode implementations (simplified - add more as needed)
    def _lda_zpx(self) -> int:
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        self.state.a = self._mr(zp_addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 4

    def _lda_absx(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.x) & 0xFFFF
        self.state.a = self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 5 if self._page_crossed(base, self.state.x) else 4

    def _lda_absy(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.y) & 0xFFFF
        self.state.a = self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 5 if self._page_crossed(base, self.state.y) else 4

    def _lda_indx(self) -> int:
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        addr = self._mr(zp_addr) | (self._mr((zp_addr + 1) & 0xFF) << 8)
        self.state.a = self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6

    def _lda_indy(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        base = self._mr(zp_addr) | (self._mr((zp_addr + 1) & 0xFF) << 8)
        addr = (base + self.state.y) & 0xFFFF
        self.state.a = self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6 if self._page_crossed(base, self.state.y) else 5

    def _ldx_imm(self) -> int:
        self.state.x = self._mr(self.state.pc + 1)
        self._update_flags(self.state.x)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _ldx_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        self.state.x = self._mr(zp_addr)
        self._update_flags(self.state.x)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _ldx_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        self.state.x = self._mr(addr)
        self._update_flags(self.state.x)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _ldy_imm(self) -> int:
        self.state.y = self._mr(self.state.pc + 1)
        self._update_flags(self.state.y)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _ldy_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        self.state.y = self._mr(zp_addr)
        self._update_flags(self.state.y)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _ldy_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        self.state.y = self._mr(addr)
        self._update_flags(self.state.y)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _ldy_absx(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.x) & 0xFFFF
        self.state.y = self._mr(addr)
        self._update_flags(self.state.y)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 5 if self._page_crossed(base, self.state.x) else 4

    def _ldy_zpx(self) -> int:
        """LDY zero page,X (undocumented opcode $B4)"""
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        self.state.y = self._mr(zp_addr)
        self._update_flags(self.state.y)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 4

    def _sta_zpx(self) -> int:
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        self._mw(zp_addr, self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 4

    def _sta_absx(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.x) & 0xFFFF
        self._mw(addr, self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 5

    def _sta_absy(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.y) & 0xFFFF
        self._mw(addr, self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 5

    def _sta_indx(self) -> int:
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        addr = self._mr(zp_addr) | (self._mr((zp_addr + 1) & 0xFF) << 8)
        self._mw(addr, self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6

    def _sta_indy(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        base = self._mr(zp_addr) | (self._mr((zp_addr + 1) & 0xFF) << 8)
        addr = (base + self.state.y) & 0xFFFF
        self._mw(addr, self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6

    def _stx_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        self._mw(zp_addr, self.state.x)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _stx_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        self._mw(addr, self.state.x)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _stx_zpy(self) -> int:
        """STX zero page,Y — opcode $96 (documented)."""
        zp_addr = (self._mr(self.state.pc + 1) + self.state.y) & 0xFF
        self._mw(zp_addr, self.state.x)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 4

    def _sty_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        self._mw(zp_addr, self.state.y)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _sty_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        self._mw(addr, self.state.y)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _sty_zpx(self) -> int:
        """STY zero page,X (undocumented opcode $94)"""
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        self._mw(zp_addr, self.state.y)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 4

    # Arithmetic operations (simplified)
    def _adc_imm(self) -> int:
        value = self._mr(self.state.pc + 1)
        old_a = self.state.a
        carry = 1 if self._get_flag(0x01) else 0
        result = old_a + value + carry
        self._adc_finish(old_a, value, result)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _adc_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        value = self._mr(zp_addr)
        old_a = self.state.a
        carry = 1 if self._get_flag(0x01) else 0
        result = old_a + value + carry
        self._adc_finish(old_a, value, result)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _adc_zpx(self) -> int:
        """ADC zero page,X — opcode $75 (documented)."""
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        value = self._mr(zp_addr)
        old_a = self.state.a
        carry = 1 if self._get_flag(0x01) else 0
        result = old_a + value + carry
        self._adc_finish(old_a, value, result)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 4

    def _adc_indx(self) -> int:
        """ADC ($zp,X) — indexed indirect"""
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        addr_low = self._mr(zp_addr)
        addr_high = self._mr((zp_addr + 1) & 0xFF)
        addr = addr_low | (addr_high << 8)
        value = self._mr(addr)
        old_a = self.state.a
        carry = 1 if self._get_flag(0x01) else 0
        result = old_a + value + carry
        self._adc_finish(old_a, value, result)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6

    def _adc_indy(self) -> int:
        """ADC ($zp),Y — indirect indexed"""
        zp_ptr = self._mr(self.state.pc + 1)
        addr_low = self._mr(zp_ptr)
        addr_high = self._mr((zp_ptr + 1) & 0xFF)
        base = addr_low | (addr_high << 8)
        addr = (base + self.state.y) & 0xFFFF
        value = self._mr(addr)
        old_a = self.state.a
        carry = 1 if self._get_flag(0x01) else 0
        result = old_a + value + carry
        self._adc_finish(old_a, value, result)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6 if self._page_crossed(base, self.state.y) else 5

    def _adc_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self._mr(addr)
        old_a = self.state.a
        carry = 1 if self._get_flag(0x01) else 0
        result = old_a + value + carry
        self._adc_finish(old_a, value, result)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _adc_absx(self) -> int:
        """ADC (Add with Carry) absolute,X"""
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.x) & 0xFFFF
        value = self._mr(addr)
        old_a = self.state.a
        carry = 1 if self._get_flag(0x01) else 0
        result = old_a + value + carry
        self._adc_finish(old_a, value, result)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 5 if self._page_crossed(base, self.state.x) else 4

    def _adc_absy(self) -> int:
        """ADC (Add with Carry) absolute,Y"""
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.y) & 0xFFFF
        value = self._mr(addr)
        old_a = self.state.a
        carry = 1 if self._get_flag(0x01) else 0
        result = old_a + value + carry
        self._adc_finish(old_a, value, result)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 5 if self._page_crossed(base, self.state.y) else 4

    def _sbc_imm(self) -> int:
        value = self._mr(self.state.pc + 1)
        carry = 1 if self._get_flag(0x01) else 0
        result = self.state.a - value - (1 - carry)
        self._set_flag(0x01, result >= 0)
        self._set_flag(0x40, ((self.state.a ^ value) & 0x80) != 0 and ((self.state.a ^ result) & 0x80) != 0)
        self.state.a = result & 0xFF
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _sbc_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        value = self._mr(zp_addr)
        carry = 1 if self._get_flag(0x01) else 0
        result = self.state.a - value - (1 - carry)
        self._set_flag(0x01, result >= 0)
        # Set overflow flag
        self._set_flag(0x40, ((self.state.a ^ value) & 0x80) != 0 and ((self.state.a ^ result) & 0x80) != 0)
        self.state.a = result & 0xFF
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _sbc_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self._mr(addr)
        carry = 1 if self._get_flag(0x01) else 0
        result = self.state.a - value - (1 - carry)
        self._set_flag(0x01, result >= 0)
        self._set_flag(0x40, ((self.state.a ^ value) & 0x80) != 0 and ((self.state.a ^ result) & 0x80) != 0)
        self.state.a = result & 0xFF
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    # Logic operations
    def _and_imm(self) -> int:
        self.state.a &= self._mr(self.state.pc + 1)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _and_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        self.state.a &= self._mr(zp_addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _and_zpx(self) -> int:
        """AND zero page,X — opcode $35 (documented)."""
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        self.state.a &= self._mr(zp_addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 4

    def _and_indx(self) -> int:
        """AND ($zp,X) — opcode $21 (documented, indexed indirect)."""
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        addr_low = self._mr(zp_addr)
        addr_high = self._mr((zp_addr + 1) & 0xFF)
        addr = addr_low | (addr_high << 8)
        self.state.a &= self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6

    def _and_indy(self) -> int:
        """AND ($zp),Y — opcode $31 (documented, indirect indexed, +1 if page cross)."""
        zp_ptr = self._mr(self.state.pc + 1)
        addr_low = self._mr(zp_ptr)
        addr_high = self._mr((zp_ptr + 1) & 0xFF)
        base = addr_low | (addr_high << 8)
        addr = (base + self.state.y) & 0xFFFF
        self.state.a &= self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6 if self._page_crossed(base, self.state.y) else 5

    def _and_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        self.state.a &= self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _and_absx(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.x) & 0xFFFF
        self.state.a &= self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 5 if self._page_crossed(base, self.state.x) else 4

    def _and_absy(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.y) & 0xFFFF
        self.state.a &= self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 5 if self._page_crossed(base, self.state.y) else 4

    def _ora_imm(self) -> int:
        self.state.a |= self._mr(self.state.pc + 1)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _ora_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        self.state.a |= self._mr(zp_addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _ora_zpx(self) -> int:
        """ORA zero page,X — opcode $15 (documented)."""
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        self.state.a |= self._mr(zp_addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 4

    def _ora_indx(self) -> int:
        """ORA ($zp,X) — opcode $01 (documented, indexed indirect)."""
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        addr_low = self._mr(zp_addr)
        addr_high = self._mr((zp_addr + 1) & 0xFF)
        addr = addr_low | (addr_high << 8)
        self.state.a |= self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6

    def _ora_indy(self) -> int:
        """ORA ($zp),Y — opcode $11 (documented, indirect indexed, +1 if page cross)."""
        zp_ptr = self._mr(self.state.pc + 1)
        addr_low = self._mr(zp_ptr)
        addr_high = self._mr((zp_ptr + 1) & 0xFF)
        base = addr_low | (addr_high << 8)
        addr = (base + self.state.y) & 0xFFFF
        self.state.a |= self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6 if self._page_crossed(base, self.state.y) else 5

    def _ora_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        self.state.a |= self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _ora_absy(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.y) & 0xFFFF
        self.state.a |= self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 5 if self._page_crossed(base, self.state.y) else 4

    def _ora_absx(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.x) & 0xFFFF
        self.state.a |= self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 5 if self._page_crossed(base, self.state.x) else 4

    def _eor_imm(self) -> int:
        self.state.a ^= self._mr(self.state.pc + 1)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _eor_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        self.state.a ^= self._mr(zp_addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _eor_zpx(self) -> int:
        """EOR zero page,X — opcode $55 (documented)."""
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        self.state.a ^= self._mr(zp_addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 4

    def _eor_indx(self) -> int:
        """EOR ($zp,X) — opcode $41 (documented, indexed indirect)."""
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        addr_low = self._mr(zp_addr)
        addr_high = self._mr((zp_addr + 1) & 0xFF)
        addr = addr_low | (addr_high << 8)
        self.state.a ^= self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6

    def _eor_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        self.state.a ^= self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _eor_absx(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.x) & 0xFFFF
        self.state.a ^= self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 5 if self._page_crossed(base, self.state.x) else 4

    def _eor_absy(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.y) & 0xFFFF
        self.state.a ^= self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 5 if self._page_crossed(base, self.state.y) else 4

    def _eor_indy(self) -> int:
        zp_ptr = self._mr(self.state.pc + 1)
        addr_low = self._mr(zp_ptr)
        addr_high = self._mr((zp_ptr + 1) & 0xFF)
        base = addr_low | (addr_high << 8)
        addr = (base + self.state.y) & 0xFFFF
        self.state.a ^= self._mr(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6 if self._page_crossed(base, self.state.y) else 5

    # Compare operations
    def _cmp_imm(self) -> int:
        value = self._mr(self.state.pc + 1)
        result = (self.state.a - value) & 0xFF
        self._set_flag(0x01, self.state.a >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _cmp_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        value = self._mr(zp_addr)
        result = (self.state.a - value) & 0xFF
        self._set_flag(0x01, self.state.a >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _cmp_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self._mr(addr)
        result = (self.state.a - value) & 0xFF
        self._set_flag(0x01, self.state.a >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _cpx_imm(self) -> int:
        value = self._mr(self.state.pc + 1)
        result = (self.state.x - value) & 0xFF
        self._set_flag(0x01, self.state.x >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _cpx_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        value = self._mr(zp_addr)
        result = (self.state.x - value) & 0xFF
        self._set_flag(0x01, self.state.x >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _cpx_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self._mr(addr)
        result = (self.state.x - value) & 0xFF
        self._set_flag(0x01, self.state.x >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _cpy_imm(self) -> int:
        value = self._mr(self.state.pc + 1)
        result = (self.state.y - value) & 0xFF
        self._set_flag(0x01, self.state.y >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _cpy_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        value = self._mr(zp_addr)
        result = (self.state.y - value) & 0xFF
        self._set_flag(0x01, self.state.y >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _cpy_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self._mr(addr)
        result = (self.state.y - value) & 0xFF
        self._set_flag(0x01, self.state.y >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    # Increment/Decrement
    def _rmw_dummy_write_6510(self, addr: int, read_value: int) -> None:
        """6502 RMW stores the read byte once before the final write.

        On the 6510, $00 (DDR) and $01 (processor port latch) are sensitive: the
        dummy write updates the latch like a real store, affecting banking before
        the final value is written (matches VICE / hardware; loaders use INC/DEC $01).
        """
        addr &= 0xFFFF
        if addr <= 0x0001:
            self._mw(addr, read_value & 0xFF)

    def _inc_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1) & 0xFF
        old = self._mr(zp_addr)
        self._rmw_dummy_write_6510(zp_addr, old)
        value = (old + 1) & 0xFF
        self._mw(zp_addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 5

    def _inc_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        old = self._mr(addr)
        self._rmw_dummy_write_6510(addr, old)
        value = (old + 1) & 0xFF
        self._mw(addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 6

    def _dec_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1) & 0xFF
        old = self._mr(zp_addr)
        self._rmw_dummy_write_6510(zp_addr, old)
        value = (old - 1) & 0xFF
        self._mw(zp_addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 5

    def _dec_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        old = self._mr(addr)
        self._rmw_dummy_write_6510(addr, old)
        value = (old - 1) & 0xFF
        self._mw(addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 6

    def _dec_absx(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.x) & 0xFFFF
        old = self._mr(addr)
        self._rmw_dummy_write_6510(addr, old)
        value = (old - 1) & 0xFF
        self._mw(addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 7  # same as INC abs,X; page-cross penalty not modeled

    def _inx(self) -> int:
        self.state.x = (self.state.x + 1) & 0xFF
        self._update_flags(self.state.x)
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 2

    def _iny(self) -> int:
        self.state.y = (self.state.y + 1) & 0xFF
        self._update_flags(self.state.y)
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 2

    def _dex(self) -> int:
        self.state.x = (self.state.x - 1) & 0xFF
        self._update_flags(self.state.x)
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 2

    def _dey(self) -> int:
        self.state.y = (self.state.y - 1) & 0xFF
        self._update_flags(self.state.y)
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 2

    # Shifts
    def _asl_acc(self) -> int:
        self._set_flag(0x01, (self.state.a & 0x80) != 0)
        self.state.a = (self.state.a << 1) & 0xFF
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 2

    def _asl_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        value = self._mr(zp_addr)
        self._set_flag(0x01, (value & 0x80) != 0)
        value = (value << 1) & 0xFF
        self._mw(zp_addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 5

    def _asl_zpx(self) -> int:
        """ASL (Arithmetic Shift Left) zero-page,X"""
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        value = self._mr(zp_addr)
        self._set_flag(0x01, (value & 0x80) != 0)  # Carry = bit 7
        value = (value << 1) & 0xFF
        self._mw(zp_addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6

    def _asl_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self._mr(addr)
        self._set_flag(0x01, (value & 0x80) != 0)
        value = (value << 1) & 0xFF
        self._mw(addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 6

    def _asl_absx(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.x) & 0xFFFF
        old = self._mr(addr)
        self._rmw_dummy_write_6510(addr, old)
        self._set_flag(0x01, (old & 0x80) != 0)
        value = (old << 1) & 0xFF
        self._mw(addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 7

    def _lsr_acc(self) -> int:
        self._set_flag(0x01, (self.state.a & 0x01) != 0)
        self.state.a = (self.state.a >> 1) & 0xFF
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 2

    def _lsr_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        value = self._mr(zp_addr)
        self._set_flag(0x01, (value & 0x01) != 0)
        value = (value >> 1) & 0xFF
        self._mw(zp_addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 5

    def _lsr_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self._mr(addr)
        self._set_flag(0x01, (value & 0x01) != 0)
        value = (value >> 1) & 0xFF
        self._mw(addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 6

    def _lsr_absx(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.x) & 0xFFFF
        old = self._mr(addr)
        self._rmw_dummy_write_6510(addr, old)
        self._set_flag(0x01, (old & 0x01) != 0)
        value = (old >> 1) & 0xFF
        self._mw(addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 7

    def _lsr_zpx(self) -> int:
        """LSR (Logical Shift Right) zero-page,X"""
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        value = self._mr(zp_addr)
        self._set_flag(0x01, (value & 0x01) != 0)  # Carry = bit 0
        value = (value >> 1) & 0xFF
        self._mw(zp_addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6

    def _rol_acc(self) -> int:
        carry = 1 if self._get_flag(0x01) else 0
        new_carry = (self.state.a & 0x80) != 0
        self.state.a = ((self.state.a << 1) | carry) & 0xFF
        self._set_flag(0x01, new_carry)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 2

    def _rol_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        value = self._mr(zp_addr)
        carry = 1 if self._get_flag(0x01) else 0
        new_carry = (value & 0x80) != 0
        value = ((value << 1) | carry) & 0xFF
        self._mw(zp_addr, value)
        self._set_flag(0x01, new_carry)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 5

    def _rol_zpx(self) -> int:
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        value = self._mr(zp_addr)
        carry = 1 if self._get_flag(0x01) else 0
        new_carry = (value & 0x80) != 0
        value = ((value << 1) | carry) & 0xFF
        self._mw(zp_addr, value)
        self._set_flag(0x01, new_carry)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6

    def _rol_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self._mr(addr)
        carry = 1 if self._get_flag(0x01) else 0
        new_carry = (value & 0x80) != 0
        value = ((value << 1) | carry) & 0xFF
        self._mw(addr, value)
        self._set_flag(0x01, new_carry)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 6

    def _rol_absx(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.x) & 0xFFFF
        old = self._mr(addr)
        self._rmw_dummy_write_6510(addr, old)
        carry = 1 if self._get_flag(0x01) else 0
        new_carry = (old & 0x80) != 0
        value = ((old << 1) | carry) & 0xFF
        self._mw(addr, value)
        self._set_flag(0x01, new_carry)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 7

    def _ror_acc(self) -> int:
        carry = 1 if self._get_flag(0x01) else 0
        new_carry = (self.state.a & 0x01) != 0
        self.state.a = ((self.state.a >> 1) | (carry << 7)) & 0xFF
        self._set_flag(0x01, new_carry)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 2

    def _ror_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        value = self._mr(zp_addr)
        carry = 1 if self._get_flag(0x01) else 0
        new_carry = (value & 0x01) != 0
        value = ((value >> 1) | (carry << 7)) & 0xFF
        self._mw(zp_addr, value)
        self._set_flag(0x01, new_carry)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 5

    def _ror_zpx(self) -> int:
        zp_addr = (self._mr(self.state.pc + 1) + self.state.x) & 0xFF
        value = self._mr(zp_addr)
        carry = 1 if self._get_flag(0x01) else 0
        new_carry = (value & 0x01) != 0
        value = ((value >> 1) | (carry << 7)) & 0xFF
        self._mw(zp_addr, value)
        self._set_flag(0x01, new_carry)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6

    def _ror_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self._mr(addr)
        carry = 1 if self._get_flag(0x01) else 0
        new_carry = (value & 0x01) != 0
        value = ((value >> 1) | (carry << 7)) & 0xFF
        self._mw(addr, value)
        self._set_flag(0x01, new_carry)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 6

    def _ror_absx(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.x) & 0xFFFF
        old = self._mr(addr)
        self._rmw_dummy_write_6510(addr, old)
        carry = 1 if self._get_flag(0x01) else 0
        new_carry = (old & 0x01) != 0
        value = ((old >> 1) | (carry << 7)) & 0xFF
        self._mw(addr, value)
        self._set_flag(0x01, new_carry)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 7

    # Branches
    def _bcc(self) -> int:
        return self._branch(not self._get_flag(0x01))

    def _bcs(self) -> int:
        return self._branch(self._get_flag(0x01))

    def _beq(self) -> int:
        return self._branch(self._get_flag(0x02))

    def _bne(self) -> int:
        return self._branch(not self._get_flag(0x02))

    def _bpl(self) -> int:
        return self._branch(not self._get_flag(0x80))

    def _bmi(self) -> int:
        return self._branch(self._get_flag(0x80))

    def _bvc(self) -> int:
        return self._branch(not self._get_flag(0x40))

    def _bvs(self) -> int:
        return self._branch(self._get_flag(0x40))

    def _branch(self, condition: bool) -> int:
        """Branch if condition is true"""
        offset = self._mr(self.state.pc + 1)
        if offset & 0x80:
            offset = offset - 256
        if condition:
            old_pc = self.state.pc + 2
            new_pc = (old_pc + offset) & 0xFFFF
            self.state.pc = new_pc
            # +1 for branch taken, +1 more if page crossed
            return 4 if (old_pc & 0xFF00) != (new_pc & 0xFF00) else 3
        else:
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 2

    # Jumps
    def _jmp_ind(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        # Handle page boundary bug
        if (addr & 0xFF) == 0xFF:
            low = self._mr(addr)
            high = self._mr(addr & 0xFF00)
        else:
            low = self._mr(addr)
            high = self._mr(addr + 1)
        self.state.pc = low | (high << 8)
        return 5

    # Stack operations
    def _pha(self) -> int:
        self._mw(0x100 + self.state.sp, self.state.a)
        self.state.sp = (self.state.sp - 1) & 0xFF
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 3

    def _pla(self) -> int:
        self.state.sp = (self.state.sp + 1) & 0xFF
        self.state.a = self._mr(0x100 + self.state.sp)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 4

    def _php(self) -> int:
        # NMOS 6502: pushed status has bits 4 (B) and 5 always 1 (see visual6502 / datasheets).
        self._mw(0x100 + self.state.sp, (self.state.p | 0x30) & 0xFF)
        self.state.sp = (self.state.sp - 1) & 0xFF
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 3

    def _plp(self) -> int:
        # Like CLI/SEI, PLP's I-flag update has a one-instruction IRQ-poll
        # delay: the poll that runs at the end of PLP uses the I value
        # EFFECTIVE BEFORE the pull, so a pending IRQ unmasked by PLP is
        # dispatched only after the following instruction.
        self.state.pre_i_flag = self.state.p & 0x04
        self.state.sp = (self.state.sp + 1) & 0xFF
        # Full P from stack (incl. B and bit 5); matches VICE trace NV-BDIZC after PLP/RTI.
        self.state.p = self._mr(0x100 + self.state.sp) & 0xFF
        self.state.cli_sei_delay = True
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 4

    # Transfers
    def _tax(self) -> int:
        self.state.x = self.state.a
        self._update_flags(self.state.x)
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 2

    def _tay(self) -> int:
        self.state.y = self.state.a
        self._update_flags(self.state.y)
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 2

    def _txa(self) -> int:
        self.state.a = self.state.x
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 2

    def _tya(self) -> int:
        self.state.a = self.state.y
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 2

    def _tsx(self) -> int:
        self.state.x = self.state.sp
        self._update_flags(self.state.x)
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 2

    def _txs(self) -> int:
        self.state.sp = self.state.x
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 2

    # Other
    def _rti(self) -> int:
        self.state.sp = (self.state.sp + 1) & 0xFF
        self.state.p = self._mr(0x100 + self.state.sp) & 0xFF
        self.state.sp = (self.state.sp + 1) & 0xFF
        pc_low = self._mr(0x100 + self.state.sp)
        self.state.sp = (self.state.sp + 1) & 0xFF
        pc_high = self._mr(0x100 + self.state.sp)
        self.state.pc = (pc_low | (pc_high << 8)) & 0xFFFF
        return 6

    def _bit_zp(self) -> int:
        zp_addr = self._mr(self.state.pc + 1)
        value = self._mr(zp_addr)
        self._set_flag(0x40, (value & 0x40) != 0)  # V flag
        self._set_flag(0x80, (value & 0x80) != 0)  # N flag
        self._set_flag(0x02, (self.state.a & value) == 0)  # Z flag
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _bit_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self._mr(addr)
        self._set_flag(0x40, (value & 0x40) != 0)  # V flag
        self._set_flag(0x80, (value & 0x80) != 0)  # N flag
        self._set_flag(0x02, (self.state.a & value) == 0)  # Z flag
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4
