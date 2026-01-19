"""
Pygame graphics interface for the C64 emulator.
"""

from __future__ import annotations

import threading
from typing import List, Optional, Tuple, TYPE_CHECKING

from .constants import (
    BASIC_BOOT_CYCLES,
    BLNSW,
    COLOR_MEM,
    CURSOR_COL_ADDR,
    CURSOR_ROW_ADDR,
    KERNAL_CHRIN_ADDR,
    ROM_KERNAL_START,
    ROM_KERNAL_END,
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
            self._queue_petscii(0x9D)
            return
        if event.key == pygame.K_RIGHT:
            self._queue_petscii(0x1D)
            return
        if event.key == pygame.K_UP:
            self._queue_petscii(0x91)
            return
        if event.key == pygame.K_DOWN:
            self._queue_petscii(0x11)
            return
        if event.key == pygame.K_BACKSPACE:
            self._queue_petscii(0x14)
            return
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self._queue_petscii(0x0D)
            return

        if event.unicode and event.unicode.isprintable():
            petscii_code = self._ascii_to_petscii(event.unicode)
            self._queue_petscii(petscii_code)

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
                            # Inject "RUN" command into keyboard buffer for autorun
                            self.emulator._inject_run_command()
                        except Exception as exc:
                            self.add_debug_log(f"Failed to load program: {exc}")
                            self.emulator.prg_file_path = None

                step_cycles = self.emulator.cpu.step(self.emulator.udp_debug, cycles)
                cycles += step_cycles
                self.emulator.current_cycles = cycles

                pc = self.emulator.cpu.state.pc
                if pc == last_pc:
                    if self.emulator.memory.kernal_rom and ROM_KERNAL_START <= pc < ROM_KERNAL_END:
                        stuck_count = 0
                    elif pc != KERNAL_CHRIN_ADDR:
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

        # Cursor blinking is now handled by KERNAL IRQ at $EA31, which modifies
        # screen memory directly (XORs character with $80 to reverse it).
        # When reversed (cursor visible), use current text color ($0286) as background.
        cursor_color = mem[0x0286] & 0x0F  # Current text color for cursor

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
                    # Reversed character (cursor): background is current text color,
                    # foreground (glyph) is screen background color
                    cursor_bg = self._palette.get(cursor_color, (255, 255, 255))
                    self._frame_surface.fill(cursor_bg, (x, y, self.CHAR_WIDTH, self.CHAR_HEIGHT))
                    glyph_index = (glyph_base + code) % glyph_count
                    glyph = self._glyph_surfaces[glyph_index][bg_code]
                else:
                    glyph_index = (glyph_base + code) % glyph_count
                    glyph = self._glyph_surfaces[glyph_index][color_code]
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

    def _queue_petscii(self, petscii_code: int) -> None:
        if not self.emulator:
            return
        if not self.emulator.send_petscii(petscii_code & 0xFF):
            self.add_debug_log("Keyboard buffer full, ignoring key")
