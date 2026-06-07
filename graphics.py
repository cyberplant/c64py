"""
Pygame graphics interface for the C64 emulator.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

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
)
from .host_keymap import (
    LSHIFT_ROW_COL,
    ShiftReq,
    build_host_to_joystick,
    build_host_to_matrix,
)
from .config import (
    default_sdl_joystick_index_for_c64_port,
    gamepad_joy_guid,
    parse_gamepad_mapping_entry,
)
from .presenter import RgbFrameBuffer

if TYPE_CHECKING:
    from .emulator import C64
    from .memory import MemoryMap


def _env_truthy(name: str) -> bool:
    v = os.environ.get(name, "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _gamepad_debug_verbose() -> bool:
    return _env_truthy("C64PY_DEBUG_GAMEPAD")


class PygameInterface:
    """Pygame-based graphics UI for the C64 emulator.

    Owns the pygame window, handles input, and renders the emulator screen.
    The main event loop runs in the caller thread while CPU execution runs
    on a background thread started by `run()`.

    Pixels are composed in :class:`presenter.RgbFrameBuffer` (host presenter, not the
    VIC) and uploaded to pygame; see ``presenter`` module docs for the core/presenter
    boundary. Presentation is throttled to ``fps`` so the main thread does less work
    between uploads than the emulated frame rate.
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
        joystick_config: Optional[dict] = None,
    ) -> None:
        self.emulator = emulator
        self.max_cycles = max_cycles
        self.scale = max(1, int(scale))
        self.fps = max(1, int(fps))
        self.border_size = self.DEFAULT_BORDER if border_size is None else max(0, int(border_size))
        # TOML ``[input.joystick]`` sub-table (port1/port2). ``None`` means
        # fall back to ``host_keymap.DEFAULT_JOYSTICK_CONFIG`` at run() time.
        self._joystick_config = joystick_config

        self.running = False
        self.emulator_thread = None
        self.max_logs = 1000
        self._log_messages: List[str] = []

        self._pygame = None
        self._display_surface = None
        self._frame_surface = None
        self._frame_scaled_src = None  # display-format native size; used when scale > 1
        self._rgb_frame: Optional[RgbFrameBuffer] = None
        self._screen_rect = None
        self._native_size: Optional[Tuple[int, int]] = None
        self._display_size: Optional[Tuple[int, int]] = None
        # Host→matrix mapping (built lazily after pygame is imported in run()).
        self._host_to_matrix: Optional[dict] = None
        # Host→joystick mapping (built in run() from self._joystick_config).
        self._host_to_joystick: Optional[dict] = None
        self._is_fullscreen: bool = False
        self._windowed_size: Optional[Tuple[int, int]] = None
        self._gamepad_cfg: Optional[dict] = None
        self._gamepad_spec: Dict[int, Dict[str, Any]] = {}
        self._gamepad_ports: Dict[int, Any] = {}
        self._gamepad_joy_by_index: Dict[int, Any] = {}
        self._gamepad_active_bits: Dict[int, int] = {1: 0, 2: 0}
        self._gamepad_debug_last_log: float = 0.0
        # SHIFT auto-press refcount: tracks how many ShiftReq.SHIFT keys are currently held.
        self._auto_shift_refcount: int = 0
        # Standard hires text when RAM charset matches char ROM: pygame blit (built once per ROM+offset).
        self._rom_glyph_key: Optional[Tuple[int, int]] = None  # (id(char_rom), rom_offset)
        self._rom_glyph_surfaces: Optional[list] = None

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

    def add_debug_log(self, message: str, *, style: Optional[str] = None) -> None:
        from datetime import datetime

        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {message}"
        self._log_messages.append(formatted_message)
        if len(self._log_messages) > self.max_logs:
            self._log_messages.pop(0)
        if style == "yellow" and sys.stdout.isatty():
            print(f"\033[33m{formatted_message}\033[0m")
        else:
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
        self._host_to_matrix = build_host_to_matrix()
        self._host_to_joystick = build_host_to_joystick(self._joystick_config)
        self._setup_gamepad()

        self.running = True
        self.emulator.running = True
        self.emulator_thread = threading.Thread(target=self._run_emulator, daemon=True)
        self.emulator_thread.start()

        clock = pygame.time.Clock()
        present_period = 1.0 / float(self.fps)
        last_present = 0.0
        try:
            while self.running:
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        self._request_quit()
                    elif event.type == pygame.KEYDOWN:
                        self._handle_keydown(event)
                    elif event.type == pygame.KEYUP:
                        self._handle_keyup(event)
                    elif event.type in (pygame.JOYAXISMOTION, pygame.JOYBUTTONDOWN, pygame.JOYBUTTONUP, pygame.JOYHATMOTION, pygame.JOYDEVICEADDED, pygame.JOYDEVICEREMOVED):
                        self._handle_gamepad_event(event)

                # Poll joystick state every frame when gamepad is enabled. Many
                # controllers (especially Bluetooth / SDL gamepad) update axes
                # without a steady stream of JOYAXISMOTION events.
                if self._gamepad_any_enabled():
                    self._sync_all_gamepad_bits()

                if self.emulator and not self.emulator.running:
                    self.running = False

                now = time.perf_counter()
                if last_present == 0.0 or (now - last_present) >= present_period:
                    self._render_frame()
                    dw, dh = self._display_surface.get_size()
                    self._display_size = (dw, dh)
                    nw, nh = self._native_size
                    if self.scale == 1 and (nw, nh) == (dw, dh):
                        self._display_surface.blit(self._frame_surface, (0, 0))
                    elif self.scale == 1:
                        pygame.transform.scale(self._frame_surface, (dw, dh), self._display_surface)
                    else:
                        if self._frame_scaled_src is not None:
                            self._frame_scaled_src.blit(self._frame_surface, (0, 0))
                            pygame.transform.scale(
                                self._frame_scaled_src, (dw, dh), self._display_surface
                            )
                        else:
                            pygame.transform.scale(self._frame_surface, (dw, dh), self._display_surface)
                    pygame.display.flip()
                    last_present = time.perf_counter()
                    clock.tick(self.fps)
                else:
                    # Sleep until the next present window instead of polling every 1 ms.
                    # Tight 1 ms wakeups (~1000/s) fight the CPU thread for the GIL and
                    # dominated the graphics+reSID+turbo regression vs older pygame loops.
                    slack = present_period - (now - last_present)
                    if slack > 0:
                        time.sleep(slack * 0.95)
                    else:
                        time.sleep(0)
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
        self._windowed_size = self._display_size
        nw, nh = self._native_size
        self._rgb_frame = RgbFrameBuffer(nw, nh)
        self._frame_surface = self._pygame.image.frombuffer(
            memoryview(self._rgb_frame.buf), self._native_size, "RGB"
        )
        # frombuffer RGB (24-bit) often differs from set_mode pixel layout; transform.scale
        # requires matching formats, so blit into a display-converted surface first.
        if self.scale == 1:
            self._frame_scaled_src = None
        else:
            self._frame_scaled_src = self._pygame.Surface(self._native_size).convert(
                self._display_surface
            )
        self._screen_rect = self._pygame.Rect(self.border_size, self.border_size, screen_w, screen_h)

    def _request_quit(self) -> None:
        self.running = False
        if self.emulator:
            self.emulator.running = False

    def _save_runtime_snapshot(self) -> None:
        """Request a snapshot write from the CPU thread (Alt+S handler).

        Builds a default path under ``snapshots/`` tagged with the current
        cycle count. Users can override the location with
        ``--save-snapshot-at-cycle`` / ``--save-snapshot-at-exit`` flags when
        scripting; this keybinding is purely interactive.
        """
        import os
        cycle = int(getattr(self.emulator, "current_cycles", 0))
        path = os.path.join("snapshots", f"manual_cycle_{cycle}.snap")
        self.emulator.request_runtime_snapshot(
            path, note=f"alt-s cycle={cycle}"
        )

    def _handle_keydown(self, event) -> None:
        pygame = self._pygame
        # 1. Host-only keybindings: consumed here, never routed to the C64 matrix.
        if event.key == pygame.K_F10:
            if self.emulator is not None:
                self.emulator.turbo = not bool(getattr(self.emulator, "turbo", False))
                self.add_debug_log(f"Turbo: {'ON' if self.emulator.turbo else 'OFF'}")
            return
        if event.key == pygame.K_F11:
            self._toggle_fullscreen()
            return
        if event.key == pygame.K_F12:
            self._save_screenshot()
            return
        if event.mod & pygame.KMOD_CTRL:
            if event.key in (pygame.K_x, pygame.K_q):
                self._request_quit()
                return

        # Alt+S: save a snapshot of the current emulator state. The CPU
        # thread services the request between instructions to avoid races
        # with the shared RAM / VIC register bytearrays.
        if event.mod & pygame.KMOD_ALT and event.key == pygame.K_s:
            if self.emulator is not None:
                self._save_runtime_snapshot()
                return

        if not self.emulator or not self.emulator.running:
            return

        mem = self.emulator.memory

        # 2a. Joystick mapping (item C/E). A host key may be bound to BOTH a
        # matrix slot and one or more (port, bit) pairs — on real hardware
        # the joystick lines share the matrix wires, so we drive both. Games
        # disambiguate via DDR; BASIC ignores the joystick bits.
        joy_targets = (self._host_to_joystick or {}).get(event.key)
        if joy_targets:
            for port, bit_mask in joy_targets:
                mem.set_joystick_dir(port, bit_mask)

        # 2b. Mapped keys → press in the CIA1 matrix. The KERNAL ISR will scan
        # them and fill the type-ahead buffer just as it does on real hardware.
        target = (self._host_to_matrix or {}).get(event.key)
        if target is None:
            # Unmapped keys (F9-F12, PrtScr, meta, etc.) are silently dropped
            # for the matrix path; joystick-only keys (e.g. arrow keys when
            # the user has rebound them out of HOST_TO_MATRIX) still pressed
            # their joystick bit above.
            return
        # NOTE: Do NOT also call send_petscii() here to "help" GETIN-based games.
        # Direct injection into the KERNAL keyboard buffer bypasses the CIA1 matrix
        # scan entirely, which breaks games that rely on key-held state (e.g. timing
        # a held key, auto-repeat, joystick-shared lines) and causes double input in
        # programs that both poll $DC01 and call GETIN. The correct path is the CIA1
        # matrix: the KERNAL ISR (CIA1 Timer A IRQ → SCNKEY at $FF9F) scans the matrix
        # and fills the typeahead buffer at $0277/$C6 exactly as real hardware does.
        row, col, shift_req = target
        mem.press_matrix_key(row, col)
        if shift_req == ShiftReq.SHIFT:
            self._auto_shift_refcount += 1
            if self._auto_shift_refcount == 1:
                mem.press_matrix_key(*LSHIFT_ROW_COL)

    def _handle_keyup(self, event) -> None:
        # Host-only bindings have no release-side action, so just look up the
        # matrix + joystick mappings. If the user releases an unmapped key,
        # nothing to do.
        if not self.emulator:
            return
        mem = self.emulator.memory

        joy_targets = (self._host_to_joystick or {}).get(event.key)
        if joy_targets:
            for port, bit_mask in joy_targets:
                mem.clear_joystick_dir(port, bit_mask)

        target = (self._host_to_matrix or {}).get(event.key)
        if target is None:
            return
        row, col, shift_req = target
        mem.release_matrix_key(row, col)
        if shift_req == ShiftReq.SHIFT and self._auto_shift_refcount > 0:
            self._auto_shift_refcount -= 1
            if self._auto_shift_refcount == 0:
                mem.release_matrix_key(*LSHIFT_ROW_COL)

    def _resize_present_surfaces(self) -> None:
        """Rebuild scaling surfaces after ``set_mode`` (windowed ↔ fullscreen)."""
        pygame = self._pygame
        if pygame is None or self._display_surface is None or not self._native_size:
            return
        self._display_size = self._display_surface.get_size()
        nw, nh = self._native_size
        if self.scale > 1:
            self._frame_scaled_src = pygame.Surface((nw, nh)).convert(self._display_surface)
        else:
            self._frame_scaled_src = None

    def _toggle_fullscreen(self) -> None:
        pygame = self._pygame
        if pygame is None:
            return
        self._is_fullscreen = not self._is_fullscreen
        if self._is_fullscreen:
            # Capture the current windowed size right before entering fullscreen
            # so we can restore it exactly on exit.
            if self._display_surface is not None:
                self._windowed_size = self._display_surface.get_size()
            flags = pygame.FULLSCREEN
            self._display_surface = pygame.display.set_mode((0, 0), flags)
            self.add_debug_log("Display: fullscreen")
        else:
            target = self._windowed_size or self._display_size
            # Some platforms (notably macOS) may keep a "fullscreen desktop" sized
            # window unless we explicitly re-enter a normal window mode.
            self._display_surface = pygame.display.set_mode(target, 0)
            got = self._display_surface.get_size() if self._display_surface is not None else None
            if got != target:
                self._display_surface = pygame.display.set_mode(target, pygame.RESIZABLE)
                got = self._display_surface.get_size() if self._display_surface is not None else None
            if got != target:
                pygame.display.quit()
                pygame.display.init()
                self._display_surface = pygame.display.set_mode(target, 0)
                got = self._display_surface.get_size() if self._display_surface is not None else None
            if got != target:
                self.add_debug_log(
                    f"Display: windowed requested {target} but OS forced {got}",
                    style="yellow",
                )
            self.add_debug_log("Display: windowed")
        self._resize_present_surfaces()

    def _save_screenshot(self) -> None:
        pygame = self._pygame
        if pygame is None or self._display_surface is None:
            return
        os.makedirs("snapshots", exist_ok=True)
        cycle = int(getattr(self.emulator, "current_cycles", 0)) if self.emulator else 0
        stamp = int(time.time())
        path = os.path.join("snapshots", f"screenshot_{stamp}_c{cycle}.png")
        try:
            pygame.image.save(self._display_surface, path)
            self.add_debug_log(f"Screenshot saved: {path}")
        except Exception as exc:
            self.add_debug_log(f"Screenshot failed: {exc}")

    def _gamepad_any_enabled(self) -> bool:
        return any(self._gamepad_spec.get(p, {}).get("enabled") for p in (1, 2))

    def _setup_gamepad(self) -> None:
        full_cfg = getattr(self.emulator, "host_config", None)
        cfg = {}
        if isinstance(full_cfg, dict):
            cfg = (((full_cfg.get("input") or {}).get("gamepad")) or {})
        self._gamepad_cfg = cfg if isinstance(cfg, dict) else {}
        pygame = self._pygame
        if pygame is None:
            return

        self._gamepad_spec.clear()
        base_th = float(self._gamepad_cfg.get("axis_threshold", 0.5))
        any_en = False
        for port in (1, 2):
            block = self._gamepad_cfg.get(f"port{port}")
            if not isinstance(block, dict):
                block = {}
            en = bool(block.get("enabled", False))
            if en:
                any_en = True
            th = float(block.get("axis_threshold", base_th))
            mapping = block.get("mapping", {})
            if not isinstance(mapping, dict):
                mapping = {}
            self._gamepad_spec[port] = {
                "enabled": en,
                "axis_threshold": th,
                "mapping": mapping,
            }

        if not any_en:
            self.add_debug_log(
                "Gamepad: all ports disabled. Enable [input.gamepad.port1] or [.port2] "
                "enabled = true in c64py.toml, then restart."
            )
            self._log_detected_joysticks("Joysticks (gamepad ports off)")
            return

        for port in (1, 2):
            spec = self._gamepad_spec.get(port, {})
            if spec.get("enabled"):
                di = default_sdl_joystick_index_for_c64_port(port)
                self.add_debug_log(
                    f"Gamepad port {port}: legacy string bindings use SDL index {di}; "
                    f"threshold={spec['axis_threshold']}, mapping={spec.get('mapping')}"
                )
        if full_cfg is None or not isinstance(full_cfg, dict):
            self.add_debug_log(
                "Gamepad: warning — emulator.host_config missing; check C64.py sets emu.host_config after load.",
                style="yellow",
            )

        try:
            pygame.event.pump()
            self._select_all_gamepad_devices()
            pygame.event.pump()
            self._select_all_gamepad_devices()
        except Exception as exc:
            self.add_debug_log(f"Gamepad init failed: {exc}")

        if _gamepad_debug_verbose():
            self.add_debug_log(
                "Gamepad: verbose logging on (C64PY_DEBUG_GAMEPAD=1) — joystick events print to console."
            )

    def _log_detected_joysticks(self, prefix: str) -> None:
        pygame = self._pygame
        if pygame is None:
            return
        n = pygame.joystick.get_count()
        if n == 0:
            self.add_debug_log(f"{prefix}: pygame.joystick.get_count() == 0 (no devices)")
            return
        names = []
        for i in range(n):
            try:
                j = pygame.joystick.Joystick(i)
                names.append(f"{i}:{j.get_name()!r}")
            except Exception as exc:
                names.append(f"{i}:<error {exc}>")
        self.add_debug_log(f"{prefix}: {n} device(s) — " + "; ".join(names))

    def _release_port_gamepad_bits(self, port: int) -> None:
        if self.emulator is None:
            self._gamepad_active_bits[port] = 0
            return
        mem = self.emulator.memory
        old = self._gamepad_active_bits.get(port, 0)
        for bit in (0x01, 0x02, 0x04, 0x08, 0x10):
            if old & bit:
                mem.clear_joystick_dir(port, bit)
        self._gamepad_active_bits[port] = 0

    def _select_all_gamepad_devices(self) -> None:
        pygame = self._pygame
        if pygame is None:
            return
        count = pygame.joystick.get_count()
        self._gamepad_joy_by_index.clear()
        for i in range(count):
            try:
                self._gamepad_joy_by_index[i] = pygame.joystick.Joystick(i)
            except Exception as exc:
                self.add_debug_log(f"Joystick SDL index {i}: open failed: {exc}")
        for port in (1, 2):
            spec = self._gamepad_spec.get(port, {})
            if not spec.get("enabled"):
                self._gamepad_ports[port] = None
                if self._gamepad_active_bits.get(port, 0):
                    self._release_port_gamepad_bits(port)
                continue
            if count <= 0:
                self._gamepad_ports[port] = None
                continue
            wanted = default_sdl_joystick_index_for_c64_port(port)
            idx = wanted if 0 <= wanted < count else 0
            if wanted != idx:
                self.add_debug_log(
                    f"Gamepad port {port}: default SDL index {wanted} out of range (count={count}); using {idx}",
                    style="yellow",
                )
            joy = self._gamepad_joy_by_index.get(idx)
            self._gamepad_ports[port] = joy
            if joy is not None:
                g = gamepad_joy_guid(joy)
                self.add_debug_log(
                    f"Gamepad port {port}: SDL index {idx} — {joy.get_name()!r} "
                    f"guid={g or '?'} "
                    f"(axes={joy.get_numaxes()}, buttons={joy.get_numbuttons()}, hats={joy.get_numhats()})"
                )
            else:
                self.add_debug_log(f"Gamepad port {port}: no joystick at SDL index {idx}")
        if count <= 0 and self._gamepad_any_enabled():
            self.add_debug_log(
                "Gamepad: no SDL/pygame joysticks yet. Connect the controller, "
                "then unplug/replug or restart. Tip: C64PY_DEBUG_GAMEPAD=1"
            )
            self._log_detected_joysticks("Joystick scan")
        self._sync_all_gamepad_bits()

    def _handle_gamepad_event(self, event) -> None:
        if not self._gamepad_any_enabled():
            return
        pygame = self._pygame
        if pygame is None:
            return
        if _gamepad_debug_verbose():
            self._log_gamepad_event(event)
        if event.type in (pygame.JOYDEVICEADDED, pygame.JOYDEVICEREMOVED):
            inst = getattr(event, "instance_id", None)
            self.add_debug_log(
                f"Gamepad: {'added' if event.type == pygame.JOYDEVICEADDED else 'removed'} "
                f"(instance_id={inst})"
            )
            self._select_all_gamepad_devices()
        self._sync_all_gamepad_bits()

    def _log_gamepad_event(self, event) -> None:
        pygame = self._pygame
        if pygame is None:
            return
        et = event.type
        if et == pygame.JOYAXISMOTION:
            self.add_debug_log(
                f"JOYAXISMOTION joy={getattr(event, 'instance_id', '?')} "
                f"axis={event.axis} value={event.value:.3f}"
            )
        elif et == pygame.JOYBUTTONDOWN:
            self.add_debug_log(f"JOYBUTTONDOWN joy={getattr(event, 'instance_id', '?')} button={event.button}")
        elif et == pygame.JOYBUTTONUP:
            self.add_debug_log(f"JOYBUTTONUP joy={getattr(event, 'instance_id', '?')} button={event.button}")
        elif et == pygame.JOYHATMOTION:
            self.add_debug_log(
                f"JOYHATMOTION joy={getattr(event, 'instance_id', '?')} hat={event.hat} value={event.value}"
            )

    def _apply_gamepad_port_bits(self, port: int, new_bits: int) -> None:
        old = self._gamepad_active_bits.get(port, 0)
        changed = old ^ new_bits
        if changed == 0:
            return
        if self.emulator is None:
            self._gamepad_active_bits[port] = new_bits
            return
        mem = self.emulator.memory
        for bit in (0x01, 0x02, 0x04, 0x08, 0x10):
            if not (changed & bit):
                continue
            if new_bits & bit:
                mem.set_joystick_dir(port, bit)
            else:
                mem.clear_joystick_dir(port, bit)
        self._gamepad_active_bits[port] = new_bits

    def _resolve_gamepad_joy(
        self, port: int, guid: Optional[str], host_index: Optional[int]
    ) -> Optional[Any]:
        """Pick the pygame Joystick for a binding (per-GUID or port default)."""

        if not guid:
            return self._gamepad_ports.get(port)
        matches: List[Tuple[int, Any]] = []
        for i, j in sorted(self._gamepad_joy_by_index.items()):
            gj = gamepad_joy_guid(j)
            if gj and gj == guid:
                matches.append((i, j))
        if not matches:
            return None
        if host_index is not None:
            for i, j in matches:
                if i == host_index:
                    return j
            return None
        return matches[0][1]

    def _sync_all_gamepad_bits(self) -> None:
        if self.emulator is None:
            return
        for port in (1, 2):
            spec = self._gamepad_spec.get(port, {})
            if not spec.get("enabled"):
                if self._gamepad_active_bits.get(port, 0):
                    self._release_port_gamepad_bits(port)
                continue
            threshold = float(spec.get("axis_threshold", 0.5))
            mapping = spec.get("mapping", {})
            if not isinstance(mapping, dict):
                mapping = {}
            bit_map = {"up": 0x01, "down": 0x02, "left": 0x04, "right": 0x08, "fire": 0x10}
            new_bits = 0
            for direction, bit in bit_map.items():
                guid, host_i, token = parse_gamepad_mapping_entry(mapping.get(direction))
                if not token:
                    continue
                joy = self._resolve_gamepad_joy(port, guid, host_i)
                if joy is None:
                    continue
                if self._gamepad_token_active(joy, token, threshold):
                    new_bits |= bit
            self._apply_gamepad_port_bits(port, new_bits)

    def _gamepad_token_active(self, joy: Any, token: str, threshold: float) -> bool:
        if joy is None:
            return False
        token = token.strip().lower()
        m_axis = re.fullmatch(r"axis(\d+)([+-])", token)
        if m_axis:
            axis = int(m_axis.group(1))
            sign = m_axis.group(2)
            if axis >= joy.get_numaxes():
                return False
            val = float(joy.get_axis(axis))
            return val >= threshold if sign == "+" else val <= -threshold
        m_button = re.fullmatch(r"button(\d+)", token)
        if m_button:
            btn = int(m_button.group(1))
            return bool(joy.get_button(btn)) if btn < joy.get_numbuttons() else False
        m_hat = re.fullmatch(r"hat(\d+):(up|down|left|right)", token)
        if m_hat:
            hat = int(m_hat.group(1))
            if hat >= joy.get_numhats():
                return False
            x, y = joy.get_hat(hat)
            d = m_hat.group(2)
            return (d == "up" and y > 0) or (d == "down" and y < 0) or (d == "left" and x < 0) or (d == "right" and x > 0)
        return False

    def _run_emulator(self) -> None:
        """Run the emulator CPU loop on a background thread."""
        prof = None
        if _env_truthy("C64PY_PROFILE_CPU_THREAD"):
            import cProfile

            prof = cProfile.Profile()
            prof.enable()
        try:
            try:
                self.emulator.running = True
                self.emulator.reset_speed_throttle()
                cycles = 0
                max_cycles = self.max_cycles
                last_pc = None
                stuck_count = 0
                inject_wall_t0 = time.perf_counter()

                # Prime render latch so pygame sees consistent regs before first PAL frame completes.
                self.emulator.memory.snapshot_vic_render_state()

                while self.emulator.running:
                    self.emulator.sync_keyboard_host_queue()
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

                    # Attach disk if pending (after BASIC boot completes)
                    if self.emulator.disk_image_path and not hasattr(self.emulator, '_disk_attached_after_boot'):
                        # BASIC is ready - attach disk now (after boot has completed)
                        # Wait until we're past boot sequence
                        if cycles > BASIC_BOOT_CYCLES:
                            try:
                                self.emulator.attach_disk(self.emulator.disk_image_path, device=8)
                                self.emulator.disk_image_path = None  # Clear path after attaching
                                self.emulator._disk_attached_after_boot = True
                                self.add_debug_log("Disk attached after BASIC boot completed")
                                # Inject LOAD"$",8 command into keyboard buffer to list directory
                                self.emulator._inject_load_directory_command(device=8)
                            except Exception as exc:
                                self.add_debug_log(f"Failed to attach disk: {exc}")
                                self.emulator.disk_image_path = None  # Clear path even on error

                    step_cycles = self.emulator.run_cpu_instruction_quantum(
                        cycles, max_cycles
                    )
                    if step_cycles == 0:
                        continue

                    cycles += step_cycles
                    self.emulator.current_cycles = cycles
                    self.emulator._step_iec_drives(step_cycles)
                    self.emulator.memory.sync_joystick_inject(cycles)
                    self.emulator._process_scheduled_inject_keys(
                        cycles, time.perf_counter() - inject_wall_t0
                    )
                    self.emulator.throttle_emulation_if_needed(cycles)

                    pc = self.emulator.cpu.state.pc
                    if pc == last_pc:
                        # Check if we're in a graphics mode wait loop
                        # A simple JMP * (4C XX XX where XX XX = current PC) is ok in graphics mode
                        mode_info = self.emulator.memory.get_display_mode()
                        in_graphics_mode = (
                            mode_info['bitmap_mode']
                            or mode_info.get('multicolor', False)
                            or self.emulator.memory.is_sprite_enabled(0)
                        )

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
                    stop_reason = "max_cycles_reached"
                else:
                    self.add_debug_log(f"Stopped at cycle {cycles} (stuck_count={stuck_count})")
                    stop_reason = "stuck_pc" if stuck_count > STUCK_PC_THRESHOLD else "unknown"
                # Honor --save-snapshot-at-exit in graphics mode as well — the
                # canonical ``emulator.run()`` path is not used here, so we
                # service the at-exit snapshot ourselves.
                self.emulator.service_exit_snapshot(stop_reason)
            except Exception as exc:
                self.add_debug_log(f"Emulator error ({type(exc).__name__}): {exc}")
                self.emulator.running = False
                self.emulator.service_exit_snapshot("exception")
        finally:
            if prof is not None:
                prof.disable()
                out = (os.environ.get("C64PY_PROFILE_CPU_OUT") or "cpu_thread.prof").strip() or "cpu_thread.prof"
                parent = os.path.dirname(os.path.abspath(out))
                if parent:
                    os.makedirs(parent, exist_ok=True)
                prof.dump_stats(out)
                if _env_truthy("C64PY_PROFILE_CPU_THREAD_PRINT"):
                    import pstats

                    pstats.Stats(prof).sort_stats("cumtime").print_stats(40)

    def _fetch_glyph_rows(self, vic_bank_base: int, char_base: int, screen_code: int) -> bytes:
        """Read 8 bytes of charset definition as the VIC-II fetches (incl. ROM mirror at bank+$1000)."""
        return self.emulator.memory.read_vic_charset_glyph_rows(
            vic_bank_base, char_base, screen_code & 0xFF
        )

    def _plot_hires_text_cell(self, x: int, y: int, rows: bytes, fg_idx: int) -> None:
        """Draw an 8×8 hires glyph; unset bits leave the existing background."""
        fg = self._palette.get(fg_idx, (255, 255, 255))
        self._rgb_frame.plot_hires_glyph(x, y, rows, fg)

    def _petscii_to_screen_code(self, petscii_char: int) -> int:
        return self.emulator._petscii_to_screen_code(petscii_char)

    @staticmethod
    def _charset_matches_char_rom_slice(block: bytes, char_rom: Optional[bytes]) -> Optional[int]:
        """If *block* equals char ROM uppercase or lowercase 2K window, return that offset; else None."""
        if not char_rom or len(block) != 2048:
            return None
        for off in (0, 2048):
            if off + 2048 <= len(char_rom) and block == char_rom[off : off + 2048]:
                return off
        return None

    def _ensure_rom_glyph_surfaces(self, char_rom: bytes, rom_offset: int) -> None:
        """Build 256×16 blit surfaces from a char ROM slice (once per ROM object + offset)."""
        pygame = self._pygame
        if pygame is None:
            return
        key = (id(char_rom), rom_offset)
        if key == self._rom_glyph_key and self._rom_glyph_surfaces is not None:
            return
        self._rom_glyph_key = key
        block = char_rom[rom_offset : rom_offset + 2048]
        glyphs: list = []
        for gi in range(256):
            rows = block[gi * 8 : (gi + 1) * 8]
            color_layers = []
            for ci in range(16):
                surf = pygame.Surface((self.CHAR_WIDTH, self.CHAR_HEIGHT), flags=pygame.SRCALPHA)
                fg = self._palette.get(ci, (255, 255, 255))
                for yy in range(self.CHAR_HEIGHT):
                    row_b = rows[yy]
                    for xx in range(self.CHAR_WIDTH):
                        if row_b & (1 << (7 - xx)):
                            surf.set_at((xx, yy), (*fg, 255))
                color_layers.append(surf)
            glyphs.append(color_layers)
        self._rom_glyph_surfaces = glyphs

    def _render_text_mode_blit_rom(
        self,
        mode_info: dict,
        snap: Optional[Tuple[bytes, int]],
    ) -> None:
        """Hires text via pygame blit using :attr:`_rom_glyph_surfaces`."""
        if self._frame_surface is None:
            return
        glyphs = self._rom_glyph_surfaces
        if not glyphs:
            return
        mem = self.emulator.memory.ram
        vic_bank = self.emulator.memory.get_render_vic_bank_base()
        screen_base = (vic_bank + mode_info['screen_base']) & 0xFFFF

        if snap:
            regb, _ = snap
            bg_colors = [
                regb[0x21] & 0x0F if len(regb) > 0x21 else 6,
                regb[0x22] & 0x0F if len(regb) > 0x22 else 0,
                regb[0x23] & 0x0F if len(regb) > 0x23 else 0,
                regb[0x24] & 0x0F if len(regb) > 0x24 else 0,
            ]
        else:
            m = self.emulator.memory
            bg_colors = [
                m.read(0xD021) & 0x0F,
                m.read(0xD022) & 0x0F,
                m.read(0xD023) & 0x0F,
                m.read(0xD024) & 0x0F,
            ]

        screen_left = self._screen_rect.left
        screen_top = self._screen_rect.top
        color_base = COLOR_MEM
        dest = self._frame_surface
        _ = bg_colors  # kept for future reverse/ECM handling symmetry

        for row in range(self.SCREEN_ROWS):
            row_offset = row * self.SCREEN_COLS
            y = screen_top + row * self.CHAR_HEIGHT
            for col in range(self.SCREEN_COLS):
                idx = row_offset + col
                raw_code = mem[(screen_base + idx) & 0xFFFF]
                color_code = mem[color_base + idx] & 0x0F
                x = screen_left + col * self.CHAR_WIDTH
                glyph = glyphs[raw_code & 0xFF][color_code]
                dest.blit(glyph, (x, y))

    @staticmethod
    def _beam_vic_regb_view(mem: "MemoryMap", rl: int):
        """64-byte VIC register row for raster index *rl* (flat buffer when Rust wrote in-place)."""
        lines = mem.beam_vic_lines
        n = len(lines) if lines else 0
        if n <= 0:
            return memoryview(b"")
        rl %= n
        flat = getattr(mem, "beam_vic_flat", None)
        if flat is not None and len(flat) >= (rl + 1) * 64:
            return memoryview(flat)[rl * 64 : (rl + 1) * 64]
        if lines is not None and rl < len(lines):
            return memoryview(lines[rl])
        return memoryview(b"")

    @staticmethod
    def _beam_cia2_byte(mem: "MemoryMap", rl: int) -> int:
        lines = mem.beam_cia2_lines
        n = len(lines) if lines else 0
        if n <= 0:
            return 0
        rl %= n
        flat = getattr(mem, "beam_cia2_flat", None)
        if flat is not None and rl < len(flat):
            return flat[rl] & 0xFF
        if lines is not None and rl < len(lines):
            return lines[rl] & 0xFF
        return 0

    def _render_frame_beam(self) -> None:
        """Per-raster (``per-raster``) video rendering: one VIC/CIA2 bank sample per raster line.

        Dispatches each of the 25 content rows individually based on the VIC
        configuration latched at that row's top raster line. Honors mid-frame
        changes to ``$D011``/``$D016``/``$D018``/``$D020``-``$D024`` /
        CIA-2 PA — i.e. split-screen layouts (HUD + playfield, FLI-lite,
        color bars). All text variants (hires, MCM, ECM), standard hi-res
        bitmap, and multicolor bitmap are supported row by row.

        Background (text/bitmap) uses one VIC/CIA2 sample per 8-line content row.
        Sprites are composited **once** after all rows using the same frame latch
        as :meth:`_render_frame_latched` (see :meth:`_render_sprites`). Per-row
        sprite DMA (raster multiplex within a band) needs the future per-cycle
        tier; sampling sprite regs only at each band start mis-draws games such
        as Arkanoid.

        Per-line data comes from :meth:`MemoryMap.beam_capture_raster_line`
        during Python CPU steps and from the Rust fast batch when beam
        capture is enabled. Prime once via
        :meth:`MemoryMap.prime_beam_snapshots_from_current_vic` so the first
        frame is not all zeros.
        """
        from .video_beam import content_row_to_raster_line

        mem = self.emulator.memory
        lines = mem.beam_vic_lines
        c2 = mem.beam_cia2_lines
        if not lines or not c2 or len(lines) != len(c2):
            self._render_frame_latched()
            return
        vs = mem.video_standard
        ram = mem.ram
        screen_left = self._screen_rect.left
        screen_top = self._screen_rect.top
        nlines = len(lines)

        # Border strip: per-raster $D020 for the whole frame (top/bottom + left/right columns).
        total_lines = 263 if vs == "ntsc" else 312
        content_first = content_row_to_raster_line(0, vs)
        content_h = 200
        top_lines = max(0, min(total_lines, content_first))
        bottom_lines = max(0, total_lines - (content_first + content_h))
        border_px = int(self.border_size)
        native_h = int(self._native_size[1]) if hasattr(self, "_native_size") else (content_h + 2 * border_px)
        for y in range(native_h):
            if border_px > 0 and y < border_px:
                rl = int((y * top_lines) / border_px) if top_lines > 0 else 0
            elif y < border_px + content_h:
                rl = content_first + (y - border_px)
            else:
                yy = y - (border_px + content_h)
                rl = (content_first + content_h) + (int((yy * bottom_lines) / border_px) if border_px > 0 else 0)
            rl %= nlines
            regb = self._beam_vic_regb_view(mem, rl)
            border_code = regb[0x20] & 0x0F if len(regb) > 0x20 else 0x0E
            if border_code == 0 and not any(regb):
                border_code = 0x0E
            self._rgb_frame.fill_rect(0, y, self._rgb_frame.width, 1, self._palette.get(border_code, (0, 0, 0)))

        for row in range(self.SCREEN_ROWS):
            # The row's VIC config is latched at the raster line where the row
            # visually starts. Sub-row changes (within the 8 scanlines) are not
            # honored — that needs the per-cycle renderer.
            rl = content_row_to_raster_line(row * self.CHAR_HEIGHT, vs) % nlines
            regb = self._beam_vic_regb_view(mem, rl)
            pra = self._beam_cia2_byte(mem, rl)
            # All-zero sample (e.g. frame 0 before beam prime wrote anything)
            # falls back to live VIC regs for this row to avoid a black stripe.
            if not any(regb):
                regb = memoryview(bytes(mem._vic_regs[:0x40]))
                pra = mem.cia2_pra & 0xFF
            mode_info = mem._display_mode_from_vic_bytes(regb)
            vic_bank = (3 - (pra & 0x03)) * 0x4000
            bg_colors = [
                regb[0x21] & 0x0F,
                regb[0x22] & 0x0F,
                regb[0x23] & 0x0F,
                regb[0x24] & 0x0F,
            ]
            y = screen_top + row * self.CHAR_HEIGHT

            if mode_info["bitmap_mode"]:
                bitmap_base = (vic_bank + mode_info["bitmap_base"]) & 0xFFFF
                screen_base = (vic_bank + mode_info["screen_base"]) & 0xFFFF
                # Bitmap mode paints its own pixels for every position —
                # pre-fill with $D021 anyway so any transparent future overlay
                # (e.g. sprite gaps) sees a consistent ground color.
                self._rgb_frame.fill_rect(
                    screen_left, y, self._screen_rect.width, self.CHAR_HEIGHT,
                    self._palette.get(bg_colors[0], (0, 0, 0)),
                )
                self._render_row_bitmap(
                    row, y, vic_bank, screen_base, bitmap_base, mode_info, bg_colors[0],
                )
            else:
                screen_base = (vic_bank + mode_info["screen_base"]) & 0xFFFF
                char_base = mode_info["char_base"]
                bg_fill = self._palette.get(bg_colors[0], (0, 0, 0))
                self._render_row_text(
                    row, y, vic_bank, screen_base, char_base,
                    mode_info, bg_colors, bg_fill_color=bg_fill,
                )

        # Sprites: one pass with end-of-frame VIC latch (matches _render_frame_latched).
        if mem.vic_render_snapshots:
            mem.snapshot_vic_render_state()
        self._render_sprites(getattr(mem, "_vic_render_snapshot", None))

    def _paint_border_from_beam(self, mem: "MemoryMap", video_standard: str) -> None:
        """Overlay border color from per-line beam snapshots onto an existing frame."""
        from .video_beam import content_row_to_raster_line

        lines = mem.beam_vic_lines
        nlines = len(lines) if lines else 0
        if nlines <= 0:
            return
        total_lines = 263 if video_standard == "ntsc" else 312
        content_first = content_row_to_raster_line(0, video_standard)
        content_h = 200
        top_lines = max(0, min(total_lines, content_first))
        bottom_lines = max(0, total_lines - (content_first + content_h))
        screen_left = int(self._screen_rect.left)
        screen_top = int(self._screen_rect.top)
        screen_w = int(self._screen_rect.width)
        screen_h = int(self._screen_rect.height)
        screen_right = screen_left + screen_w
        border_px = int(self.border_size)
        native_h = int(self._rgb_frame.height)
        native_w = int(self._rgb_frame.width)

        for y in range(native_h):
            if border_px > 0 and y < border_px:
                rl = int((y * top_lines) / border_px) if top_lines > 0 else 0
            elif y < border_px + content_h:
                rl = content_first + (y - border_px)
            else:
                yy = y - (border_px + content_h)
                rl = (content_first + content_h) + (int((yy * bottom_lines) / border_px) if border_px > 0 else 0)
            rl %= nlines
            regb = self._beam_vic_regb_view(mem, rl)
            border_code = regb[0x20] & 0x0F if len(regb) > 0x20 else 0x0E
            if border_code == 0 and not any(regb):
                border_code = 0x0E
            c = self._palette.get(border_code, (0, 0, 0))
            if y < screen_top or y >= screen_top + screen_h:
                self._rgb_frame.fill_rect(0, y, native_w, 1, c)
            else:
                if screen_left > 0:
                    self._rgb_frame.fill_rect(0, y, screen_left, 1, c)
                if screen_right < native_w:
                    self._rgb_frame.fill_rect(screen_right, y, native_w - screen_right, 1, c)

    def _render_frame_latched(self) -> None:
        """Render one frame using the per-frame VIC latch (non-beam path).

        **Video rendering** (host output, separate from ``--vic-emulation``):
        - *per-frame* (default, alias ``fast``): one VIC snapshot per host present
          (``snapshot_vic_render_state`` / latch).
        - *per-raster* (alias ``accurate``, ``--video-rendering per-raster``): beam mode uses
          per-raster VIC samples and dispatches per-row; with the Rust core, those samples are
          written in-place into shared flat buffers each batch.
        """
        mem = self.emulator.memory
        if mem.vic_render_snapshots and not mem.vic_snapshot_each_emulated_frame:
            mem.snapshot_vic_render_state()
        mode_info = mem.get_render_display_mode()
        snap = getattr(mem, "_vic_render_snapshot", None)
        if snap:
            regb, _ = snap
            bg_code = regb[0x21] & 0x0F if len(regb) > 0x21 else 6
            border_code = regb[0x20] & 0x0F if len(regb) > 0x20 else 0x0E
        else:
            bg_code = mem.read(0xD021) & 0x0F
            border_code = mem.read(0xD020) & 0x0F
        bg_color = self._palette.get(bg_code, (0, 0, 0))
        border_color = self._palette.get(border_code, (0, 0, 0))

        multicolor_text = mode_info.get('multicolor') and not mode_info.get('extended_color')
        simple_hires_text = (
            not mode_info['bitmap_mode']
            and not mode_info.get('extended_color', False)
            and not multicolor_text
            and self._frame_surface is not None
            and self._pygame is not None
        )

        if simple_hires_text:
            # Fast path only when the visible charset bytes match char ROM (one-time glyph build).
            # RAM-based cache on every byte change rebuilt ~262k set_at/frame and was slower than RGB glyphs.
            mmap = self.emulator.memory
            cr = mmap.char_rom
            vic_bank = mmap.get_render_vic_bank_base()
            block = mmap.read_vic_charset_block_2k(vic_bank, mode_info["char_base"])
            rom_off = self._charset_matches_char_rom_slice(block, cr)
            if rom_off is not None and cr is not None:
                self._frame_surface.fill(border_color)
                self._frame_surface.fill(bg_color, self._screen_rect)
                self._ensure_rom_glyph_surfaces(cr, rom_off)
                self._render_text_mode_blit_rom(mode_info, snap)
                self._render_sprites(snap)
                return

        # Fill border and background (RGB buffer → shared pygame surface in _setup_surfaces)
        self._rgb_frame.fill(border_color)
        self._rgb_frame.fill_rect(
            self._screen_rect.left,
            self._screen_rect.top,
            self._screen_rect.width,
            self._screen_rect.height,
            bg_color,
        )

        # Render based on display mode
        if mode_info['bitmap_mode']:
            self._render_bitmap_mode(mode_info, snap)
        else:
            self._render_text_mode(mode_info, snap)

        # Render sprites on top
        self._render_sprites(snap)
        
    def _render_frame(self) -> None:
        """Render one frame into the back buffer (beam or latched)."""
        mem = self.emulator.memory
        if (
            getattr(mem, "beam_render_enabled", False)
            and mem.beam_vic_lines
            and getattr(mem, "beam_snapshots_primed", False)
        ):
            self._render_frame_beam()
            return
        self._render_frame_latched()
    
    def _render_text_mode(self, mode_info: dict, snap: Optional[Tuple[bytes, int]] = None) -> None:
        """Render text mode; charset definitions are read from VIC bank RAM (as the VIC would).

        Thin wrapper that dispatches each of the 25 content rows to
        :meth:`_render_row_text` using one shared VIC config — the "latched"
        per-frame rendering path. The beam renderer reuses the same per-row
        helper with per-raster configs for split-screen frames.
        """
        mem = self.emulator.memory.ram
        mmap = self.emulator.memory
        vic_bank = mmap.get_render_vic_bank_base()
        screen_base = (vic_bank + mode_info['screen_base']) & 0xFFFF
        char_base = mode_info['char_base']
        screen_top = self._screen_rect.top

        if snap:
            regb, _ = snap
            bg_colors = [
                regb[0x21] & 0x0F if len(regb) > 0x21 else 6,
                regb[0x22] & 0x0F if len(regb) > 0x22 else 0,
                regb[0x23] & 0x0F if len(regb) > 0x23 else 0,
                regb[0x24] & 0x0F if len(regb) > 0x24 else 0,
            ]
        else:
            m = self.emulator.memory
            bg_colors = [
                m.read(0xD021) & 0x0F,
                m.read(0xD022) & 0x0F,
                m.read(0xD023) & 0x0F,
                m.read(0xD024) & 0x0F,
            ]

        for row in range(self.SCREEN_ROWS):
            y = screen_top + row * self.CHAR_HEIGHT
            self._render_row_text(
                row, y, vic_bank, screen_base, char_base,
                mode_info, bg_colors, bg_fill_color=None,
            )

    def _plot_multicolor_text_cell(
        self,
        x: int,
        y: int,
        rows: bytes,
        char_color_idx: int,
        bg_i: int,
        c1_i: int,
        c2_i: int,
    ) -> None:
        """Draw one 8x8 multicolor text cell (6569 MCM + color bit 3 set)."""
        p0 = self._palette.get(bg_i, (0, 0, 0))
        p1 = self._palette.get(c1_i, (0, 0, 0))
        p2 = self._palette.get(c2_i, (0, 0, 0))
        p3 = self._palette.get(char_color_idx, (0, 0, 0))
        for yy in range(8):
            b = rows[yy]
            py = y + yy
            for pair in range(4):
                bits = (b >> (6 - pair * 2)) & 0x03
                if bits == 0:
                    c = p0
                elif bits == 1:
                    c = p1
                elif bits == 2:
                    c = p2
                else:
                    c = p3
                px = x + pair * 2
                self._rgb_frame.fill_rect(px, py, 2, 1, c)

    def _render_row_text(
        self,
        row: int,
        y: int,
        vic_bank: int,
        screen_base: int,
        char_base: int,
        mode_info: dict,
        bg_colors: list,
        bg_fill_color: Optional[tuple] = None,
    ) -> None:
        """Render a single 8-pixel-tall content row in text mode.

        Handles the three VIC text variants (hires, multicolor, extended
        background color). Screen-matrix bytes are used as *full 8-bit* charset
        indices — this is how the real VIC-II interprets them. Reverse video
        is not a hardware flag: the char ROM's second half (codes 128-255)
        contains pixel-inverted versions of codes 0-127, so the BASIC KERNAL
        gets reverse output "for free" just by flipping bit 7 of the screen
        RAM byte.

        ``bg_fill_color``, when provided, is painted under the row before the
        glyphs are drawn. Callers that already cleared the whole content area
        can pass ``None`` to skip it.
        """
        mem = self.emulator.memory.ram
        extended_color = bool(mode_info.get("extended_color", False))
        multicolor_text = bool(mode_info.get("multicolor")) and not extended_color
        color_base = COLOR_MEM
        screen_left = self._screen_rect.left
        if bg_fill_color is not None:
            self._rgb_frame.fill_rect(
                screen_left, y, self._screen_rect.width, self.CHAR_HEIGHT, bg_fill_color
            )
        row_offset = row * self.SCREEN_COLS
        for col in range(self.SCREEN_COLS):
            idx = row_offset + col
            raw_code = mem[(screen_base + idx) & 0xFFFF]
            color_code = mem[color_base + idx] & 0x0F
            x = screen_left + col * self.CHAR_WIDTH

            if extended_color:
                # ECM uses the top two bits of the byte as a 4-way background
                # picker; only screen codes $00-$3F address glyphs.
                bg_index = (raw_code >> 6) & 0x03
                code_ecm = raw_code & 0x3F
                char_bg = self._palette.get(bg_colors[bg_index], (0, 0, 0))
                self._rgb_frame.fill_rect(x, y, self.CHAR_WIDTH, self.CHAR_HEIGHT, char_bg)
                row_bytes = self._fetch_glyph_rows(vic_bank, char_base, code_ecm)
                self._plot_hires_text_cell(x, y, row_bytes, color_code)
                continue

            row_bytes = self._fetch_glyph_rows(vic_bank, char_base, raw_code)
            if multicolor_text and (color_code & 0x08):
                self._plot_multicolor_text_cell(
                    x,
                    y,
                    row_bytes,
                    color_code & 0x07,
                    bg_colors[0],
                    bg_colors[1],
                    bg_colors[2],
                )
            else:
                fg = (color_code & 0x07) if multicolor_text else color_code
                self._plot_hires_text_cell(x, y, row_bytes, fg)

    def _render_row_bitmap(
        self,
        row: int,
        y: int,
        vic_bank: int,
        screen_base: int,
        bitmap_base: int,
        mode_info: dict,
        bg0_color_idx: int,
    ) -> None:
        """Render a single 8-pixel-tall content row in bitmap mode.

        Hi-res (``mode_info["multicolor"] == False``): 320x8 px, each byte
        drives 8 pixels with ``color_data`` hi-nibble = FG, lo-nibble = BG.

        Multicolor (``mode_info["multicolor"] == True``): 160x8 px, 2 bits per
        wide pixel with lookup 00/01/10/11 → $D021 / color_data hi / color_data
        lo / color RAM.

        ``bg0_color_idx`` is the live ``$D021`` for this row (passed separately
        because the beam path reads it from per-raster samples).
        """
        mem = self.emulator.memory.ram
        screen_left = self._screen_rect.left
        multicolor = bool(mode_info.get("multicolor"))
        for col in range(40):
            char_index = row * 40 + col
            color_data = mem[(screen_base + char_index) & 0xFFFF]
            color_mem = mem[COLOR_MEM + char_index] & 0x0F
            bitmap_offset = char_index * 8
            base_x = screen_left + col * 8
            if multicolor:
                color1 = (color_data >> 4) & 0x0F
                color2 = color_data & 0x0F
                color3 = color_mem
                for r in range(8):
                    byte = mem[(bitmap_base + bitmap_offset + r) & 0xFFFF]
                    yy = y + r
                    for bit_pair in range(4):
                        pixel_bits = (byte >> (6 - bit_pair * 2)) & 0x03
                        if pixel_bits == 0:
                            c = self._palette.get(bg0_color_idx, (0, 0, 0))
                        elif pixel_bits == 1:
                            c = self._palette.get(color1, (0, 0, 0))
                        elif pixel_bits == 2:
                            c = self._palette.get(color2, (0, 0, 0))
                        else:
                            c = self._palette.get(color3, (0, 0, 0))
                        self._rgb_frame.fill_rect(base_x + bit_pair * 2, yy, 2, 1, c)
            else:
                color1 = (color_data >> 4) & 0x0F
                color0 = color_data & 0x0F
                for r in range(8):
                    byte = mem[(bitmap_base + bitmap_offset + r) & 0xFFFF]
                    yy = y + r
                    for bit in range(8):
                        pixel_bit = (byte >> (7 - bit)) & 0x01
                        c = self._palette.get(color1 if pixel_bit else color0, (0, 0, 0))
                        self._rgb_frame.put_pixel(base_x + bit, yy, c)

    def _render_bitmap_mode(self, mode_info: dict, snap: Optional[Tuple[bytes, int]] = None) -> None:
        """Render bitmap mode (standard or multicolor); wrapper over per-row helper."""
        vic_bank = self.emulator.memory.get_render_vic_bank_base()
        bitmap_base = (vic_bank + mode_info['bitmap_base']) & 0xFFFF
        screen_base = (vic_bank + mode_info['screen_base']) & 0xFFFF
        screen_top = self._screen_rect.top
        if snap:
            regb, _ = snap
            bg0 = regb[0x21] & 0x0F if len(regb) > 0x21 else 6
        else:
            bg0 = self.emulator.memory.read(0xD021) & 0x0F

        for char_row in range(25):
            y = screen_top + char_row * 8
            self._render_row_bitmap(
                char_row, y, vic_bank, screen_base, bitmap_base, mode_info, bg0,
            )

    def _render_sprites(self, snap: Optional[Tuple[bytes, int]] = None) -> None:
        """Render sprites on top of the display."""
        if self._rgb_frame is None:
            return

        mem = self.emulator.memory.ram
        screen_left = self._screen_rect.left
        screen_top = self._screen_rect.top
        
        if snap:
            regb, _ = snap
            sprite_mc0 = regb[0x25] & 0x0F if len(regb) > 0x25 else 0
            sprite_mc1 = regb[0x26] & 0x0F if len(regb) > 0x26 else 0
        else:
            sprite_mc0 = self.emulator.memory.read(0xD025) & 0x0F
            sprite_mc1 = self.emulator.memory.read(0xD026) & 0x0F
        
        # Render sprites 0-7 (back to front)
        for sprite_num in range(8):
            sprite_data = self.emulator.memory.get_sprite_data(sprite_num, for_render=True)
            
            if not sprite_data['enabled']:
                continue
            
            # Get sprite bitmap data (63 bytes per sprite) in VIC bank RAM
            sprite_addr = sprite_data['sprite_ram_base']

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
                    byte_offset = (sprite_addr + row * 3) & 0xFFFF
                    row_data = (mem[byte_offset] << 16) | (mem[(byte_offset + 1) & 0xFFFF] << 8) | mem[(byte_offset + 2) & 0xFFFF]
                    
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
                            self._rgb_frame.fill_rect(px, py, 2, 1, color)
            else:
                # Hi-res sprite: 24x21
                for row in range(21):
                    byte_offset = (sprite_addr + row * 3) & 0xFFFF
                    row_data = (mem[byte_offset] << 16) | (mem[(byte_offset + 1) & 0xFFFF] << 8) | mem[(byte_offset + 2) & 0xFFFF]
                    
                    for bit in range(24):
                        pixel_bit = (row_data >> (23 - bit)) & 0x01
                        
                        if pixel_bit:
                            px = screen_left + sprite_x + bit
                            py = screen_top + sprite_y + row
                            # Check if pixel is within screen rect bounds
                            if (self._screen_rect.left <= px < self._screen_rect.right and 
                                self._screen_rect.top <= py < self._screen_rect.bottom):
                                self._rgb_frame.put_pixel(px, py, sprite_color)
