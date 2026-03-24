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
        # Preserve any mixer settings already applied by the SID emulator.
        # pygame.init() would reset the mixer to stereo defaults otherwise.
        mixer = getattr(pygame, "mixer", None)
        if mixer is None:
            pygame.init()
        else:
            try:
                mixer_initialized = mixer.get_init()
            except Exception:
                mixer_initialized = None
            if not mixer_initialized:
                pygame.init()
            else:
                for mod in (pygame.display, pygame.font, pygame.joystick, pygame.event):
                    try:
                        mod.init()
                    except Exception:
                        pass
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
                # Explicitly shutdown SID and other background tasks before pygame.quit()
                # to avoid race conditions with audio threads calling into a deinitialized mixer.
                self.emulator.shutdown()
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
            self.emulator.reset_speed_throttle()
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
                self.emulator.throttle_emulation_if_needed(cycles)

                pc = self.emulator.cpu.state.pc
                if pc == last_pc:
                    # Check if we're in a graphics mode wait loop
                    # A simple JMP * (4C XX XX where XX XX = current PC) is ok in graphics mode
                    mode_info = self.emulator.memory.get_display_mode()
                    in_graphics_mode = mode_info['bitmap_mode'] or self.emulator.memory.is_sprite_enabled(0)
                    
                    if self.emulator.memory.kernal_rom and ROM_KERNAL_START <= pc < ROM_KERNAL_END:
                        stuck_count = 0
                    elif pc != KERNAL_CHRIN_ADDR:
                        # Check if it's a simple wait loop (JMP to self or nearby)
                        opcode = self.emulator.memory.read(pc)
                        if opcode == 0x4C:  # JMP absolute
                            target_low = self.emulator.memory.read(pc + 1)
                            target_high = self.emulator.memory.read(pc + 2)
                            target = target_low | (target_high << 8)
                            # Allow JMP * in graphics mode (infinite wait loop)
                            if in_graphics_mode and abs(target - pc) <= 10:
                                stuck_count = 0
                            else:
                                stuck_count += 1
                        else:
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
        """Render one frame of the C64 screen into the back buffer.
        
        Supports both text and bitmap modes based on VIC-II registers.
        """
        # Get display mode information
        mode_info = self.emulator.memory.get_display_mode()
        
        # Read background and border colors
        bg_code = self.emulator.memory.read(0xD021) & 0x0F
        border_code = self.emulator.memory.read(0xD020) & 0x0F
        bg_color = self._palette.get(bg_code, (0, 0, 0))
        border_color = self._palette.get(border_code, (0, 0, 0))

        # Fill border and background
        self._frame_surface.fill(border_color)
        self._frame_surface.fill(bg_color, self._screen_rect)
        
        # Render based on display mode
        if mode_info['bitmap_mode']:
            self._render_bitmap_mode(mode_info)
        else:
            self._render_text_mode(mode_info)
        
        # Render sprites on top
        self._render_sprites()
    
    def _render_text_mode(self, mode_info: dict) -> None:
        """Render text mode display."""
        if not self._glyph_surfaces:
            self._build_glyph_surfaces()
        if not self._glyph_surfaces:
            return

        mem = self.emulator.memory.ram
        screen_base = mode_info['screen_base']
        color_base = COLOR_MEM
        screen_left = self._screen_rect.left
        screen_top = self._screen_rect.top
        charset_offset = self._get_charset_offset()
        glyph_base = charset_offset >> 3
        glyph_count = len(self._glyph_surfaces)
        
        # Get background colors for extended color mode
        bg_colors = [
            self.emulator.memory.read(0xD021) & 0x0F,
            self.emulator.memory.read(0xD022) & 0x0F,
            self.emulator.memory.read(0xD023) & 0x0F,
            self.emulator.memory.read(0xD024) & 0x0F,
        ]

        # Cursor color
        cursor_color = mem[0x0286] & 0x0F

        for row in range(self.SCREEN_ROWS):
            row_offset = row * self.SCREEN_COLS
            y = screen_top + row * self.CHAR_HEIGHT
            for col in range(self.SCREEN_COLS):
                idx = row_offset + col
                raw_code = mem[screen_base + idx]
                color_code = mem[color_base + idx] & 0x0F
                reverse = False
                
                # Handle reversed characters (cursor)
                if raw_code & 0x80:
                    reverse = True
                    raw_code &= 0x7F
                
                code = self._petscii_to_screen_code(raw_code)
                x = screen_left + col * self.CHAR_WIDTH
                
                # Handle extended color mode
                if mode_info['extended_color']:
                    bg_index = (code >> 6) & 0x03
                    code &= 0x3F
                    char_bg_color = self._palette.get(bg_colors[bg_index], (0, 0, 0))
                    self._frame_surface.fill(char_bg_color, (x, y, self.CHAR_WIDTH, self.CHAR_HEIGHT))
                
                if reverse:
                    # Reversed character (cursor)
                    cursor_bg = self._palette.get(cursor_color, (255, 255, 255))
                    self._frame_surface.fill(cursor_bg, (x, y, self.CHAR_WIDTH, self.CHAR_HEIGHT))
                    glyph_index = (glyph_base + code) % glyph_count
                    glyph = self._glyph_surfaces[glyph_index][bg_colors[0]]
                else:
                    glyph_index = (glyph_base + code) % glyph_count
                    glyph = self._glyph_surfaces[glyph_index][color_code]
                
                self._frame_surface.blit(glyph, (x, y))
    
    def _render_bitmap_mode(self, mode_info: dict) -> None:
        """Render bitmap mode display (standard or multicolor)."""
        mem = self.emulator.memory.ram
        bitmap_base = mode_info['bitmap_base']
        screen_base = mode_info['screen_base']
        screen_left = self._screen_rect.left
        screen_top = self._screen_rect.top
        
        # 320x200 pixels, organized as 40x25 character blocks (8x8 pixels each)
        for char_row in range(25):
            for char_col in range(40):
                char_index = char_row * 40 + char_col
                
                # Get color data from screen memory
                color_data = mem[screen_base + char_index]
                color_mem = mem[COLOR_MEM + char_index] & 0x0F
                
                # Get bitmap data for this 8x8 block
                # Each char is 8 bytes (8 rows of 8 pixels)
                bitmap_offset = char_index * 8
                
                if mode_info['multicolor']:
                    # Multicolor bitmap mode: 160x200, 4x8 pixel blocks
                    # Each pair of bits = one color
                    # 00 = background color ($D021)
                    # 01 = upper nibble of screen RAM
                    # 10 = lower nibble of screen RAM  
                    # 11 = color RAM
                    bg_color = self.emulator.memory.read(0xD021) & 0x0F
                    color1 = (color_data >> 4) & 0x0F
                    color2 = color_data & 0x0F
                    color3 = color_mem
                    
                    for row in range(8):
                        byte = mem[bitmap_base + bitmap_offset + row]
                        y = screen_top + char_row * 8 + row
                        
                        # Process 4 pixel pairs (8 pixels total, but 4 wide pixels)
                        for bit_pair in range(4):
                            pixel_bits = (byte >> (6 - bit_pair * 2)) & 0x03
                            
                            # Select color based on bit pair
                            if pixel_bits == 0:
                                pixel_color = self._palette.get(bg_color, (0, 0, 0))
                            elif pixel_bits == 1:
                                pixel_color = self._palette.get(color1, (0, 0, 0))
                            elif pixel_bits == 2:
                                pixel_color = self._palette.get(color2, (0, 0, 0))
                            else:  # pixel_bits == 3
                                pixel_color = self._palette.get(color3, (0, 0, 0))
                            
                            # Draw double-wide pixel
                            x = screen_left + char_col * 8 + bit_pair * 2
                            self._frame_surface.fill(pixel_color, (x, y, 2, 1))
                else:
                    # Standard hi-res bitmap mode: 320x200
                    # 1 = upper nibble of screen RAM
                    # 0 = lower nibble of screen RAM
                    color1 = (color_data >> 4) & 0x0F
                    color0 = color_data & 0x0F
                    
                    for row in range(8):
                        byte = mem[bitmap_base + bitmap_offset + row]
                        y = screen_top + char_row * 8 + row
                        
                        # Process 8 pixels
                        for bit in range(8):
                            pixel_bit = (byte >> (7 - bit)) & 0x01
                            pixel_color = self._palette.get(color1 if pixel_bit else color0, (0, 0, 0))
                            
                            x = screen_left + char_col * 8 + bit
                            self._frame_surface.set_at((x, y), pixel_color)
    
    def _render_sprites(self) -> None:
        """Render sprites on top of the display."""
        pygame = self._pygame
        if not pygame:
            return
        
        mem = self.emulator.memory.ram
        screen_left = self._screen_rect.left
        screen_top = self._screen_rect.top
        
        # Sprite multicolor shared colors
        sprite_mc0 = self.emulator.memory.read(0xD025) & 0x0F
        sprite_mc1 = self.emulator.memory.read(0xD026) & 0x0F
        
        # Render sprites 0-7 (back to front)
        for sprite_num in range(8):
            sprite_data = self.emulator.memory.get_sprite_data(sprite_num)
            
            if not sprite_data['enabled']:
                continue
            
            # Get sprite bitmap data (63 bytes per sprite)
            sprite_ptr = sprite_data['pointer']
            sprite_addr = sprite_ptr * 64
            
            # Sprites are 24x21 pixels
            # Offset sprite coordinates to screen space
            # VIC-II sprite coordinates are relative to the display area, not the border
            # Subtract 24 pixels for X to align with display area (standard C64 offset)
            # Subtract 50 pixels for Y to align with display area (standard C64 offset)
            sprite_x = sprite_data['x'] - 24
            sprite_y = sprite_data['y'] - 50
            sprite_color = self._palette.get(sprite_data['color'], (255, 255, 255))
            
            # Render sprite pixels
            if sprite_data['multicolor']:
                # Multicolor sprite: 12x21 (double-wide pixels)
                for row in range(21):
                    # Each row is 3 bytes
                    byte_offset = sprite_addr + row * 3
                    if byte_offset + 2 >= len(mem):
                        continue
                    
                    row_data = (mem[byte_offset] << 16) | (mem[byte_offset + 1] << 8) | mem[byte_offset + 2]
                    
                    for bit_pair in range(12):
                        pixel_bits = (row_data >> (22 - bit_pair * 2)) & 0x03
                        
                        if pixel_bits == 0:
                            continue  # Transparent
                        elif pixel_bits == 1:
                            color = self._palette.get(sprite_mc0, (0, 0, 0))
                        elif pixel_bits == 2:
                            color = sprite_color
                        else:  # pixel_bits == 3
                            color = self._palette.get(sprite_mc1, (0, 0, 0))
                        
                        # Draw double-wide pixel
                        px = screen_left + sprite_x + bit_pair * 2
                        py = screen_top + sprite_y + row
                        # Check if pixel is within screen rect bounds
                        if (self._screen_rect.left <= px < self._screen_rect.right and 
                            self._screen_rect.top <= py < self._screen_rect.bottom):
                            self._frame_surface.fill(color, (px, py, 2, 1))
            else:
                # Hi-res sprite: 24x21
                for row in range(21):
                    byte_offset = sprite_addr + row * 3
                    if byte_offset + 2 >= len(mem):
                        continue
                    
                    row_data = (mem[byte_offset] << 16) | (mem[byte_offset + 1] << 8) | mem[byte_offset + 2]
                    
                    for bit in range(24):
                        pixel_bit = (row_data >> (23 - bit)) & 0x01
                        
                        if pixel_bit:
                            px = screen_left + sprite_x + bit
                            py = screen_top + sprite_y + row
                            # Check if pixel is within screen rect bounds
                            if (self._screen_rect.left <= px < self._screen_rect.right and 
                                self._screen_rect.top <= py < self._screen_rect.bottom):
                                self._frame_surface.set_at((px, py), sprite_color)

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
