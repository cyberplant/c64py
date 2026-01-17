"""
Textual User Interface
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, List, Optional, Tuple

from rich.console import Console
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.events import Key
from textual.widgets import Static, Header, Footer, RichLog

from .constants import (
    BLNSW,
    COLOR_MEM,
    BORDER_WIDTH,
    BORDER_HEIGHT,
    SCREEN_COLS,
    SCREEN_ROWS,
    CURSOR_COL_ADDR,
    CURSOR_PTR_HIGH,
    CURSOR_PTR_LOW,
    CURSOR_ROW_ADDR,
    INPUT_BUFFER_BASE,
    INPUT_BUFFER_INDEX_ADDR,
    INPUT_BUFFER_LEN_ADDR,
    KEYBOARD_BUFFER_BASE,
    KEYBOARD_BUFFER_LEN_ADDR,
    SCREEN_MEM,
)

if TYPE_CHECKING:
    from .emulator import C64

class TextualInterface(App):
    """Textual-based interface with TCSS styling"""

    BINDINGS = [
        ("ctrl+x", "quit", "Quit the emulator"),
        ("ctrl+r", "random_screen", "Fill screen with random characters"),
        ("ctrl+k", "dump_memory", "Dump screen memory and $0801 to debug logs"),
    ]

    CSS = """
    Screen {
        background: $surface;
        layout: vertical;
    }

    #c64-display {
        border: solid $primary;
        margin: 0 1;
        padding: 0;
        height: 40fr;
        width: 10fr;
        background: #0000AA;
        color: #FFFFFF;
    }

    Screen.fullscreen #c64-display {
        border: none;
        margin: 0;
        padding: 0;
        height: 100%;
        width: 100%;
    }

    #debug-panel {
        border: solid $secondary;
        margin: 0 0;
        overflow-y: scroll;
        padding: 0 0;
        height: 25%;
    }

    #status-bar {
        border: none;
        margin: 0 0;
        padding: 0 0;
        height: 1;
        background: $primary;
        color: $surface;
    }
    """

    def __init__(self, emulator, max_cycles=None, fullscreen=False):
        super().__init__()
        self.emulator = emulator
        self.max_cycles = max_cycles
        self.max_logs = 1000
        self.current_cycle = 0
        self.emulator_thread = None
        self.running = False
        self.fullscreen = fullscreen
        # Widget references (set in on_mount)
        self.c64_display = None
        self.debug_logs = None
        self.status_bar = None
        # Last committed input line (debug/inspection)
        self.last_committed_line = ""
        # Cursor blink is machine-driven (IRQ-tied); UI just displays it.
        self.cursor_blink_on = True

    def compose(self) -> ComposeResult:
        if not self.fullscreen:
            yield Header()
        yield RichLog(id="c64-display", auto_scroll=False)
        if not self.fullscreen:
            yield RichLog(id="debug-panel", auto_scroll=True)
            yield Static("Initializing...", id="status-bar")
        if not self.fullscreen:
            yield Footer()

    def on_mount(self):
        """Called when the app is mounted"""
        if self.fullscreen:
            # In fullscreen mode, add the fullscreen class to the screen
            self.screen.add_class("fullscreen")

        self.c64_display = self.query_one("#c64-display", RichLog)
        self.c64_display.write("Loading C64...")

        if not self.fullscreen:
            self.debug_logs = self.query_one("#debug-panel", RichLog)
            self.status_bar = self.query_one("#status-bar", Static)

        # Debug: check if widgets are found (only in non-fullscreen mode)
        if not self.fullscreen:
            self.add_debug_log(f"Widgets found: c64={self.c64_display is not None}, debug={self.debug_logs is not None}, status={self.status_bar is not None}")

        # Buffered messages are handled automatically in add_debug_log

        # Start emulator in background thread
        self.running = True
        self.emulator_thread = threading.Thread(target=self._run_emulator, daemon=True)
        self.emulator_thread.start()

        # Update UI periodically
        self.set_interval(0.1, self._update_ui)

    def _run_emulator(self):
        """Run the emulator in background thread"""
        try:
            # For Textual interface, run without the screen update worker
            # since UI updates are handled by _update_ui
            self.emulator.running = True
            cycles = 0
            max_cycles = self.max_cycles
            last_pc = None
            stuck_count = 0

            while self.emulator.running:
                if max_cycles is not None and cycles >= max_cycles:
                    if hasattr(self.emulator, 'autoquit') and self.emulator.autoquit:
                        self.emulator.running = False
                    break

                # Load program if pending (after BASIC boot completes)
                if self.emulator.prg_file_path and not hasattr(self.emulator, '_program_loaded_after_boot'):
                    # BASIC is ready - load the program now (after boot has completed)
                    # Wait until we're past boot sequence (cycles > 2020000)
                    if cycles > 2020000:
                        try:
                            self.emulator.load_prg(self.emulator.prg_file_path)
                            self.emulator.prg_file_path = None  # Clear path after loading
                            self.emulator._program_loaded_after_boot = True
                            self.add_debug_log("💾 Program loaded after BASIC boot completed")
                        except Exception as e:
                            self.add_debug_log(f"❌ Failed to load program: {e}")
                            self.emulator.prg_file_path = None  # Clear path even on error

                step_cycles = self.emulator.cpu.step(self.emulator.udp_debug, cycles)
                cycles += step_cycles
                self.emulator.current_cycles = cycles

                # Stuck detection
                pc = self.emulator.cpu.state.pc
                if pc == last_pc:
                    # CHRIN ($FFCF) blocks when keyboard buffer is empty - this is expected behavior
                    # Don't count it as stuck
                    if pc != 0xFFCF:
                        stuck_count += 1
                        if stuck_count > 1000:
                            self.add_debug_log(f"⚠️ PC stuck at ${pc:04X} for {stuck_count} steps - stopping")
                            self.emulator.running = False
                            break
                    else:
                        # PC is at CHRIN - reset stuck count since blocking is expected
                        stuck_count = 0
                else:
                    stuck_count = 0
                last_pc = pc

            # Log why we stopped
            if hasattr(self, 'add_debug_log'):
                if max_cycles is not None and cycles >= max_cycles:
                    self.add_debug_log(f"🛑 Stopped at cycle {cycles} (reached max_cycles={max_cycles})")
                else:
                    self.add_debug_log(f"🛑 Stopped at cycle {cycles} (unknown reason, stuck_count={stuck_count})")

        except Exception as e:
            if hasattr(self, 'add_debug_log'):
                self.add_debug_log(f"❌ Emulator error: {e}")

    def _update_ui(self):
        """Update the UI periodically"""
        if self.emulator and not self.emulator.running:
            # Emulator has stopped (e.g., due to autoquit), exit the app
            self.add_debug_log("🛑 Emulator stopped, exiting...")
            # Capture last lines of log before exiting
            last_lines = self._get_last_log_lines(20)
            self.exit()
            # Print captured logs to console after UI shutdown
            if last_lines:
                print("\n=== Last log messages ===")
                for line in last_lines:
                    print(line)
            return

        if self.emulator:
            # Update text screen from memory
            self.emulator._update_text_screen()

            # Update screen display
            screen_content = self.emulator.render_text_screen(no_colors=False)
            cursor_row = max(0, min(self.emulator.memory.read(CURSOR_ROW_ADDR), SCREEN_ROWS - 1))
            cursor_col = max(0, min(self.emulator.memory.read(CURSOR_COL_ADDR), SCREEN_COLS - 1))

            # Normalize render output once.
            if isinstance(screen_content, Text):
                screen_text = screen_content.copy()
                screen_plain = screen_text.plain
            else:
                screen_plain = str(screen_content)
                screen_text = Text(screen_plain)

            # Debug: Check if screen has any non-space content
            non_space_count = sum(1 for c in screen_plain if c not in (' ', '\n'))
            if non_space_count > 0 and not hasattr(self, '_screen_debug_logged'):
                # Sample first few characters from screen memory
                sample_chars = []
                for addr in range(SCREEN_MEM, SCREEN_MEM + 20):
                    char_code = self.emulator.memory.read(addr)
                    sample_chars.append(f"${char_code:02X}")
                self.add_debug_log(f"📺 Screen has {non_space_count} non-space chars. First 20 bytes: {', '.join(sample_chars)}")
                self._screen_debug_logged = True

            # Machine-controlled cursor blink (IRQ-tied emulation).
            # Only show cursor when the ROM is actually waiting for keyboard input.
            line_edit_mode = self._is_line_edit_mode()
            # bit0 = enabled, bit7 = visible
            self.cursor_blink_on = bool(self.emulator.memory.read(BLNSW) & 0x80)

            if line_edit_mode and self.cursor_blink_on:
                # Cursor position is relative to the 40x25 text area.
                # Our renderer includes a thick border; map cursor into the rendered text.
                full_cols = SCREEN_COLS + BORDER_WIDTH * 2  # must match emulator renderer
                line_stride = full_cols + 1  # + newline
                cursor_index = (BORDER_HEIGHT * line_stride) + (cursor_row * line_stride) + BORDER_WIDTH + cursor_col
                if 0 <= cursor_index < len(screen_plain):
                    screen_text.stylize("reverse", cursor_index, cursor_index + 1)
            self.c64_display.clear()
            self.c64_display.write(screen_text)

            # Update status bar with actual cycle count from emulator (only in non-fullscreen mode)
            if not self.fullscreen:
                emu = self.emulator
                # Reuse cursor_row/cursor_col from earlier in this update cycle.
                port01 = emu.memory.ram[0x01]
                txt_color = emu.memory.read(0x0286) & 0x0F
                bg = emu.memory.peek_vic(0x21) & 0x0F
                border = emu.memory.peek_vic(0x20) & 0x0F
                status_text = (
                    f"🎮 C64 | Cycle: {emu.current_cycles:,} | PC: ${emu.cpu.state.pc:04X} | "
                    f"A: ${emu.cpu.state.a:02X} | X: ${emu.cpu.state.x:02X} | Y: ${emu.cpu.state.y:02X} | "
                    f"SP: ${emu.cpu.state.sp:02X} | Cursor: {cursor_row},{cursor_col} | "
                    f"$01=${port01:02X} | BG:{bg} BORDER:{border} TXT:{txt_color}"
                )
                if self.status_bar:
                    self.status_bar.update(status_text)

            # Debug: show screen content periodically
            if hasattr(self.emulator, 'debug') and self.emulator.debug:
                non_spaces = sum(1 for row in self.emulator.text_screen for char in row if char != ' ')
                if non_spaces > 0:
                    first_line = ''.join(self.emulator.text_screen[0]).rstrip()
                    if first_line:
                        self.add_debug_log(f"📝 Screen content: '{first_line}'")

    def add_debug_log(self, message: str):
        """Add a debug message"""
        # Skip debug logging in fullscreen mode
        if self.fullscreen:
            return

        from datetime import datetime
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {message}"

        # Buffer message if widget not ready yet
        if not hasattr(self, 'debug_messages'):
            self.debug_messages = []
            self.max_logs = 1000  # Keep more messages

        self.debug_messages.append(formatted_message)
        if len(self.debug_messages) > self.max_logs:
            self.debug_messages.pop(0)

        # Update widget if it's available
        if self.debug_logs:
            # If this is the first time, write all buffered messages
            if not hasattr(self, '_debug_initialized'):
                for msg in self.debug_messages:
                    self.debug_logs.write(msg)
                self._debug_initialized = True
            else:
                # Just write the latest message
                self.debug_logs.write(formatted_message)

    def _get_last_log_lines(self, count: int = 20) -> List[str]:
        """Get the last N lines from the debug log"""
        if hasattr(self, 'debug_messages'):
            return self.debug_messages[-count:] if len(self.debug_messages) > count else self.debug_messages
        return []

    def update_screen(self, screen_content: str):
        """Stub method for compatibility - Textual updates automatically"""
        pass

    def update_status(self):
        """Stub method for compatibility - Textual updates automatically"""
        pass

    def check_input(self):
        """Stub method for compatibility - Textual handles input automatically"""
        return False

    def action_quit(self):
        """Quit the emulator"""
        self.running = False
        if self.emulator:
            self.emulator.running = False
        self.exit()

    def action_random_screen(self):
        """Fill screen with random characters for testing"""
        import random
        if self.emulator:
            # Fill screen memory with random visible characters
            for addr in range(0x0400, 0x0400 + 1000):  # Full screen
                # Use random printable ASCII characters (0x20-0x7E)
                char_code = random.randint(0x20, 0x7E)
                self.emulator.memory.ram[addr] = char_code
            self.add_debug_log("🎲 Filled screen with random characters")
            # Trigger immediate screen update
            self.emulator._update_text_screen()

    def action_dump_memory(self):
        """Dump screen memory and $0801 bytes to debug logs"""
        if self.emulator:
            # Dump first few lines of screen memory
            lines = []
            for row in range(min(5, 25)):  # First 5 rows
                line_start = 0x0400 + row * 40
                line_data = []
                for col in range(min(20, 40)):  # First 20 columns
                    char_code = self.emulator.memory.ram[line_start + col]
                    # Convert to printable char or show code
                    if 32 <= char_code <= 126:
                        line_data.append(chr(char_code))
                    else:
                        line_data.append(f'${char_code:02X}')
                lines.append(f"Row {row}: {''.join(line_data)}")
            self.add_debug_log("📺 Screen memory dump:")
            for line in lines:
                self.add_debug_log(f"  {line}")

            # Dump first 16 bytes at $0801
            self.add_debug_log("📝 Memory dump at $0801 (first 16 bytes):")
            bytes_list = []
            for i in range(16):
                byte_val = self.emulator.memory.read(0x0801 + i)
                bytes_list.append(f"${byte_val:02X}")
            self.add_debug_log(f"  {', '.join(bytes_list)}")

            # Also show BASIC pointers
            basic_start = self.emulator.memory.read(0x002B) | (self.emulator.memory.read(0x002C) << 8)
            basic_end = self.emulator.memory.read(0x002D) | (self.emulator.memory.read(0x002E) << 8)
            self.add_debug_log(f"📝 BASIC start pointer ($2B/$2C): ${basic_start:04X}")
            self.add_debug_log(f"📝 BASIC end pointer ($2D/$2E): ${basic_end:04X}")

    def _ascii_to_petscii(self, char: str) -> int:
        """Convert ASCII character to PETSCII code"""
        if not char:
            return 0
        ascii_code = ord(char)

        # Basic ASCII to PETSCII conversion
        # PETSCII uppercase letters: 0x41-0x5A (A-Z)
        # PETSCII lowercase letters: 0x61-0x7A (a-z) but shifted
        # For simplicity, map common ASCII to PETSCII
        if 0x20 <= ascii_code <= 0x5F:  # Space through underscore
            # Most ASCII printable chars map directly in this range
            return ascii_code
        elif 0x61 <= ascii_code <= 0x7A:  # Lowercase a-z
            # Convert to uppercase PETSCII (shifted)
            return ascii_code - 0x20  # a-z -> A-Z in PETSCII
        elif ascii_code == 0x0D or ascii_code == 0x0A:  # CR or LF
            return 0x0D  # Carriage return
        else:
            # Default: return as-is (may need more mapping)
            return ascii_code & 0xFF

    def _echo_character(self, petscii_code: int) -> None:
        """Echo a character to the screen at current cursor position"""
        if not self.emulator:
            return

        # Get cursor position from zero-page
        cursor_low = self.emulator.memory.read(CURSOR_PTR_LOW)
        cursor_high = self.emulator.memory.read(CURSOR_PTR_HIGH)
        cursor_addr = cursor_low | (cursor_high << 8)

        # If cursor is invalid, start at screen base
        if cursor_addr < SCREEN_MEM or cursor_addr >= SCREEN_MEM + 1000:
            cursor_addr = SCREEN_MEM

        # Handle special characters
        if petscii_code == 0x0D:  # Carriage return
            # Move to next line, scroll if at bottom
            row = (cursor_addr - SCREEN_MEM) // 40
            if row < 24:
                # Just move to next row
                cursor_addr = SCREEN_MEM + (row + 1) * 40
            else:
                # At bottom row, scroll screen up
                self.emulator.memory._scroll_screen_up()
                # Cursor stays at bottom row (24) after scroll
                cursor_addr = SCREEN_MEM + 24 * 40
        elif petscii_code == 0x0A:  # Line feed - ignore (C64 screen editor ignores it)
            return  # Don't echo LF
        elif petscii_code == 0x93:  # Clear screen
            for addr in range(SCREEN_MEM, SCREEN_MEM + 1000):
                self.emulator.memory.write(addr, 0x20)  # Space
            # Clear color RAM to current text color as well
            current_color = self.emulator.memory.read(0x0286) & 0x0F
            for addr in range(COLOR_MEM, COLOR_MEM + 1000):
                self.emulator.memory.write(addr, current_color)
            cursor_addr = SCREEN_MEM
        else:
            # Write character to screen
            if SCREEN_MEM <= cursor_addr < SCREEN_MEM + 1000:
                self.emulator.memory.write(cursor_addr, petscii_code)
                # Also update color RAM so typed characters reflect the active BASIC color.
                current_color = self.emulator.memory.read(0x0286) & 0x0F
                self.emulator.memory.write(COLOR_MEM + (cursor_addr - SCREEN_MEM), current_color)
                cursor_addr += 1
                # Handle wrapping/scrolling when reaching end of screen
                if cursor_addr >= SCREEN_MEM + 1000:
                    # At end of screen - scroll up and move to next line
                    self.emulator.memory._scroll_screen_up()
                    # Cursor moves to start of bottom row (row 24, column 0)
                    cursor_addr = SCREEN_MEM + 24 * 40

        # Update cursor position
        self.emulator.memory.write(CURSOR_PTR_LOW, cursor_addr & 0xFF)
        self.emulator.memory.write(CURSOR_PTR_HIGH, (cursor_addr >> 8) & 0xFF)

        # Also update row and column variables
        row = (cursor_addr - SCREEN_MEM) // 40
        col = (cursor_addr - SCREEN_MEM) % 40
        self.emulator.memory.write(CURSOR_ROW_ADDR, row)  # Cursor row
        self.emulator.memory.write(CURSOR_COL_ADDR, col)  # Cursor column

        # Update the text screen representation for display
        self.emulator._update_text_screen()

    def _get_cursor_position(self) -> Tuple[int, int, int]:
        """Return cursor row, column, and absolute address."""
        if not self.emulator:
            return 0, 0, SCREEN_MEM
        return self.emulator.get_cursor_position()

    def _set_cursor_position(self, row: int, col: int) -> None:
        """Update cursor position in zero-page variables."""
        if not self.emulator:
            return

        row = max(0, min(row, 24))
        col = max(0, min(col, 39))
        cursor_addr = SCREEN_MEM + row * 40 + col

        self.emulator.memory.write(CURSOR_PTR_LOW, cursor_addr & 0xFF)
        self.emulator.memory.write(CURSOR_PTR_HIGH, (cursor_addr >> 8) & 0xFF)
        self.emulator.memory.write(CURSOR_ROW_ADDR, row)
        self.emulator.memory.write(CURSOR_COL_ADDR, col)

    def _move_cursor_left(self) -> None:
        if not self.emulator:
            return

        row, col, _ = self._get_cursor_position()
        if col > 0:
            col -= 1
        elif row > 0:
            row -= 1
            col = 39
        self._set_cursor_position(row, col)

    def _move_cursor_right(self) -> None:
        if not self.emulator:
            return

        row, col, _ = self._get_cursor_position()
        if col < 39:
            col += 1
        elif row < 24:
            row += 1
            col = 0
        else:
            # Wrap past bottom-right with scroll
            self.emulator.memory._scroll_screen_up()
            row = 24
            col = 0
        self._set_cursor_position(row, col)

    def _move_cursor_up(self) -> None:
        if not self.emulator:
            return

        row, col, _ = self._get_cursor_position()
        if row > 0:
            row -= 1
        self._set_cursor_position(row, col)

    def _move_cursor_down(self) -> None:
        if not self.emulator:
            return

        row, col, _ = self._get_cursor_position()
        if row < 24:
            row += 1
        else:
            # Scroll when moving down at bottom row
            self.emulator.memory._scroll_screen_up()
            row = 24
        self._set_cursor_position(row, col)

    def _enqueue_keyboard_buffer(self, petscii_code: int) -> bool:
        if not self.emulator:
            return False
        # Delegate to emulator to keep buffer logic centralized.
        return self.emulator._enqueue_keyboard_buffer(petscii_code)

    def _remove_last_keyboard_buffer_char(self) -> bool:
        if not self.emulator:
            return False

        kb_buf_base = KEYBOARD_BUFFER_BASE
        kb_buf_len = self.emulator.memory.read(KEYBOARD_BUFFER_LEN_ADDR)
        if kb_buf_len <= 0:
            return False

        kb_buf_len -= 1
        self.emulator.memory.write(kb_buf_base + kb_buf_len, 0)
        self.emulator.memory.write(KEYBOARD_BUFFER_LEN_ADDR, kb_buf_len)
        return True

    def _clear_keyboard_buffer(self) -> None:
        if not self.emulator:
            return

        kb_buf_base = KEYBOARD_BUFFER_BASE
        self.emulator.memory.write(KEYBOARD_BUFFER_LEN_ADDR, 0)
        for i in range(10):
            self.emulator.memory.write(kb_buf_base + i, 0)

    def _is_line_edit_mode(self) -> bool:
        if not self.emulator:
            return False

        try:
            # Heuristic: CHRIN is used for keyboard input; this does not
            # fully represent the C64 screen editor state.
            return self.emulator.cpu.state.pc == 0xFFCF
        except Exception:
            return False

    def _read_screen_line_codes(self, row: int) -> List[int]:
        if not self.emulator:
            return []
        return self.emulator.read_screen_line_codes(row)

    def _extract_current_line_codes(self) -> List[int]:
        row, _, _ = self._get_cursor_position()
        line_codes = self._read_screen_line_codes(row)
        last_non_space = -1
        for i in range(39, -1, -1):
            if line_codes[i] != 0x20:
                last_non_space = i
                break
        if last_non_space == -1:
            return []
        return line_codes[:last_non_space + 1]

    def _codes_to_ascii(self, codes: List[int]) -> str:
        chars = []
        for code in codes:
            if 0x20 <= code <= 0x7E:
                chars.append(chr(code))
            else:
                chars.append(".")
        return "".join(chars)

    def _commit_current_line(self) -> None:
        if not self.emulator:
            return

        line_codes = self._extract_current_line_codes()
        max_line_len = 88  # 89 bytes total including CR
        if len(line_codes) > max_line_len:
            line_codes = line_codes[:max_line_len]
        line_codes.append(0x0D)

        for i in range(89):
            value = line_codes[i] if i < len(line_codes) else 0
            self.emulator.memory.write(INPUT_BUFFER_BASE + i, value)

        self.emulator.memory.write(INPUT_BUFFER_INDEX_ADDR, 0)  # Input buffer read index
        self.emulator.memory.write(INPUT_BUFFER_LEN_ADDR, len(line_codes))  # Input buffer length
        self._clear_keyboard_buffer()

        self.last_committed_line = self._codes_to_ascii(line_codes[:-1])
        self.add_debug_log(f"⌨️  Committed line: '{self.last_committed_line}' (len={len(line_codes)})")

    def _process_petscii_code(self, petscii_code: int, line_edit_mode: Optional[bool] = None) -> None:
        if not self.emulator:
            return

        if line_edit_mode is None:
            line_edit_mode = self._is_line_edit_mode()

        # Don't accept input while the machine is running something (not waiting in CHRIN).
        if not line_edit_mode:
            return

        # Cursor control codes
        if petscii_code == 0x9D:  # Cursor left
            self._move_cursor_left()
            return
        if petscii_code == 0x1D:  # Cursor right
            self._move_cursor_right()
            return
        if petscii_code == 0x91:  # Cursor up
            self._move_cursor_up()
            return
        if petscii_code == 0x11:  # Cursor down
            self._move_cursor_down()
            return

        if petscii_code == 0x14:  # Backspace/Delete
            self._handle_backspace()
            return

        if petscii_code == 0x0D:  # Enter / CR
            self._commit_current_line()
            # IMPORTANT: Do NOT locally echo CR here.
            # The edited line already exists on screen (we echoed keystrokes),
            # and the ROM/BASIC side will advance the line / print the next prompt.
            # Echoing it here results in a double line break and can confuse
            # the internal editor state.
            return

        if petscii_code == 0x93:  # Clear screen
            self._echo_character(0x93)
            return

        # Printable characters
        if 0x20 <= petscii_code <= 0xFF:
            # While waiting for input, we use local echo as a stand-in for the
            # full ROM screen editor/keyboard scanning path.
            self._echo_character(petscii_code)
            return

    def _handle_backspace(self) -> None:
        """Handle backspace - erase character at cursor and move cursor back"""
        if not self.emulator:
            return

        row, col, _ = self._get_cursor_position()

        # Don't backspace if we're at the start of screen
        if row == 0 and col == 0:
            return

        if col > 0:
            col -= 1
        else:
            row -= 1
            col = 39

        self._set_cursor_position(row, col)
        cursor_addr = SCREEN_MEM + row * 40 + col

        # Erase character at cursor position (write space)
        if SCREEN_MEM <= cursor_addr < SCREEN_MEM + 1000:
            self.emulator.memory.write(cursor_addr, 0x20)  # Space
            # Erase the color too (set to current text color for consistency)
            current_color = self.emulator.memory.read(0x0286) & 0x0F
            self.emulator.memory.write(COLOR_MEM + (cursor_addr - SCREEN_MEM), current_color)

        # Update the text screen representation for display
        self.emulator._update_text_screen()

    def on_key(self, event: Key) -> None:
        """Handle keyboard input and send to C64 keyboard buffer"""
        # Don't handle keys in fullscreen mode (or handle differently)
        if self.fullscreen:
            # In fullscreen, only allow quit
            if event.key == "ctrl+x" or event.key == "ctrl+q":
                self.action_quit()
            return

        # Handle special keys first
        if event.key == "ctrl+x" or event.key == "ctrl+q":
            self.action_quit()
            return
        elif event.key == "escape":
            # ESC might be used for something, but for now just ignore
            event.prevent_default()
            return

        # Only process keys when emulator is running
        if not self.emulator or not self.emulator.running:
            return

        # Only accept keys when the ROM is actually waiting for keyboard input.
        # This prevents local echo/cursor movement while programs are running.
        if not self._is_line_edit_mode():
            return

        if event.key == "left":
            self._move_cursor_left()
            event.prevent_default()
            return
        if event.key == "right":
            self._move_cursor_right()
            event.prevent_default()
            return
        if event.key == "up":
            self._move_cursor_up()
            event.prevent_default()
            return
        if event.key == "down":
            self._move_cursor_down()
            event.prevent_default()
            return

        if event.key == "backspace":
            self._process_petscii_code(0x14)
            event.prevent_default()
            return

        if event.key == "enter":
            self._process_petscii_code(0x0D)
            event.prevent_default()
            return

        # Check if character is printable
        if event.is_printable and event.character:
            char = event.character
            petscii_code = self._ascii_to_petscii(char)
            self._process_petscii_code(petscii_code)
            event.prevent_default()

    def handle_petscii_input(self, petscii_code: int) -> None:
        """Handle a PETSCII code injected programmatically."""
        self._process_petscii_code(petscii_code)


