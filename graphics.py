"""
Pygame graphics interface for the C64 emulator.
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional, Tuple, TYPE_CHECKING

from .constants import (
    BASIC_BOOT_CYCLES,
    BASIC_INPUT_BUFFER_SIZE,
    BASIC_MAX_LINE_LENGTH,
    COLOR_MEM,
    CURSOR_COL_ADDR,
    CURSOR_PTR_HIGH,
    CURSOR_PTR_LOW,
    CURSOR_ROW_ADDR,
    INPUT_BUFFER_BASE,
    INPUT_BUFFER_INDEX_ADDR,
    INPUT_BUFFER_LEN_ADDR,
    KEYBOARD_BUFFER_BASE,
    KEYBOARD_BUFFER_LEN_ADDR,
    KEYBOARD_BUFFER_SIZE,
    KERNAL_CHRIN_ADDR,
    SCREEN_MEM,
    SCREEN_COLS as C64_SCREEN_COLS,
    SCREEN_ROWS as C64_SCREEN_ROWS,
    SCREEN_SIZE as C64_SCREEN_SIZE,
    STUCK_PC_THRESHOLD,
    VIC_MEMORY_CONTROL_REG,
)

if TYPE_CHECKING:
    from .emulator import C64


class PygameInterface:
    """Pygame-based graphics UI for the C64 emulator.

    Owns the pygame window, handles input, and renders the emulator screen.
    The main event loop runs in the caller thread while CPU execution runs
    on a background thread started by `run()`.
    """

    CHAR_WIDTH = 8
    CHAR_HEIGHT = 8
    SCREEN_COLS = C64_SCREEN_COLS
    SCREEN_ROWS = C64_SCREEN_ROWS
    SCREEN_SIZE = C64_SCREEN_SIZE
    DEFAULT_BORDER = 32

    def __init__(
        self,
        emulator: "C64",
        max_cycles: Optional[int] = None,
        scale: int = 2,
        fps: int = 30,
        border_size: Optional[int] = None,
    ) -> None:
        self.emulator = emulator
        self.max_cycles = max_cycles
        self.scale = max(1, int(scale))
        self.fps = max(1, int(fps))
        self.border_size = self.DEFAULT_BORDER if border_size is None else max(0, int(border_size))

        self.running = False
        self.emulator_thread = None
        self.cursor_blink_interval = 0.5
        self.cursor_blink_on = True
        self.cursor_blink_last_toggle = time.monotonic()
        self.last_committed_line = ""
        self.max_logs = 1000
        self._log_messages: List[str] = []

        self._pygame = None
        self._display_surface = None
        self._frame_surface = None
        self._screen_rect = None
        self._native_size: Optional[Tuple[int, int]] = None
        self._display_size: Optional[Tuple[int, int]] = None
        self._glyph_surfaces = None
        self._glyph_rom_id = None

        self._palette = {
            0: (0, 0, 0),
            1: (255, 255, 255),
            2: (136, 0, 0),
            3: (170, 255, 238),
            4: (204, 68, 204),
            5: (0, 204, 85),
            6: (0, 0, 170),
            7: (238, 238, 119),
            8: (221, 136, 85),
            9: (102, 68, 0),
            10: (255, 119, 119),
            11: (51, 51, 51),
            12: (119, 119, 119),
            13: (170, 255, 102),
            14: (0, 136, 255),
            15: (187, 187, 187),
        }

    def add_debug_log(self, message: str) -> None:
        from datetime import datetime

        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {message}"
        self._log_messages.append(formatted_message)
        if len(self._log_messages) > self.max_logs:
            self._log_messages.pop(0)
        print(formatted_message)

    def _get_last_log_lines(self, count: int = 20) -> List[str]:
        if not self._log_messages:
            return []
        return self._log_messages[-count:] if len(self._log_messages) > count else list(self._log_messages)

    def run(self) -> None:
        """Start the pygame event loop and render C64 output."""
        try:
            import pygame
        except ImportError as exc:
            raise RuntimeError("Pygame is required for --graphics mode") from exc

        self._pygame = pygame
        pygame.init()
        pygame.display.set_caption("C64 Emulator (Graphics)")
        self._setup_surfaces()

        self.running = True
        self.emulator.running = True
        self.emulator_thread = threading.Thread(target=self._run_emulator, daemon=True)
        self.emulator_thread.start()

        clock = pygame.time.Clock()
        try:
            while self.running:
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        self._request_quit()
                    elif event.type == pygame.KEYDOWN:
                        self._handle_keydown(event)

                if self.emulator and not self.emulator.running:
                    self.running = False

                self._render_frame()
                if self.scale == 1:
                    self._display_surface.blit(self._frame_surface, (0, 0))
                else:
                    pygame.transform.scale(self._frame_surface, self._display_size, self._display_surface)
                pygame.display.flip()
                clock.tick(self.fps)
        finally:
            self.running = False
            if self.emulator:
                self.emulator.running = False
            if self.emulator_thread and self.emulator_thread.is_alive():
                self.emulator_thread.join()
            pygame.quit()

    def _setup_surfaces(self) -> None:
        screen_w = self.SCREEN_COLS * self.CHAR_WIDTH
        screen_h = self.SCREEN_ROWS * self.CHAR_HEIGHT
        native_w = screen_w + self.border_size * 2
        native_h = screen_h + self.border_size * 2
        self._native_size = (native_w, native_h)
        self._display_size = (native_w * self.scale, native_h * self.scale)
        self._display_surface = self._pygame.display.set_mode(self._display_size)
        self._frame_surface = self._pygame.Surface(self._native_size)
        self._screen_rect = self._pygame.Rect(self.border_size, self.border_size, screen_w, screen_h)

    def _request_quit(self) -> None:
        self.running = False
        if self.emulator:
            self.emulator.running = False

    def _handle_keydown(self, event) -> None:
        pygame = self._pygame
        if event.mod & pygame.KMOD_CTRL:
            if event.key in (pygame.K_x, pygame.K_q):
                self._request_quit()
                return

        if not self.emulator or not self.emulator.running:
            return

        if event.key == pygame.K_LEFT:
            self._process_petscii_code(0x9D)
            return
        if event.key == pygame.K_RIGHT:
            self._process_petscii_code(0x1D)
            return
        if event.key == pygame.K_UP:
            self._process_petscii_code(0x91)
            return
        if event.key == pygame.K_DOWN:
            self._process_petscii_code(0x11)
            return
        if event.key == pygame.K_BACKSPACE:
            self._process_petscii_code(0x14)
            return
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self._process_petscii_code(0x0D)
            return

        if event.unicode and event.unicode.isprintable():
            petscii_code = self._ascii_to_petscii(event.unicode)
            self._process_petscii_code(petscii_code)

    def _run_emulator(self) -> None:
        """Run the emulator CPU loop on a background thread."""
        try:
            self.emulator.running = True
            cycles = 0
            max_cycles = self.max_cycles
            last_pc = None
            stuck_count = 0

            while self.emulator.running:
                if max_cycles is not None and cycles >= max_cycles:
                    if hasattr(self.emulator, "autoquit") and self.emulator.autoquit:
                        self.emulator.running = False
                    break

                if self.emulator.prg_file_path and not hasattr(self.emulator, "_program_loaded_after_boot"):
                    # BASIC init takes roughly this many cycles before the prompt is ready.
                    if cycles > BASIC_BOOT_CYCLES:
                        try:
                            self.emulator.load_prg(self.emulator.prg_file_path)
                            self.emulator.prg_file_path = None
                            self.emulator._program_loaded_after_boot = True
                            self.add_debug_log("Program loaded after BASIC boot completed")
                        except Exception as exc:
                            self.add_debug_log(f"Failed to load program: {exc}")
                            self.emulator.prg_file_path = None

                step_cycles = self.emulator.cpu.step(self.emulator.udp_debug, cycles)
                cycles += step_cycles
                self.emulator.current_cycles = cycles

                pc = self.emulator.cpu.state.pc
                if pc == last_pc:
                    if pc != KERNAL_CHRIN_ADDR:
                        stuck_count += 1
                        if stuck_count > STUCK_PC_THRESHOLD:
                            self.add_debug_log(f"PC stuck at ${pc:04X} for {stuck_count} steps - stopping")
                            self.emulator.running = False
                            break
                    else:
                        stuck_count = 0
                else:
                    stuck_count = 0
                last_pc = pc

            if max_cycles is not None and cycles >= max_cycles:
                self.add_debug_log(f"Stopped at cycle {cycles} (reached max_cycles={max_cycles})")
            else:
                self.add_debug_log(f"Stopped at cycle {cycles} (stuck_count={stuck_count})")
        except Exception as exc:
            self.add_debug_log(f"Emulator error ({type(exc).__name__}): {exc}")

    def _build_glyph_surfaces(self) -> None:
        char_rom = self.emulator.memory.char_rom
        if not char_rom:
            return

        if self._glyph_rom_id == id(char_rom):
            return

        pygame = self._pygame
        glyph_count = len(char_rom) // 8
        glyph_surfaces = []
        for glyph_index in range(glyph_count):
            rows = char_rom[glyph_index * 8 : (glyph_index + 1) * 8]
            color_surfaces = []
            for color_index in range(16):
                surface = pygame.Surface((self.CHAR_WIDTH, self.CHAR_HEIGHT), flags=pygame.SRCALPHA)
                fg = self._palette[color_index]
                for y in range(self.CHAR_HEIGHT):
                    row_bits = rows[y]
                    for x in range(self.CHAR_WIDTH):
                        if row_bits & (1 << (7 - x)):
                            surface.set_at((x, y), (*fg, 255))
                color_surfaces.append(surface)
            glyph_surfaces.append(color_surfaces)

        self._glyph_surfaces = glyph_surfaces
        self._glyph_rom_id = id(char_rom)

    def _get_charset_offset(self) -> int:
        if not hasattr(self.emulator.memory, "_vic_regs"):
            return 0
        regs = self.emulator.memory._vic_regs
        if len(regs) <= VIC_MEMORY_CONTROL_REG:
            return 0
        char_addr = (regs[VIC_MEMORY_CONTROL_REG] & 0x0E) << 10
        return 0x800 if (char_addr & 0x0800) else 0

    def _petscii_to_screen_code(self, petscii_char: int) -> int:
        return self.emulator._petscii_to_screen_code(petscii_char)

    def _render_frame(self) -> None:
        """Render one frame of the C64 text screen into the back buffer."""
        bg_code = self.emulator.memory.read(0xD021) & 0x0F
        border_code = self.emulator.memory.read(0xD020) & 0x0F
        bg_color = self._palette.get(bg_code, (0, 0, 0))
        border_color = self._palette.get(border_code, (0, 0, 0))

        self._frame_surface.fill(border_color)
        self._frame_surface.fill(bg_color, self._screen_rect)

        if not self._glyph_surfaces:
            self._build_glyph_surfaces()
        if not self._glyph_surfaces:
            return

        mem = self.emulator.memory.ram
        screen_base = SCREEN_MEM
        color_base = COLOR_MEM
        screen_left = self._screen_rect.left
        screen_top = self._screen_rect.top
        charset_offset = self._get_charset_offset()
        glyph_base = charset_offset >> 3
        glyph_count = len(self._glyph_surfaces)
        max_row_index = self.SCREEN_ROWS - 1
        max_col_index = self.SCREEN_COLS - 1

        now = time.monotonic()
        if now - self.cursor_blink_last_toggle >= self.cursor_blink_interval:
            self.cursor_blink_on = not self.cursor_blink_on
            self.cursor_blink_last_toggle = now

        for row in range(self.SCREEN_ROWS):
            row_offset = row * self.SCREEN_COLS
            y = screen_top + row * self.CHAR_HEIGHT
            for col in range(self.SCREEN_COLS):
                idx = row_offset + col
                raw_code = mem[screen_base + idx]
                color_code = mem[color_base + idx] & 0x0F
                reverse = False
                if raw_code & 0x80:
                    reverse = True
                    raw_code &= 0x7F
                code = self._petscii_to_screen_code(raw_code)

                x = screen_left + col * self.CHAR_WIDTH
                if reverse:
                    fg_color = self._palette.get(color_code, (255, 255, 255))
                    self._frame_surface.fill(fg_color, (x, y, self.CHAR_WIDTH, self.CHAR_HEIGHT))
                    glyph_index = (glyph_base + code) % glyph_count
                    glyph = self._glyph_surfaces[glyph_index][bg_code]
                else:
                    glyph_index = (glyph_base + code) % glyph_count
                    glyph = self._glyph_surfaces[glyph_index][color_code]
                self._frame_surface.blit(glyph, (x, y))

        if self.cursor_blink_on:
            cursor_row = mem[CURSOR_ROW_ADDR]
            cursor_col = mem[CURSOR_COL_ADDR]
            cursor_row = max(0, min(cursor_row, max_row_index))
            cursor_col = max(0, min(cursor_col, max_col_index))
            idx = cursor_row * self.SCREEN_COLS + cursor_col
            raw_code = mem[screen_base + idx] & 0x7F
            code = self._petscii_to_screen_code(raw_code)
            color_code = mem[color_base + idx] & 0x0F
            x = screen_left + cursor_col * self.CHAR_WIDTH
            y = screen_top + cursor_row * self.CHAR_HEIGHT
            fg_color = self._palette.get(color_code, (255, 255, 255))
            self._frame_surface.fill(fg_color, (x, y, self.CHAR_WIDTH, self.CHAR_HEIGHT))
            glyph_index = (glyph_base + code) % glyph_count
            glyph = self._glyph_surfaces[glyph_index][bg_code]
            self._frame_surface.blit(glyph, (x, y))

    def _ascii_to_petscii(self, char: str) -> int:
        if not char:
            return 0
        ascii_code = ord(char)
        if 0x20 <= ascii_code <= 0x5F:
            return ascii_code
        if 0x61 <= ascii_code <= 0x7A:
            # C64 keyboard input maps lowercase to uppercase PETSCII.
            return ascii_code - 0x20
        if ascii_code in (0x0D, 0x0A):
            return 0x0D
        return ascii_code & 0xFF

    def _echo_character(self, petscii_code: int) -> None:
        if not self.emulator:
            return

        if not 0 <= petscii_code <= 0xFF:
            return

        screen_size = self.SCREEN_SIZE
        max_row_index = self.SCREEN_ROWS - 1
        cursor_low = self.emulator.memory.read(CURSOR_PTR_LOW)
        cursor_high = self.emulator.memory.read(CURSOR_PTR_HIGH)
        cursor_addr = cursor_low | (cursor_high << 8)

        if cursor_addr < SCREEN_MEM or cursor_addr >= SCREEN_MEM + screen_size:
            cursor_addr = SCREEN_MEM

        if petscii_code == 0x0D:
            row = (cursor_addr - SCREEN_MEM) // self.SCREEN_COLS
            if row < max_row_index:
                cursor_addr = SCREEN_MEM + (row + 1) * self.SCREEN_COLS
            else:
                self.emulator.memory._scroll_screen_up()
                cursor_addr = SCREEN_MEM + max_row_index * self.SCREEN_COLS
        elif petscii_code == 0x0A:
            return
        elif petscii_code == 0x93:
            for addr in range(SCREEN_MEM, SCREEN_MEM + screen_size):
                self.emulator.memory.write(addr, 0x20)
            cursor_addr = SCREEN_MEM
        else:
            if SCREEN_MEM <= cursor_addr < SCREEN_MEM + screen_size:
                self.emulator.memory.write(cursor_addr, petscii_code)
                cursor_addr += 1
                if cursor_addr >= SCREEN_MEM + screen_size:
                    self.emulator.memory._scroll_screen_up()
                    cursor_addr = SCREEN_MEM + max_row_index * self.SCREEN_COLS

        self.emulator.memory.write(CURSOR_PTR_LOW, cursor_addr & 0xFF)
        self.emulator.memory.write(CURSOR_PTR_HIGH, (cursor_addr >> 8) & 0xFF)

        row = (cursor_addr - SCREEN_MEM) // self.SCREEN_COLS
        col = (cursor_addr - SCREEN_MEM) % self.SCREEN_COLS
        self.emulator.memory.write(CURSOR_ROW_ADDR, row)
        self.emulator.memory.write(CURSOR_COL_ADDR, col)

        self.emulator._update_text_screen()

    def _get_cursor_position(self) -> Tuple[int, int, int]:
        if not self.emulator:
            return 0, 0, SCREEN_MEM
        return self.emulator.get_cursor_position()

    def _set_cursor_position(self, row: int, col: int) -> None:
        if not self.emulator:
            return

        max_row_index = self.SCREEN_ROWS - 1
        max_col_index = self.SCREEN_COLS - 1
        row = max(0, min(row, max_row_index))
        col = max(0, min(col, max_col_index))
        cursor_addr = SCREEN_MEM + row * self.SCREEN_COLS + col

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
            col = self.SCREEN_COLS - 1
        self._set_cursor_position(row, col)

    def _move_cursor_right(self) -> None:
        if not self.emulator:
            return

        row, col, _ = self._get_cursor_position()
        max_row_index = self.SCREEN_ROWS - 1
        max_col_index = self.SCREEN_COLS - 1
        if col < max_col_index:
            col += 1
        elif row < max_row_index:
            row += 1
            col = 0
        else:
            self.emulator.memory._scroll_screen_up()
            row = max_row_index
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
        max_row_index = self.SCREEN_ROWS - 1
        if row < max_row_index:
            row += 1
        else:
            self.emulator.memory._scroll_screen_up()
            row = max_row_index
        self._set_cursor_position(row, col)

    def _enqueue_keyboard_buffer(self, petscii_code: int) -> bool:
        if not self.emulator:
            return False
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
        for i in range(KEYBOARD_BUFFER_SIZE):
            self.emulator.memory.write(kb_buf_base + i, 0)

    def _is_line_edit_mode(self) -> bool:
        if not self.emulator:
            return False

        try:
            return self.emulator.cpu.state.pc == KERNAL_CHRIN_ADDR
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
        for i in range(self.SCREEN_COLS - 1, -1, -1):
            if line_codes[i] != 0x20:
                last_non_space = i
                break
        if last_non_space == -1:
            return []
        return line_codes[: last_non_space + 1]

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
        max_line_len = BASIC_MAX_LINE_LENGTH
        if len(line_codes) > max_line_len:
            line_codes = line_codes[:max_line_len]
        line_codes.append(0x0D)

        for i in range(BASIC_INPUT_BUFFER_SIZE):
            value = line_codes[i] if i < len(line_codes) else 0
            self.emulator.memory.write(INPUT_BUFFER_BASE + i, value)

        self.emulator.memory.write(INPUT_BUFFER_INDEX_ADDR, 0)
        self.emulator.memory.write(INPUT_BUFFER_LEN_ADDR, len(line_codes))
        self._clear_keyboard_buffer()

        self.last_committed_line = self._codes_to_ascii(line_codes[:-1])
        self.add_debug_log(f"Committed line: '{self.last_committed_line}' (len={len(line_codes)})")

    def _process_petscii_code(self, petscii_code: int, line_edit_mode: Optional[bool] = None) -> None:
        if not self.emulator:
            return

        if not 0 <= petscii_code <= 0xFF:
            return

        if line_edit_mode is None:
            line_edit_mode = self._is_line_edit_mode()

        if petscii_code == 0x9D:
            self._move_cursor_left()
            return
        if petscii_code == 0x1D:
            self._move_cursor_right()
            return
        if petscii_code == 0x91:
            self._move_cursor_up()
            return
        if petscii_code == 0x11:
            self._move_cursor_down()
            return

        if petscii_code == 0x14:
            if line_edit_mode:
                self._handle_backspace()
            else:
                if self._remove_last_keyboard_buffer_char():
                    self.add_debug_log("Backspace (removed from buffer)")
                self._handle_backspace()
            return

        if petscii_code == 0x0D:
            if line_edit_mode:
                self._commit_current_line()
            else:
                if not self._enqueue_keyboard_buffer(petscii_code):
                    self.add_debug_log("Keyboard buffer full, ignoring Enter")
            self._echo_character(0x0D)
            return

        if petscii_code == 0x93:
            self._echo_character(0x93)
            return

        if 0x20 <= petscii_code <= 0xFF:
            if line_edit_mode:
                self._echo_character(petscii_code)
            else:
                if self._enqueue_keyboard_buffer(petscii_code):
                    self._echo_character(petscii_code)
                else:
                    self.add_debug_log("Keyboard buffer full, ignoring key")
            return

    def _handle_backspace(self) -> None:
        if not self.emulator:
            return

        row, col, _ = self._get_cursor_position()
        if row == 0 and col == 0:
            return

        if col > 0:
            col -= 1
        else:
            row -= 1
            col = self.SCREEN_COLS - 1

        self._set_cursor_position(row, col)
        cursor_addr = SCREEN_MEM + row * self.SCREEN_COLS + col

        if SCREEN_MEM <= cursor_addr < SCREEN_MEM + self.SCREEN_SIZE:
            self.emulator.memory.write(cursor_addr, 0x20)

        self.emulator._update_text_screen()

    def handle_petscii_input(self, petscii_code: int) -> None:
        """Handle a single PETSCII input code (0-255)."""
        self._process_petscii_code(petscii_code)
