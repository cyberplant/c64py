"""
Textual User Interface
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, List, Optional

from rich.console import Console
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.events import Key
from textual.widgets import Static, Header, Footer, RichLog

from .constants import SCREEN_MEM

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
        margin: 0 1;
        overflow-y: scroll;
        padding: 0 1;
        height: 35%;
    }

    #status-bar {
        border: solid $primary;
        margin: 0 1;
        padding: 0 1;
        height: 4;
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
        # Local input buffer for managing typed characters before sending to C64
        # This allows backspace to work correctly by removing characters before they're sent
        self.input_buffer = []  # List of PETSCII codes

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

            # Debug: Check if screen has any non-space content
            non_space_count = sum(1 for c in screen_content if c not in (' ', '\n'))
            if non_space_count > 0 and not hasattr(self, '_screen_debug_logged'):
                # Sample first few characters from screen memory
                sample_chars = []
                for addr in range(SCREEN_MEM, SCREEN_MEM + 20):
                    char_code = self.emulator.memory.read(addr)
                    sample_chars.append(f"${char_code:02X}")
                self.add_debug_log(f"📺 Screen has {non_space_count} non-space chars. First 20 bytes: {', '.join(sample_chars)}")
                self._screen_debug_logged = True

            # For RichLog, clear and write new content
            self.c64_display.clear()
            self.c64_display.write(screen_content)

            # Update status bar with actual cycle count from emulator (only in non-fullscreen mode)
            if not self.fullscreen:
                emu = self.emulator
                # Read cursor position from memory
                cursor_row = emu.memory.read(0xD3)
                cursor_col = emu.memory.read(0xD8)
                status_text = f"🎮 C64 | Cycle: {emu.current_cycles:,} | PC: ${emu.cpu.state.pc:04X} | A: ${emu.cpu.state.a:02X} | X: ${emu.cpu.state.x:02X} | Y: ${emu.cpu.state.y:02X} | SP: ${emu.cpu.state.sp:02X} | Cursor: {cursor_row},{cursor_col}"
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
        cursor_low = self.emulator.memory.read(0xD1)
        cursor_high = self.emulator.memory.read(0xD2)
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
            cursor_addr = SCREEN_MEM
        else:
            # Write character to screen
            if SCREEN_MEM <= cursor_addr < SCREEN_MEM + 1000:
                self.emulator.memory.write(cursor_addr, petscii_code)
                cursor_addr += 1
                # Handle wrapping/scrolling when reaching end of screen
                if cursor_addr >= SCREEN_MEM + 1000:
                    # At end of screen - scroll up and move to next line
                    self.emulator.memory._scroll_screen_up()
                    # Cursor moves to start of bottom row (row 24, column 0)
                    cursor_addr = SCREEN_MEM + 24 * 40

        # Update cursor position
        self.emulator.memory.write(0xD1, cursor_addr & 0xFF)
        self.emulator.memory.write(0xD2, (cursor_addr >> 8) & 0xFF)

        # Also update row and column variables
        row = (cursor_addr - SCREEN_MEM) // 40
        col = (cursor_addr - SCREEN_MEM) % 40
        self.emulator.memory.write(0xD3, row)  # Cursor row
        self.emulator.memory.write(0xD8, col)  # Cursor column

        # Update the text screen representation for display
        self.emulator._update_text_screen()

    def _handle_backspace(self) -> None:
        """Handle backspace - erase character at cursor and move cursor back"""
        if not self.emulator:
            return

        # Get cursor position
        cursor_low = self.emulator.memory.read(0xD1)
        cursor_high = self.emulator.memory.read(0xD2)
        cursor_addr = cursor_low | (cursor_high << 8)

        # If cursor is invalid, reset to screen start
        if cursor_addr < SCREEN_MEM or cursor_addr >= SCREEN_MEM + 1000:
            cursor_addr = SCREEN_MEM

        # Don't backspace if we're at the start of screen
        if cursor_addr <= SCREEN_MEM:
            return

        # Move cursor back one position
        cursor_addr -= 1

        # Handle wrapping to previous line if we're at the start of a line
        if (cursor_addr - SCREEN_MEM) % 40 == 39 and cursor_addr > SCREEN_MEM:
            # We wrapped to end of previous line - this shouldn't happen with simple backspace
            # But if it does, just stay at current position
            cursor_addr += 1
            return

        # Erase character at cursor position (write space)
        if SCREEN_MEM <= cursor_addr < SCREEN_MEM + 1000:
            self.emulator.memory.write(cursor_addr, 0x20)  # Space

        # Update cursor position
        self.emulator.memory.write(0xD1, cursor_addr & 0xFF)
        self.emulator.memory.write(0xD2, (cursor_addr >> 8) & 0xFF)

        # Also update row and column variables
        row = (cursor_addr - SCREEN_MEM) // 40
        col = (cursor_addr - SCREEN_MEM) % 40
        self.emulator.memory.write(0xD3, row)  # Cursor row
        self.emulator.memory.write(0xD8, col)  # Cursor column

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

        # Only process printable characters when emulator is running
        if not self.emulator or not self.emulator.running:
            return

        # Handle backspace - remove last character from buffer AND erase from screen
        # This handles both cases: character still in buffer, or already processed
        if event.key == "backspace":
            kb_buf_base = 0x0277
            kb_buf_len = self.emulator.memory.read(0xC6)

            if kb_buf_len > 0:
                # Character is still in buffer - remove it
                kb_buf_len -= 1
                self.emulator.memory.write(kb_buf_base + kb_buf_len, 0)
                self.emulator.memory.write(0xC6, kb_buf_len)
                self.add_debug_log(f"⌨️  Backspace (removed from buffer) -> buffer len={kb_buf_len}")

            # Always erase from screen and move cursor back
            # This handles the case where character was already read by BASIC
            self._handle_backspace()

            event.prevent_default()
            return

        # Check if character is printable
        if event.is_printable and event.character:
            char = event.character
            # Convert to PETSCII
            petscii_code = self._ascii_to_petscii(char)

            # Put character into keyboard buffer ($0277-$0280)
            kb_buf_base = 0x0277
            kb_buf_len = self.emulator.memory.read(0xC6)

            # Check if buffer has space (max 10 characters)
            if kb_buf_len < 10:
                # Add character to end of buffer
                self.emulator.memory.write(kb_buf_base + kb_buf_len, petscii_code)
                kb_buf_len += 1
                self.emulator.memory.write(0xC6, kb_buf_len)
                # Echo character to screen
                self._echo_character(petscii_code)
                self.add_debug_log(f"⌨️  Key pressed: '{char}' (PETSCII ${petscii_code:02X}) -> buffer len={kb_buf_len}")
            else:
                self.add_debug_log("⌨️  Keyboard buffer full, ignoring key")
        elif event.key == "enter":
            # Enter key = Carriage Return
            kb_buf_base = 0x0277
            kb_buf_len = self.emulator.memory.read(0xC6)
            if kb_buf_len < 10:
                self.emulator.memory.write(kb_buf_base + kb_buf_len, 0x0D)  # CR
                kb_buf_len += 1
                self.emulator.memory.write(0xC6, kb_buf_len)
                # Echo Enter (CR) to screen
                self._echo_character(0x0D)
                self.add_debug_log(f"⌨️  Enter pressed (CR) -> buffer len={kb_buf_len}")
            else:
                self.add_debug_log("⌨️  Keyboard buffer full, ignoring Enter")

        # Prevent default handling for most keys (so they go to C64, not Textual)
        if event.is_printable or event.key == "enter":
            event.prevent_default()


