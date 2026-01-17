"""
6502 CPU Emulator
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from .constants import (
    SCREEN_MEM,
    COLOR_MEM,
    IRQ_VECTOR,
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

if TYPE_CHECKING:
    from .debug import UdpDebugLogger

class CPU6502:
    """6502 CPU emulator"""

    def __init__(self, memory: MemoryMap, interface=None):
        self.memory = memory
        self.interface = interface
        self.state = CPUState()
        # PC will be set from reset vector after ROMs are loaded
        # Don't read it here as ROMs might not be loaded yet
        self.state.pc = 0x0000

    def _read_word(self, addr: int) -> int:
        """Read 16-bit word (little-endian)"""
        low = self.memory.read(addr)
        high = self.memory.read((addr + 1) & 0xFFFF)
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

    def _advance_time(self, cycles: int, udp_debug: Optional['UdpDebugLogger'] = None) -> None:
        """Advance timers/video/IRQs even if CPU is 'blocked'."""
        self.state.cycles += cycles

        # Update CIA timers
        self._update_cia_timers(cycles)

        # Update VIC-II raster line (simulate video timing)
        raster_max = 312 if self.memory.video_standard == "pal" else 263
        # We keep the existing "1 step per instruction" behavior for consistency.
        self.memory.raster_line = (self.memory.raster_line + 1) % raster_max

        # Check for pending IRQ (only if interrupts are enabled)
        if self.memory.pending_irq and not self._get_flag(0x04):  # I flag clear
            if self.memory.cia1_icr & 0x80:  # CIA interrupt pending
                self._handle_cia_interrupt()

    def step(self, udp_debug: Optional['UdpDebugLogger'] = None, current_cycles: int = 0) -> int:
        """Execute one instruction, return cycles"""
        self.current_cycles = current_cycles
        if self.state.stopped:
            # If CPU is stopped (KIL), don't execute anything
            # Return 1 cycle to prevent infinite loops in the run loop
            return 1

        pc = self.state.pc
        opcode = self.memory.read(pc)

        # Log instruction execution if UDP debug is enabled
        # Note: cycles haven't been incremented yet, so we log the current cycle count
        # The actual cycles for this instruction will be returned and added later
        if udp_debug and udp_debug.enabled:
            # Sample logging to avoid queue overflow (log every 100 cycles or important events)
            should_log = (self.state.cycles % 100 == 0) or (opcode == 0x00)  # Log BRK instructions

            should_log = (self.state.cycles > 2020000)

            if should_log:
                # Minimal data to reduce JSON/serialization overhead
                udp_debug.send('cpu_step', {
                    'pc': pc,
                    'opcode': opcode,
                    'cycles': self.state.cycles
                })


        # Special handling for CINT - simulate PAL/NTSC detection
        if pc == 0xFF5B:  # Start of CINT
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
                self.memory.poke_vic(0x18, 0x10)  # screen/charset layout
            except Exception:
                # If VIC helpers aren't available for some reason, fall back to I/O writes.
                self.memory.write(0xD020, 0x0E)
                self.memory.write(0xD021, 0x06)
                self.memory.write(0xD018, 0x10)

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
            return 1  # Minimal cycles


        # Check if we're at a KERNAL vector that needs handling
        # CHRIN ($FFCF) - Input character from keyboard
        if pc == 0xFFCF:
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
            # Reconstruct return address: (high << 8) | low + 1
            self.state.pc = ((pc_high << 8) | pc_low + 1) & 0xFFFF

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
                return 20

            if udp_debug and udp_debug.enabled:
                kb_buf_len = self.memory.read(0xC6)
                udp_debug.send('chrin', {
                    'char': self.state.a,
                    'kb_buf_len': kb_buf_len
                })

            return 20  # Approximate cycles for CHRIN

        # CHROUT ($FFD2) - Output character to screen
        if pc == 0xFFD2:
            # This is CHROUT - character should be in accumulator
            char = self.state.a

            # Debug: log CHROUT entry
            if udp_debug and udp_debug.enabled:
                udp_debug.send('chrout_entry', {
                    'char': char,
                    'ascii': chr(char),
                    'pc': pc,
                    'sp': self.state.sp,
                    'cycles': getattr(self, 'current_cycles', 0)
                })

            # KERNAL screen editor sets $D0 to 0 at start (quote mode flag)
            # This is important for proper screen editor state
            self.memory.write(0xD0, 0)

            # Get cursor position from zero-page
            cursor_low = self.memory.read(0xD1)
            cursor_high = self.memory.read(0xD2)
            cursor_addr = cursor_low | (cursor_high << 8)

            # If cursor is 0 or invalid, start at screen base
            if cursor_addr < SCREEN_MEM or cursor_addr >= SCREEN_MEM + 1000:
                cursor_addr = SCREEN_MEM

            # Track last character for loop detection
            self.last_chrout_char = char

            # Minimal CHROUT implementation to avoid loops
            if char == 0x0D:  # Carriage return
                # Move to next line, scroll if at bottom
                row = (cursor_addr - SCREEN_MEM) // 40
                if row < 24:
                    # Just move to next row
                    cursor_addr = SCREEN_MEM + (row + 1) * 40
                else:
                    # At bottom row, scroll screen up
                    self.memory._scroll_screen_up()
                    # Cursor stays at bottom row (24) after scroll
                    cursor_addr = SCREEN_MEM + 24 * 40

            elif char == 0x0A:  # Line feed (LF) - in PETSCII this is 'J', but C64 screen editor ignores it
                # C64 screen editor ignores 0x0A - it has no effect on cursor positioning
                # In PETSCII, 0x0A would display as 'J' if written, but the real C64 ignores it
                # Don't write anything, don't advance cursor - just return
                pass
            elif char == 0x14:  # Backspace/Delete (PETSCII DEL)
                # Move cursor left and erase character
                # On C64, backspace moves left and deletes the character at the new position
                if cursor_addr > SCREEN_MEM:
                    cursor_addr -= 1
                    # Erase character at cursor position (write space)
                    if SCREEN_MEM <= cursor_addr < SCREEN_MEM + 1000:
                        self.memory.write(cursor_addr, 0x20)  # Space
                        # Update color RAM for the erased cell to current text color.
                        current_color = self.memory.read(0x0286) & 0x0F
                        self.memory.write(COLOR_MEM + (cursor_addr - SCREEN_MEM), current_color)
                # If at start of screen, do nothing (can't backspace further)
                # Note: cursor_addr is already updated above, so we continue to update cursor position
            elif char == 0x93:  # Clear screen
                for addr in range(SCREEN_MEM, SCREEN_MEM + 1000):
                    self.memory.write(addr, 0x20)  # Space
                # Clear color RAM to the current text color (C64 behavior).
                current_color = self.memory.read(0x0286) & 0x0F
                for addr in range(COLOR_MEM, COLOR_MEM + 1000):
                    self.memory.write(addr, current_color)
                cursor_addr = SCREEN_MEM
            else:
                # Write character to screen (no PETSCII conversion for now)
                if SCREEN_MEM <= cursor_addr < SCREEN_MEM + 1000:
                    # Just write the character as-is (PETSCII)
                    self.memory.write(cursor_addr, char)
                    # Also write the current text color to color RAM so BASIC output
                    # reflects POKE 646 (and other color changes).
                    current_color = self.memory.read(0x0286) & 0x0F
                    self.memory.write(COLOR_MEM + (cursor_addr - SCREEN_MEM), current_color)
                    cursor_addr += 1
                    # Handle wrapping/scrolling when reaching end of screen
                    if cursor_addr >= SCREEN_MEM + 1000:
                        # At end of screen - scroll up and move to next line
                        self.memory._scroll_screen_up()
                        # Cursor moves to start of bottom row (row 24, column 0)
                        cursor_addr = SCREEN_MEM + 24 * 40

            # Update cursor position
            self.memory.write(0xD1, cursor_addr & 0xFF)
            self.memory.write(0xD2, (cursor_addr >> 8) & 0xFF)

            # Also update row and column variables
            row = (cursor_addr - SCREEN_MEM) // 40
            col = (cursor_addr - SCREEN_MEM) % 40
            self.memory.write(0xD3, row)  # Cursor row
            self.memory.write(0xD8, col)  # Cursor column

            # CHROUT must return with carry CLEAR (CLC) - this is critical!
            # The KERNAL code at $E10F checks BCS (Branch if Carry Set)
            # If carry is set, it loops back to call CHROUT again
            self._clear_flag(0x01)  # Clear carry flag (bit 0)

            # Return from JSR (RTS behavior)
            # On JSR: pushes (return_address - 1)
            #   High byte first at current SP, then SP--
            #   Low byte second at new SP, then SP--
            #   So after JSR, SP points below the low byte
            # On RTS: pops in reverse order
            #   Increment SP, read low byte
            #   Increment SP, read high byte
            #   PC = (high << 8) | low + 1
            sp_before = self.state.sp
            self.state.sp = (self.state.sp + 1) & 0xFF
            pc_low = self.memory.read(0x100 + self.state.sp)
            self.state.sp = (self.state.sp + 1) & 0xFF
            pc_high = self.memory.read(0x100 + self.state.sp)
            # Reconstruct return address: (high << 8) | low + 1
            return_addr = ((pc_high << 8) | pc_low) + 1
            self.state.pc = return_addr & 0xFFFF

            # Debug: log RTS
            if udp_debug and udp_debug.enabled:
                udp_debug.send('chrout_rts', {
                    'sp_before': sp_before,
                    'sp_after': self.state.sp,
                    'pc_low': pc_low,
                    'pc_high': pc_high,
                    'return_addr': f'${return_addr:04X}',
                    'new_pc': f'${self.state.pc:04X}'
                })

            # Safety check: if return address is invalid (e.g., $0000), something is wrong
            if self.state.pc == 0x0000:
                if udp_debug and udp_debug.enabled:
                    udp_debug.send('chrout_error', {
                        'error': 'Invalid return address $0000',
                        'sp_before': (self.state.sp - 2) & 0xFF,
                        'sp_after': self.state.sp,
                        'stack_low': pc_low,
                        'stack_high': pc_high
                    })
                # Don't jump to $0000 - instead stop CPU or use a safe address
                self.state.stopped = True
                return 20

            # Log CHROUT call
            if udp_debug and udp_debug.enabled:
                udp_debug.send('chrout', {
                    'char': char,
                    'char_hex': f'${char:02X}',
                    'cursor_addr': cursor_addr,
                    'screen_addr': SCREEN_MEM,
                    'cycles': getattr(self, 'current_cycles', 0),
                    'pc': self.state.pc
                })

            return 20  # Approximate cycles for CHROUT

        cycles = self._execute_opcode(opcode)
        self.state.cycles += cycles

        # Update CIA timers
        self._update_cia_timers(cycles)

        # Update VIC-II raster line (simulate video timing)
        # Increment every cycle for fast CINT timing
        raster_max = 312 if self.memory.video_standard == "pal" else 263
        self.memory.raster_line = (self.memory.raster_line + 1) % raster_max

        # Jiffy clock is now handled by CIA timer interrupts

        # Check for pending IRQ (only if interrupts are enabled)
        if self.memory.pending_irq and not self._get_flag(0x04):  # I flag clear
            # Only handle CIA interrupts for now, skip VIC
            if self.memory.cia1_icr & 0x80:  # CIA interrupt pending
                self._handle_cia_interrupt()
            # Don't call general IRQ handler yet

        return cycles

    def _update_cia_timers(self, cycles: int) -> None:
        """Update CIA timers and check for IRQ"""
        # Update Timer A
        if self.memory.cia1_timer_a.update(cycles):
            if self.memory.cia1_timer_a.irq_enabled:
                self.memory.cia1_icr |= 0x01  # Timer A interrupt
                self.memory.cia1_icr |= 0x80  # IRQ flag
                self.memory.pending_irq = True
            self.memory.cia1_timer_a.reset()

        # Update Timer B (can be clocked by Timer A underflow)
        timer_a_underflow = False
        if self.memory.cia1_timer_a.counter <= 0 and self.memory.cia1_timer_a.running:
            timer_a_underflow = True

        if self.memory.cia1_timer_b.input_mode == 2:  # Timer A underflow mode
            if timer_a_underflow:
                if self.memory.cia1_timer_b.update(1):  # Count by 1
                    self.memory.cia1_icr |= 0x02  # Timer B interrupt
                    self.memory.cia1_icr |= 0x80  # IRQ flag
                    self.memory.pending_irq = True
                    self.memory.cia1_timer_b.reset()
        else:
            if self.memory.cia1_timer_b.update(cycles):
                self.memory.cia1_icr |= 0x02  # Timer B interrupt
                self.memory.cia1_icr |= 0x80  # IRQ flag
                self.memory.pending_irq = True
                self.memory.cia1_timer_b.reset()

    def _handle_cia_interrupt(self) -> None:
        """Handle CIA interrupts directly (bypass KERNAL for stability)"""
        # This is a simplified handler - the real C64 uses KERNAL IRQ handler at $EA31
        # which includes keyboard scanning (SCNKEY). For now, we just update jiffy clock.
        # The real IRQ handler should be called via _handle_irq() which jumps to $EA31

        # Check what CIA interrupt occurred
        icr = self.memory.cia1_icr

        if icr & 0x01:  # Timer A interrupt
            # Increment jiffy clock (C64 standard locations)
            jiffy_low = self.memory.read(0xA0)
            jiffy_mid = self.memory.read(0xA1)
            jiffy_high = self.memory.read(0xA2)

            jiffy = jiffy_low | (jiffy_mid << 8) | (jiffy_high << 16)
            jiffy += 1

            self.memory.write(0xA0, jiffy & 0xFF)
            self.memory.write(0xA1, (jiffy >> 8) & 0xFF)
            self.memory.write(0xA2, (jiffy >> 16) & 0xFF)

            # Cursor blink emulation (machine-driven, IRQ-tied).
            # Use BLNSW bit0 as "enabled" and bit7 as "visible".
            blnsw = self.memory.read(BLNSW)
            if blnsw & 0x01:
                blnct = (self.memory.read(BLNCT) + 1) & 0xFF
                if blnct >= CURSOR_BLINK_TICKS:  # ~0.5s at 60Hz
                    # Toggle visible state by flipping bit 7.
                    self.memory.write(BLNSW, blnsw ^ 0x80)
                    self.memory.write(BLNCT, 0)
                else:
                    self.memory.write(BLNCT, blnct)

            # Debug: show jiffy updates occasionally
            if hasattr(self, 'debug') and self.debug and jiffy % 10 == 0:
                debug_msg = f"⏰ Jiffy clock: {jiffy}"
                if self.interface:
                    self.interface.add_debug_log(debug_msg)

        # Clear IRQ state (we're bypassing the real KERNAL handler).
        self.memory.cia1_icr = 0
        self.memory.pending_irq = False

    def _handle_irq(self, udp_debug: Optional['UdpDebugLogger'] = None) -> None:
        """Handle IRQ interrupt"""
        # Clear pending IRQ flag before handling
        self.memory.pending_irq = False

        # Push PC and P to stack
        pc = self.state.pc
        self.memory.write(0x100 + self.state.sp, (pc >> 8) & 0xFF)
        self.state.sp = (self.state.sp - 1) & 0xFF
        self.memory.write(0x100 + self.state.sp, pc & 0xFF)
        self.state.sp = (self.state.sp - 1) & 0xFF
        self.memory.write(0x100 + self.state.sp, self.state.p | 0x10)  # Set B flag
        self.state.sp = (self.state.sp - 1) & 0xFF

        # Set interrupt disable flag
        self._set_flag(0x04, True)

        # Jump to IRQ vector
        irq_addr = self._read_word(IRQ_VECTOR)
        self.state.pc = irq_addr

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
            self.state.a = self.memory.read(addr)
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 4
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
            zp_addr = (self.memory.read(self.state.pc + 1) + self.state.y) & 0xFF
            self.state.x = self.memory.read(zp_addr)
            self._update_flags(self.state.x)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 4
        elif opcode == 0xBE:  # LDX absy
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.y) & 0xFFFF
            self.state.x = self.memory.read(addr)
            self._update_flags(self.state.x)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 4
        elif opcode == 0xA0:  # LDY imm
            return self._ldy_imm()
        elif opcode == 0xA4:  # LDY zp
            return self._ldy_zp()
        elif opcode == 0xAC:  # LDY abs
            return self._ldy_abs()
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
        elif opcode == 0x84:  # STY zp
            return self._sty_zp()
        elif opcode == 0x8C:  # STY abs
            return self._sty_abs()
        elif opcode == 0x94:  # STY zp,X (undocumented)
            return self._sty_zpx()
        elif opcode == 0x87:  # SAX zp (undocumented - A & X -> memory)
            zp_addr = self.memory.read(self.state.pc + 1)
            self.memory.write(zp_addr, self.state.a & self.state.x)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 3
        elif opcode == 0x80:  # NOP (undocumented)
            self.state.pc = (self.state.pc + 1) & 0xFFFF
            return 2
        elif opcode == 0xA3:  # LAX (indirect,X) (undocumented - LDA + TAX)
            zp_addr = (self.memory.read(self.state.pc + 1) + self.state.x) & 0xFF
            addr = self.memory.read(zp_addr) | (self.memory.read((zp_addr + 1) & 0xFF) << 8)
            self.state.a = self.memory.read(addr)
            self.state.x = self.state.a
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 6
        elif opcode == 0xC7:  # DCP zp (undocumented - DEC then CMP)
            zp_addr = self.memory.read(self.state.pc + 1)
            value = (self.memory.read(zp_addr) - 1) & 0xFF
            self.memory.write(zp_addr, value)
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
        elif opcode == 0x65:  # ADC zp
            return self._adc_zp()
        elif opcode == 0x6D:  # ADC abs
            return self._adc_abs()
        elif opcode == 0x79:  # ADC abs,Y
            return self._adc_absy()
        elif opcode == 0x7D:  # ADC abs,X
            return self._adc_absx()
        elif opcode == 0xE9:  # SBC imm
            return self._sbc_imm()
        elif opcode == 0xE5:  # SBC zp
            return self._sbc_zp()
        elif opcode == 0xF5:  # SBC zpx
            zp_addr = (self.memory.read(self.state.pc + 1) + self.state.x) & 0xFF
            value = self.memory.read(zp_addr)
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
            zp_addr = (self.memory.read(self.state.pc + 1) + self.state.x) & 0xFF
            addr_low = self.memory.read(zp_addr)
            addr_high = self.memory.read((zp_addr + 1) & 0xFF)
            addr = addr_low | (addr_high << 8)
            value = self.memory.read(addr)
            carry = 1 if self._get_flag(0x01) else 0
            result = self.state.a - value - (1 - carry)
            self._set_flag(0x01, result >= 0)
            self._set_flag(0x40, ((self.state.a ^ value) & 0x80) != 0 and ((self.state.a ^ result) & 0x80) != 0)
            self.state.a = result & 0xFF
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 6
        elif opcode == 0xF1:  # SBC indy (SBC ($nn),Y)
            zp_ptr = self.memory.read(self.state.pc + 1)
            addr_low = self.memory.read(zp_ptr)
            addr_high = self.memory.read((zp_ptr + 1) & 0xFF)
            base = addr_low | (addr_high << 8)
            addr = (base + self.state.y) & 0xFFFF
            value = self.memory.read(addr)
            carry = 1 if self._get_flag(0x01) else 0
            result = self.state.a - value - (1 - carry)
            self._set_flag(0x01, result >= 0)
            self._set_flag(0x40, ((self.state.a ^ value) & 0x80) != 0 and ((self.state.a ^ result) & 0x80) != 0)
            self.state.a = result & 0xFF
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 5  # +1 if page crossed (ignored)
        elif opcode == 0xED:  # SBC abs
            return self._sbc_abs()
        elif opcode == 0xFD:  # SBC absx
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.x) & 0xFFFF
            value = self.memory.read(addr)
            carry = 1 if self._get_flag(0x01) else 0
            result = self.state.a - value - (1 - carry)
            self._set_flag(0x01, result >= 0)
            self._set_flag(0x40, ((self.state.a ^ value) & 0x80) != 0 and ((self.state.a ^ result) & 0x80) != 0)
            self.state.a = result & 0xFF
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 4
        elif opcode == 0xF9:  # SBC absy
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.y) & 0xFFFF
            value = self.memory.read(addr)
            carry = 1 if self._get_flag(0x01) else 0
            result = self.state.a - value - (1 - carry)
            self._set_flag(0x01, result >= 0)
            self._set_flag(0x40, ((self.state.a ^ value) & 0x80) != 0 and ((self.state.a ^ result) & 0x80) != 0)
            self.state.a = result & 0xFF
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 4  # +1 cycle if page boundary crossed, but we'll ignore for simplicity

        # Logic
        elif opcode == 0x29:  # AND imm
            return self._and_imm()
        elif opcode == 0x25:  # AND zp
            return self._and_zp()
        elif opcode == 0x2D:  # AND abs
            return self._and_abs()
        elif opcode == 0x09:  # ORA imm
            return self._ora_imm()
        elif opcode == 0x05:  # ORA zp
            return self._ora_zp()
        elif opcode == 0x0D:  # ORA abs
            return self._ora_abs()
        elif opcode == 0x19:  # ORA abs,Y
            return self._ora_absy()
        elif opcode == 0x49:  # EOR imm
            return self._eor_imm()
        elif opcode == 0x45:  # EOR zp
            return self._eor_zp()
        elif opcode == 0x4D:  # EOR abs
            return self._eor_abs()

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
            value = self.memory.read(addr)
            result = (self.state.a - value) & 0xFF
            self._set_flag(0x01, self.state.a >= value)
            self._update_flags(result)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 4
        elif opcode == 0xD9:  # CMP absy
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.y) & 0xFFFF
            value = self.memory.read(addr)
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
            zp_addr = (self.memory.read(self.state.pc + 1) + self.state.x) & 0xFF
            addr = self.memory.read(zp_addr) | (self.memory.read((zp_addr + 1) & 0xFF) << 8)
            value = self.memory.read(addr)
            result = (self.state.a - value) & 0xFF
            self._set_flag(0x01, self.state.a >= value)
            self._update_flags(result)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 6
        elif opcode == 0xD1:  # CMP indy
            zp_addr = self.memory.read(self.state.pc + 1)
            base = self.memory.read(zp_addr) | (self.memory.read((zp_addr + 1) & 0xFF) << 8)
            addr = (base + self.state.y) & 0xFFFF
            value = self.memory.read(addr)
            result = (self.state.a - value) & 0xFF
            self._set_flag(0x01, self.state.a >= value)
            self._update_flags(result)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 5

        # Increment/Decrement
        elif opcode == 0xE6:  # INC zp
            return self._inc_zp()
        elif opcode == 0xEE:  # INC abs
            return self._inc_abs()
        elif opcode == 0xC6:  # DEC zp
            return self._dec_zp()
        elif opcode == 0xCE:  # DEC abs
            return self._dec_abs()
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
        elif opcode == 0x4A:  # LSR acc
            return self._lsr_acc()
        elif opcode == 0x46:  # LSR zp
            return self._lsr_zp()
        elif opcode == 0x56:  # LSR zp,X
            return self._lsr_zpx()
        elif opcode == 0x4E:  # LSR abs
            return self._lsr_abs()
        elif opcode == 0x2A:  # ROL acc
            return self._rol_acc()
        elif opcode == 0x26:  # ROL zp
            return self._rol_zp()
        elif opcode == 0x2E:  # ROL abs
            return self._rol_abs()
        elif opcode == 0x6A:  # ROR acc
            return self._ror_acc()
        elif opcode == 0x66:  # ROR zp
            return self._ror_zp()
        elif opcode == 0x76:  # ROR zp,X
            return self._ror_zpx()
        elif opcode == 0x6E:  # ROR abs
            return self._ror_abs()
        elif opcode == 0xFE:  # INC absx
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.x) & 0xFFFF
            value = (self.memory.read(addr) + 1) & 0xFF
            self.memory.write(addr, value)
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
            self.state.y = self.memory.read(0x100 + self.state.sp)
            self._update_flags(self.state.y)
            self.state.pc = (self.state.pc + 1) & 0xFFFF
            return 4
        elif opcode == 0x7F:  # RRA absx (undocumented - ROR + ADC)
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.x) & 0xFFFF
            value = self.memory.read(addr)
            carry = 1 if self._get_flag(0x01) else 0
            new_carry = (value & 0x01) != 0
            value = ((value >> 1) | (carry << 7)) & 0xFF
            self.memory.write(addr, value)
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
            zp_addr = self.memory.read(self.state.pc + 1)
            self.state.a = self.memory.read(zp_addr)
            self.state.x = self.state.a
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 3
        elif opcode == 0xAF:  # LAX abs (undocumented - LDA + TAX)
            addr = self._read_word(self.state.pc + 1)
            self.state.a = self.memory.read(addr)
            self.state.x = self.state.a
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 4
        elif opcode == 0xBF:  # LAX absy (undocumented - LDA + TAX)
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.y) & 0xFFFF
            self.state.a = self.memory.read(addr)
            self.state.x = self.state.a
            self._update_flags(self.state.a)
            self.state.pc = (self.state.pc + 3) & 0xFFFF
            return 4
        elif opcode == 0xFF:  # ISC absx (undocumented - increment memory, then subtract with carry)
            base = self._read_word(self.state.pc + 1)
            addr = (base + self.state.x) & 0xFFFF
            value = (self.memory.read(addr) + 1) & 0xFF
            self.memory.write(addr, value)
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
            # Clear any pending interrupts before enabling
            self.memory.pending_irq = False
            self._set_flag(0x04, False)
            self.state.pc = (self.state.pc + 1) & 0xFFFF
            if self.interface:
                self.interface.add_debug_log(f"🚫 CLI executed, I-flag now {self._get_flag(0x04)}, cleared pending IRQs")
            return 2
        elif opcode == 0x78:  # SEI
            self._set_flag(0x04, True)
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
        elif opcode in [0x80, 0x82, 0x89, 0xC2, 0xE2]:  # NOP imm (documented - consume 1 byte operand)
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
        self.memory.write(0x100 + self.state.sp, pc_high)
        self.state.sp = (self.state.sp - 1) & 0xFF
        self.memory.write(0x100 + self.state.sp, pc_low)
        self.state.sp = (self.state.sp - 1) & 0xFF
        self.memory.write(0x100 + self.state.sp, self.state.p | 0x10)  # Set B flag
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
        self.memory.write(0x100 + self.state.sp, pc_high)
        self.state.sp = (self.state.sp - 1) & 0xFF
        self.memory.write(0x100 + self.state.sp, pc_low)
        self.state.sp = (self.state.sp - 1) & 0xFF
        self.state.pc = addr
        return 6

    def _rts(self) -> int:
        """RTS"""
        self.state.sp = (self.state.sp + 1) & 0xFF
        pc_low = self.memory.read(0x100 + self.state.sp)
        self.state.sp = (self.state.sp + 1) & 0xFF
        pc_high = self.memory.read(0x100 + self.state.sp)
        self.state.pc = ((pc_high << 8) | pc_low + 1) & 0xFFFF
        return 6

    def _lda_imm(self) -> int:
        """LDA immediate"""
        self.state.a = self.memory.read(self.state.pc + 1)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _lda_zp(self) -> int:
        """LDA zero page"""
        zp_addr = self.memory.read(self.state.pc + 1)
        self.state.a = self.memory.read(zp_addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _lda_abs(self) -> int:
        """LDA absolute"""
        addr = self._read_word(self.state.pc + 1)
        self.state.a = self.memory.read(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _sta_zp(self) -> int:
        """STA zero page"""
        zp_addr = self.memory.read(self.state.pc + 1)
        self.memory.write(zp_addr, self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _sta_abs(self) -> int:
        """STA absolute"""
        addr = self._read_word(self.state.pc + 1)
        self.memory.write(addr, self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    # Additional opcode implementations (simplified - add more as needed)
    def _lda_zpx(self) -> int:
        zp_addr = (self.memory.read(self.state.pc + 1) + self.state.x) & 0xFF
        self.state.a = self.memory.read(zp_addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 4

    def _lda_absx(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.x) & 0xFFFF
        self.state.a = self.memory.read(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _lda_absy(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.y) & 0xFFFF
        self.state.a = self.memory.read(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _lda_indx(self) -> int:
        zp_addr = (self.memory.read(self.state.pc + 1) + self.state.x) & 0xFF
        addr = self.memory.read(zp_addr) | (self.memory.read((zp_addr + 1) & 0xFF) << 8)
        self.state.a = self.memory.read(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6

    def _lda_indy(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        base = self.memory.read(zp_addr) | (self.memory.read((zp_addr + 1) & 0xFF) << 8)
        addr = (base + self.state.y) & 0xFFFF
        self.state.a = self.memory.read(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 5

    def _ldx_imm(self) -> int:
        self.state.x = self.memory.read(self.state.pc + 1)
        self._update_flags(self.state.x)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _ldx_zp(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        self.state.x = self.memory.read(zp_addr)
        self._update_flags(self.state.x)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _ldx_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        self.state.x = self.memory.read(addr)
        self._update_flags(self.state.x)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _ldy_imm(self) -> int:
        self.state.y = self.memory.read(self.state.pc + 1)
        self._update_flags(self.state.y)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _ldy_zp(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        self.state.y = self.memory.read(zp_addr)
        self._update_flags(self.state.y)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _ldy_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        self.state.y = self.memory.read(addr)
        self._update_flags(self.state.y)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _ldy_zpx(self) -> int:
        """LDY zero page,X (undocumented opcode $B4)"""
        zp_addr = (self.memory.read(self.state.pc + 1) + self.state.x) & 0xFF
        self.state.y = self.memory.read(zp_addr)
        self._update_flags(self.state.y)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 4

    def _sta_zpx(self) -> int:
        zp_addr = (self.memory.read(self.state.pc + 1) + self.state.x) & 0xFF
        self.memory.write(zp_addr, self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 4

    def _sta_absx(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.x) & 0xFFFF
        self.memory.write(addr, self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 5

    def _sta_absy(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.y) & 0xFFFF
        self.memory.write(addr, self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 5

    def _sta_indx(self) -> int:
        zp_addr = (self.memory.read(self.state.pc + 1) + self.state.x) & 0xFF
        addr = self.memory.read(zp_addr) | (self.memory.read((zp_addr + 1) & 0xFF) << 8)
        self.memory.write(addr, self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6

    def _sta_indy(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        base = self.memory.read(zp_addr) | (self.memory.read((zp_addr + 1) & 0xFF) << 8)
        addr = (base + self.state.y) & 0xFFFF
        self.memory.write(addr, self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6

    def _stx_zp(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        self.memory.write(zp_addr, self.state.x)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _stx_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        self.memory.write(addr, self.state.x)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _sty_zp(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        self.memory.write(zp_addr, self.state.y)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _sty_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        self.memory.write(addr, self.state.y)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _sty_zpx(self) -> int:
        """STY zero page,X (undocumented opcode $94)"""
        zp_addr = (self.memory.read(self.state.pc + 1) + self.state.x) & 0xFF
        self.memory.write(zp_addr, self.state.y)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 4

    # Arithmetic operations (simplified)
    def _adc_imm(self) -> int:
        value = self.memory.read(self.state.pc + 1)
        carry = 1 if self._get_flag(0x01) else 0
        result = self.state.a + value + carry
        self._set_flag(0x01, result > 0xFF)
        self.state.a = result & 0xFF
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _adc_zp(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        value = self.memory.read(zp_addr)
        carry = 1 if self._get_flag(0x01) else 0
        result = self.state.a + value + carry
        self._set_flag(0x01, result > 0xFF)
        self.state.a = result & 0xFF
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _adc_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self.memory.read(addr)
        carry = 1 if self._get_flag(0x01) else 0
        result = self.state.a + value + carry
        self._set_flag(0x01, result > 0xFF)
        self.state.a = result & 0xFF
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _adc_absx(self) -> int:
        """ADC (Add with Carry) absolute,X"""
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.x) & 0xFFFF
        value = self.memory.read(addr)
        carry = 1 if self._get_flag(0x01) else 0
        result = self.state.a + value + carry
        self._set_flag(0x01, result > 0xFF)
        self.state.a = result & 0xFF
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4  # +1 cycle if page boundary crossed, but we'll ignore for simplicity

    def _adc_absy(self) -> int:
        """ADC (Add with Carry) absolute,Y"""
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.y) & 0xFFFF
        value = self.memory.read(addr)
        carry = 1 if self._get_flag(0x01) else 0
        result = self.state.a + value + carry
        self._set_flag(0x01, result > 0xFF)
        self.state.a = result & 0xFF
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4  # +1 cycle if page boundary crossed, but we'll ignore for simplicity

    def _sbc_imm(self) -> int:
        value = self.memory.read(self.state.pc + 1)
        carry = 1 if self._get_flag(0x01) else 0
        result = self.state.a - value - (1 - carry)
        self._set_flag(0x01, result >= 0)
        self._set_flag(0x40, ((self.state.a ^ value) & 0x80) != 0 and ((self.state.a ^ result) & 0x80) != 0)
        self.state.a = result & 0xFF
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _sbc_zp(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        value = self.memory.read(zp_addr)
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
        value = self.memory.read(addr)
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
        self.state.a &= self.memory.read(self.state.pc + 1)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _and_zp(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        self.state.a &= self.memory.read(zp_addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _and_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        self.state.a &= self.memory.read(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _ora_imm(self) -> int:
        self.state.a |= self.memory.read(self.state.pc + 1)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _ora_zp(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        self.state.a |= self.memory.read(zp_addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _ora_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        self.state.a |= self.memory.read(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _ora_absy(self) -> int:
        base = self._read_word(self.state.pc + 1)
        addr = (base + self.state.y) & 0xFFFF
        self.state.a |= self.memory.read(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _eor_imm(self) -> int:
        self.state.a ^= self.memory.read(self.state.pc + 1)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _eor_zp(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        self.state.a ^= self.memory.read(zp_addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _eor_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        self.state.a ^= self.memory.read(addr)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    # Compare operations
    def _cmp_imm(self) -> int:
        value = self.memory.read(self.state.pc + 1)
        result = (self.state.a - value) & 0xFF
        self._set_flag(0x01, self.state.a >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _cmp_zp(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        value = self.memory.read(zp_addr)
        result = (self.state.a - value) & 0xFF
        self._set_flag(0x01, self.state.a >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _cmp_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self.memory.read(addr)
        result = (self.state.a - value) & 0xFF
        self._set_flag(0x01, self.state.a >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _cpx_imm(self) -> int:
        value = self.memory.read(self.state.pc + 1)
        result = (self.state.x - value) & 0xFF
        self._set_flag(0x01, self.state.x >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _cpx_zp(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        value = self.memory.read(zp_addr)
        result = (self.state.x - value) & 0xFF
        self._set_flag(0x01, self.state.x >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _cpx_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self.memory.read(addr)
        result = (self.state.x - value) & 0xFF
        self._set_flag(0x01, self.state.x >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    def _cpy_imm(self) -> int:
        value = self.memory.read(self.state.pc + 1)
        result = (self.state.y - value) & 0xFF
        self._set_flag(0x01, self.state.y >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 2

    def _cpy_zp(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        value = self.memory.read(zp_addr)
        result = (self.state.y - value) & 0xFF
        self._set_flag(0x01, self.state.y >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _cpy_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self.memory.read(addr)
        result = (self.state.y - value) & 0xFF
        self._set_flag(0x01, self.state.y >= value)
        self._update_flags(result)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4

    # Increment/Decrement
    def _inc_zp(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        value = (self.memory.read(zp_addr) + 1) & 0xFF
        self.memory.write(zp_addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 5

    def _inc_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = (self.memory.read(addr) + 1) & 0xFF
        self.memory.write(addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 6

    def _dec_zp(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        value = (self.memory.read(zp_addr) - 1) & 0xFF
        self.memory.write(zp_addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 5

    def _dec_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = (self.memory.read(addr) - 1) & 0xFF
        self.memory.write(addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 6

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
        zp_addr = self.memory.read(self.state.pc + 1)
        value = self.memory.read(zp_addr)
        self._set_flag(0x01, (value & 0x80) != 0)
        value = (value << 1) & 0xFF
        self.memory.write(zp_addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 5

    def _asl_zpx(self) -> int:
        """ASL (Arithmetic Shift Left) zero-page,X"""
        zp_addr = (self.memory.read(self.state.pc + 1) + self.state.x) & 0xFF
        value = self.memory.read(zp_addr)
        self._set_flag(0x01, (value & 0x80) != 0)  # Carry = bit 7
        value = (value << 1) & 0xFF
        self.memory.write(zp_addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6

    def _asl_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self.memory.read(addr)
        self._set_flag(0x01, (value & 0x80) != 0)
        value = (value << 1) & 0xFF
        self.memory.write(addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 6

    def _lsr_acc(self) -> int:
        self._set_flag(0x01, (self.state.a & 0x01) != 0)
        self.state.a = (self.state.a >> 1) & 0xFF
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 2

    def _lsr_zp(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        value = self.memory.read(zp_addr)
        self._set_flag(0x01, (value & 0x01) != 0)
        value = (value >> 1) & 0xFF
        self.memory.write(zp_addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 5

    def _lsr_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self.memory.read(addr)
        self._set_flag(0x01, (value & 0x01) != 0)
        value = (value >> 1) & 0xFF
        self.memory.write(addr, value)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 6

    def _lsr_zpx(self) -> int:
        """LSR (Logical Shift Right) zero-page,X"""
        zp_addr = (self.memory.read(self.state.pc + 1) + self.state.x) & 0xFF
        value = self.memory.read(zp_addr)
        self._set_flag(0x01, (value & 0x01) != 0)  # Carry = bit 0
        value = (value >> 1) & 0xFF
        self.memory.write(zp_addr, value)
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
        zp_addr = self.memory.read(self.state.pc + 1)
        value = self.memory.read(zp_addr)
        carry = 1 if self._get_flag(0x01) else 0
        new_carry = (value & 0x80) != 0
        value = ((value << 1) | carry) & 0xFF
        self.memory.write(zp_addr, value)
        self._set_flag(0x01, new_carry)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 5

    def _rol_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self.memory.read(addr)
        carry = 1 if self._get_flag(0x01) else 0
        new_carry = (value & 0x80) != 0
        value = ((value << 1) | carry) & 0xFF
        self.memory.write(addr, value)
        self._set_flag(0x01, new_carry)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 6

    def _ror_acc(self) -> int:
        carry = 1 if self._get_flag(0x01) else 0
        new_carry = (self.state.a & 0x01) != 0
        self.state.a = ((self.state.a >> 1) | (carry << 7)) & 0xFF
        self._set_flag(0x01, new_carry)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 2

    def _ror_zp(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        value = self.memory.read(zp_addr)
        carry = 1 if self._get_flag(0x01) else 0
        new_carry = (value & 0x01) != 0
        value = ((value >> 1) | (carry << 7)) & 0xFF
        self.memory.write(zp_addr, value)
        self._set_flag(0x01, new_carry)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 5

    def _ror_zpx(self) -> int:
        zp_addr = (self.memory.read(self.state.pc + 1) + self.state.x) & 0xFF
        value = self.memory.read(zp_addr)
        carry = 1 if self._get_flag(0x01) else 0
        new_carry = (value & 0x01) != 0
        value = ((value >> 1) | (carry << 7)) & 0xFF
        self.memory.write(zp_addr, value)
        self._set_flag(0x01, new_carry)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 6

    def _ror_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self.memory.read(addr)
        carry = 1 if self._get_flag(0x01) else 0
        new_carry = (value & 0x01) != 0
        value = ((value >> 1) | (carry << 7)) & 0xFF
        self.memory.write(addr, value)
        self._set_flag(0x01, new_carry)
        self._update_flags(value)
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 6

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
        offset = self.memory.read(self.state.pc + 1)
        if offset & 0x80:
            offset = offset - 256
        if condition:
            self.state.pc = (self.state.pc + 2 + offset) & 0xFFFF
            return 3
        else:
            self.state.pc = (self.state.pc + 2) & 0xFFFF
            return 2

    # Jumps
    def _jmp_ind(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        # Handle page boundary bug
        if (addr & 0xFF) == 0xFF:
            low = self.memory.read(addr)
            high = self.memory.read(addr & 0xFF00)
        else:
            low = self.memory.read(addr)
            high = self.memory.read(addr + 1)
        self.state.pc = low | (high << 8)
        return 5

    # Stack operations
    def _pha(self) -> int:
        self.memory.write(0x100 + self.state.sp, self.state.a)
        self.state.sp = (self.state.sp - 1) & 0xFF
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 3

    def _pla(self) -> int:
        self.state.sp = (self.state.sp + 1) & 0xFF
        self.state.a = self.memory.read(0x100 + self.state.sp)
        self._update_flags(self.state.a)
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 4

    def _php(self) -> int:
        self.memory.write(0x100 + self.state.sp, self.state.p | 0x10)  # Set B flag
        self.state.sp = (self.state.sp - 1) & 0xFF
        self.state.pc = (self.state.pc + 1) & 0xFFFF
        return 3

    def _plp(self) -> int:
        self.state.sp = (self.state.sp + 1) & 0xFF
        self.state.p = self.memory.read(0x100 + self.state.sp) & 0xEF  # Clear B flag
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
        self.state.p = self.memory.read(0x100 + self.state.sp) & 0xEF
        self.state.sp = (self.state.sp + 1) & 0xFF
        pc_low = self.memory.read(0x100 + self.state.sp)
        self.state.sp = (self.state.sp + 1) & 0xFF
        pc_high = self.memory.read(0x100 + self.state.sp)
        self.state.pc = (pc_low | (pc_high << 8)) & 0xFFFF
        return 6

    def _bit_zp(self) -> int:
        zp_addr = self.memory.read(self.state.pc + 1)
        value = self.memory.read(zp_addr)
        self._set_flag(0x40, (value & 0x40) != 0)  # V flag
        self._set_flag(0x80, (value & 0x80) != 0)  # N flag
        self._set_flag(0x02, (self.state.a & value) == 0)  # Z flag
        self.state.pc = (self.state.pc + 2) & 0xFFFF
        return 3

    def _bit_abs(self) -> int:
        addr = self._read_word(self.state.pc + 1)
        value = self.memory.read(addr)
        self._set_flag(0x40, (value & 0x40) != 0)  # V flag
        self._set_flag(0x80, (value & 0x80) != 0)  # N flag
        self._set_flag(0x02, (self.state.a & value) == 0)  # Z flag
        self.state.pc = (self.state.pc + 3) & 0xFFFF
        return 4


