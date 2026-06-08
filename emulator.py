"""
C64 Emulator Main Class
"""

from __future__ import annotations

import os
import queue
import struct
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple, Union

from .keyboard_inject import InjectKeyRule

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    np = None
    HAS_NUMPY = False

from rich.console import Console
from rich.text import Text

from .constants import (
    BASIC_BOOT_CYCLES,
    COLOR_MEM,
    BLNSW,
    BLNCT,
    BORDER_WIDTH,
    BORDER_HEIGHT,
    CPU_CLOCK_NTSC_HZ,
    CPU_CLOCK_PAL_HZ,
    CURSOR_COL_ADDR,
    CURSOR_ROW_ADDR,
    KEYBOARD_BUFFER_BASE,
    KEYBOARD_BUFFER_LEN_ADDR,
    KEYBOARD_BUFFER_SIZE,
    ROM_KERNAL_START,
    ROM_KERNAL_END,
    SCREEN_MEM,
    SCREEN_COLS,
    SCREEN_ROWS,
)
from .cpu import CPU6502
from .debug import UdpDebugLogger
from .memory import MemoryMap
from .roms import REQUIRED_ROMS
from .ui import TextualInterface
from .iec_bus import IECBus
from .drives.tcp_drive_client import TcpDriveClient
from .drives.iec_backend import IECDriveBackend

class C64:
    """Main C64 emulator"""

    # C64 16-color palette (RGB), Pepto/VICE-like approximation.
    # Index matches C64 color codes (0-15) used by BASIC/VIC.
    _C64_PALETTE_RGB: Tuple[Tuple[int, int, int], ...] = (
        (0x00, 0x00, 0x00),  # 0  black
        (0xFF, 0xFF, 0xFF),  # 1  white
        (0x88, 0x00, 0x00),  # 2  red
        (0xAA, 0xFF, 0xEE),  # 3  cyan
        (0xCC, 0x44, 0xCC),  # 4  purple
        (0x00, 0xCC, 0x55),  # 5  green
        (0x00, 0x00, 0xAA),  # 6  blue
        (0xEE, 0xEE, 0x77),  # 7  yellow
        (0xDD, 0x88, 0x55),  # 8  orange
        (0x66, 0x44, 0x00),  # 9  brown
        (0xFF, 0x77, 0x77),  # 10 light red
        (0x33, 0x33, 0x33),  # 11 dark gray
        (0x77, 0x77, 0x77),  # 12 gray
        (0xAA, 0xFF, 0x66),  # 13 light green
        (0x00, 0x88, 0xFF),  # 14 light blue
        (0xBB, 0xBB, 0xBB),  # 15 light gray
    )

    def __init__(
        self,
        interface_factory=None,
        enable_sid: bool = False,
        enable_resid: bool = False,
        audio_volume: float = 1.0,
        vic_emulation: str = "fast",
        disk_emulation: str = "fast",
    ):
        allowed_vic = frozenset({"fast", "accurate-python", "accurate-rust"})
        if vic_emulation not in allowed_vic:
            raise ValueError(
                f"vic_emulation must be one of {sorted(allowed_vic)}, got {vic_emulation!r}"
            )
        allowed_disk = frozenset({"fast", "accurate-python", "accurate-rust"})
        if disk_emulation not in allowed_disk:
            raise ValueError(
                f"disk_emulation must be one of {sorted(allowed_disk)}, got {disk_emulation!r}"
            )
        self.disk_emulation = disk_emulation
        self.vic_emulation = vic_emulation
        accurate_vic = vic_emulation != "fast"
        rust_hybrid_vic = vic_emulation == "accurate-rust"
        self.memory = MemoryMap()
        if interface_factory is None:
            self.interface = TextualInterface(self)
        else:
            self.interface = interface_factory(self)

        # Create CPU with interface reference
        self.cpu = CPU6502(
            self.memory,
            self.interface,
            accurate_vic=accurate_vic,
            rust_hybrid_vic=rust_hybrid_vic,
        )
        self.accurate_vic = accurate_vic
        self.sid = None

        self.running = False
        # Use NumPy arrays for faster screen operations (fallback to lists if unavailable)
        if HAS_NUMPY:
            self.text_screen = np.full((25, 40), ' ', dtype='U1')  # Unicode chars
            self.text_colors = np.full((25, 40), 7, dtype=np.uint8)  # Default: yellow
            self.text_reversed = np.zeros((25, 40), dtype=np.bool_)  # Track reversed chars
        else:
            self.text_screen = [[' '] * 40 for _ in range(25)]
            self.text_colors = [[7] * 40 for _ in range(25)]
            self.text_reversed = [[False] * 40 for _ in range(25)]
        self.debug = False
        self.no_colors = False  # ANSI color output enabled by default
        self.udp_debug = None  # Will be set if UDP debugging is enabled
        self.vice_trace = None  # Will be set if VICE-compatible tracing is enabled
        self.screen_update_thread = None
        self.screen_update_interval = 0.0167  # Update screen every 16.7ms (60Hz)
        self.screen_lock = threading.Lock()
        self.current_cycles = 0  # Track current cycle count
        self.program_loaded = False  # Track if a program was loaded via command line
        self.prg_file_path = None  # Store PRG file path to load after BASIC is ready
        # Snapshot scheduling (see snapshot.py / C64.py CLI flags).
        self._snapshot_at_cycle: Optional[tuple[int, str, str]] = None  # (cycle, path, note)
        self._snapshot_at_exit: Optional[tuple[str, str]] = None  # (path, note)
        self._snapshot_runtime_request: Optional[tuple[str, str]] = None  # Alt+S, TCP, etc.
        self.screen_update_callback = None  # Callback for screen updates (set by interface)
        self.turbo = False  # When True, no wall-clock throttling (see --turbo)
        self.inject_key_rules: List[InjectKeyRule] = []

        self.disk_image_path = None  # Store D64 path to attach after BASIC is ready

        self.monitor_server = None  # type: ignore[var-annotated]
        self.monitor_breakpoints: set[int] = set()
        self._monitor_cmd_queue: Optional[queue.Queue] = None
        self._monitor_reply_queue: Optional[queue.Queue] = None
        self._monitor_pending_step_ack = False

        # IEC serial bus for 1541 drive emulation (optional, created when needed)
        self.iec_bus: Optional[IECBus] = None
        self.iec_drives: Dict[int, IECDriveBackend] = {}  # TCP drive clients
        self.use_iec_bus = False  # Enable when 1541 ROMs are available
        # When True (default), the Python KERNAL LOAD/SAVE hooks at $FFD5/$FFD8
        # short-circuit disk I/O by serving the requested file directly from the
        # `D64Image`. Set False for `disk_emulation` accurate-python /
        # `accurate-rust` so the real KERNAL code paths talk to the 1541 over
        # the IEC bus (exercises the bit-level handshake + DOS ROM end to end).
        self.kernal_load_shortcut_enabled: bool = True
        # Thread-safe queue: UI/server threads must not write $C6/$0277 directly (races KERNAL
        # CHRIN on the CPU thread). Producers call send_petscii; CPU thread drains in sync_keyboard_host_queue.
        self._keyboard_incoming: queue.Queue[int] = queue.Queue(maxsize=64)

        # Dirty-checking for screen updates - use bytes for fast comparison
        self._prev_screen_data = b''
        self._prev_color_data = b''
        self._screen_dirty = False  # Flag for manual screen updates
        self._screen_dirty = True  # Force initial render

        # Backward compatibility
        self.rich_interface = self.interface

        if enable_resid:
            try:
                from .resid import ReSIDEmulator
                self.sid = ReSIDEmulator(
                    video_standard=self.memory.video_standard,
                    cpu_lockstep=accurate_vic,
                    audio_volume=audio_volume,
                )
                self.memory.sid = self.sid
                if self.interface:
                    self.interface.add_debug_log("🔊 reSID audio enabled (VICE-Team reSID)")
            except ImportError as exc:
                if self.interface:
                    self.interface.add_debug_log(f"⚠️ reSID library not found: {exc}")
            except Exception as exc:
                if self.interface:
                    self.interface.add_debug_log(f"⚠️ reSID initialisation failed: {exc}")
        elif enable_sid:
            try:
                from .sid import SidEmulator
                self.sid = SidEmulator(video_standard=self.memory.video_standard)
                self.memory.sid = self.sid
                if self.interface:
                    self.interface.add_debug_log("🔊 SID audio enabled")
            except Exception as exc:
                if self.interface:
                    self.interface.add_debug_log(f"⚠️ SID audio unavailable: {exc}")

    # --- Wall-clock throttling (PAL/NTSC) unless turbo ---
    #
    # Future direction (major refactor): drive the core from a *priority queue of events*
    # (CIA ticks, VIC raster IRQs, disk sector, etc.), each tagged with emulated cycle time.
    # The host wakes at min(next_event) using a monotonic clock; run CPU until that cycle
    # count, dispatch devices, repeat. You still batch 6502 instructions between events;
    # you cannot get a true 985 kHz POSIX signal, but frame/audio callbacks (~50–44100 Hz)
    # can anchor wall time. See also: audio callback pacing, asyncio call_later.
    #
    _SPEED_THROTTLE_INTERVAL = 50_000
    # Learn mean sleep overshoot (actual − requested) per host; subtract from next request.
    _SPEED_THROTTLE_OVERSHOOT_EMA_ALPHA = 0.12
    _SPEED_THROTTLE_OVERSHOOT_EMA_MAX = 0.018  # clamp ± (seconds)
    # Below this, time.sleep() precision is dominated by the OS; skip and let deadline absorb.
    _SPEED_THROTTLE_MIN_SLEEP_SEC = 0.0002
    # Once per second: if measured Hz is off nominal, nudge overshoot EMA (closed loop).
    _SPEED_THROTTLE_HZ_NUDGE_SLOW = 0.00035  # add to EMA when too slow (sleep less next times)
    _SPEED_THROTTLE_HZ_NUDGE_FAST = 0.00022  # subtract when too fast

    @property
    def target_cpu_hz(self) -> float:
        std = (self.memory.video_standard or "pal").lower()
        return float(CPU_CLOCK_PAL_HZ) if std == "pal" else float(CPU_CLOCK_NTSC_HZ)

    def reset_speed_throttle(self) -> None:
        """Call when starting a new emulation run (resets wall-clock baseline)."""
        wall = time.perf_counter()
        self._speed_throttle_run_wall_start = wall
        self._speed_throttle_checkpoint = 0
        # Ideal wall-clock time at which the current checkpoint should have been reached.
        self._speed_throttle_deadline = wall
        self._speed_sleep_overshoot_ema = 0.0
        self._speed_throttle_sec_anchor_wall = None
        self._speed_throttle_sec_anchor_cycles = 0

    def _speed_throttle_per_second_log_and_tune(self, cycles: int) -> None:
        """Every ~1 s wall: log achieved MHz, EMA, and nudge EMA if off nominal."""
        now = time.perf_counter()
        anchor_w = self._speed_throttle_sec_anchor_wall
        if anchor_w is None:
            self._speed_throttle_sec_anchor_wall = now
            self._speed_throttle_sec_anchor_cycles = cycles
            return
        dt = now - anchor_w
        if dt < 1.0:
            return
        dc = cycles - self._speed_throttle_sec_anchor_cycles
        self._speed_throttle_sec_anchor_wall = now
        self._speed_throttle_sec_anchor_cycles = cycles
        if dc <= 0:
            return
        hz_ach = dc / dt
        tgt = self.target_cpu_hz
        ema = getattr(self, "_speed_sleep_overshoot_ema", 0.0)
        mx = self._SPEED_THROTTLE_OVERSHOOT_EMA_MAX
        if getattr(self, "debug", False):
            msg = (
                f"⏱ throttle: ~{hz_ach / 1e6:.3f} MHz actual vs {tgt / 1e6:.3f} MHz target; "
                f"sleep_overshoot_ema={ema * 1000:.3f} ms"
            )
            iface = getattr(self, "interface", None) or getattr(self, "rich_interface", None)
            if iface and hasattr(iface, "add_debug_log"):
                iface.add_debug_log(msg)
            else:
                print(msg, flush=True)
        if hz_ach < tgt * 0.995:
            ema = min(mx, ema + self._SPEED_THROTTLE_HZ_NUDGE_SLOW)
        elif hz_ach > tgt * 1.005:
            ema = max(-mx, ema - self._SPEED_THROTTLE_HZ_NUDGE_FAST)
        self._speed_sleep_overshoot_ema = max(-mx, min(mx, ema))

    def throttle_emulation_if_needed(self, cycles: int) -> None:
        """Sleep if we're ahead of real time for the configured CPU clock (no-op if turbo).

        Cumulative *deadline* (+= span/hz) sets the target wall time. Each ``time.sleep`` is
        shortened by an EMA of (actual_sleep − requested_sleep) so different OS/schedulers
        converge toward ~100% of nominal PAL/NTSC MHz without per-machine constants.

        Event queues / high-resolution host timers are useful for *wall-clock* sync (audio,
        vsync, UI). They do not replace per-cycle CPU/VIC stepping when modeling BA stalls
        and IRQ phase; that still requires advancing the chip model each emulated cycle
        (or a proven fast-forward with equivalent state).

        """
        if getattr(self, "turbo", False):
            return
        if not hasattr(self, "_speed_throttle_deadline"):
            self.reset_speed_throttle()
        self._speed_throttle_per_second_log_and_tune(cycles)
        if cycles - self._speed_throttle_checkpoint < self._SPEED_THROTTLE_INTERVAL:
            return
        span = cycles - self._speed_throttle_checkpoint
        dt_emulated = span / self.target_cpu_hz
        self._speed_throttle_deadline += dt_emulated
        now = time.perf_counter()
        delay = self._speed_throttle_deadline - now
        if delay > 0:
            ema = getattr(self, "_speed_sleep_overshoot_ema", 0.0)
            corrected = max(0.0, delay - ema)
            mn = self._SPEED_THROTTLE_MIN_SLEEP_SEC
            if corrected >= mn:
                t0 = time.perf_counter()
                time.sleep(corrected)
                err = (time.perf_counter() - t0) - corrected
                a = self._SPEED_THROTTLE_OVERSHOOT_EMA_ALPHA
                mx = self._SPEED_THROTTLE_OVERSHOOT_EMA_MAX
                ema = (1.0 - a) * ema + a * err
                ema = max(-mx, min(mx, ema))
                self._speed_sleep_overshoot_ema = ema
        self._speed_throttle_checkpoint = cycles

    def load_roms(self, rom_dir: str, *, require_char_rom: bool = True) -> None:
        """Load C64 ROM files

        Args:
            rom_dir: Absolute path to directory containing ROM files
            require_char_rom: Whether the character ROM must be present
        """
        import os

        def _read_rom_file(filename: str) -> bytes:
            """
            Read a ROM file from rom_dir.

            Supports both c64py's canonical dot-names and common VICE dash-names.
            """
            # Build name_candidates from REQUIRED_ROMS to maintain single source of truth
            name_candidates = (filename,)
            for spec in REQUIRED_ROMS:
                if spec.filename == filename:
                    name_candidates = (spec.filename, *spec.aliases)
                    break

            tried_paths = []
            for name in name_candidates:
                path = os.path.join(rom_dir, name)
                tried_paths.append(path)
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        return f.read()
            tried_paths_str = ", ".join(tried_paths) if tried_paths else "<no paths constructed>"
            raise FileNotFoundError(
                f"ROM not found. Tried candidate names {list(name_candidates)} at paths: {tried_paths_str}"
            )

        try:
            # Load BASIC ROM
            self.memory.basic_rom = _read_rom_file("basic.901226-01.bin")
            if self.rich_interface:
                self.rich_interface.add_debug_log(f"💾 Loaded BASIC ROM: {len(self.memory.basic_rom)} bytes")

            # Load KERNAL ROM
            self.memory.kernal_rom = _read_rom_file("kernal.901227-03.bin")
            if self.rich_interface:
                self.rich_interface.add_debug_log(f"💾 Loaded KERNAL ROM: {len(self.memory.kernal_rom)} bytes")

            # Set reset vector in RAM (KERNAL ROM has it at $FFFC-$FFFD)
            if self.memory.kernal_rom and len(self.memory.kernal_rom) >= (0x10000 - ROM_KERNAL_START):
                reset_offset = 0xFFFC - ROM_KERNAL_START
                reset_low = self.memory.kernal_rom[reset_offset]
                reset_high = self.memory.kernal_rom[reset_offset + 1]
                self.memory.ram[0xFFFC] = reset_low
                self.memory.ram[0xFFFD] = reset_high
                if self.rich_interface:
                    self.rich_interface.add_debug_log(f"🔄 Reset vector: ${reset_high:02X}{reset_low:02X}")

            # Load Character ROM (optional for text-only mode)
            if require_char_rom:
                self.memory.char_rom = _read_rom_file("characters.901225-01.bin")
                if self.rich_interface:
                    self.rich_interface.add_debug_log(f"💾 Loaded Character ROM: {len(self.memory.char_rom)} bytes")
            else:
                try:
                    self.memory.char_rom = _read_rom_file("characters.901225-01.bin")
                    if self.rich_interface:
                        self.rich_interface.add_debug_log(
                            f"💾 Loaded Character ROM: {len(self.memory.char_rom)} bytes"
                        )
                except FileNotFoundError:
                    self.memory.char_rom = None
                    if self.rich_interface:
                        self.rich_interface.add_debug_log(
                            "⚠️ Character ROM not found; text-only mode will still run"
                        )
        except Exception:
            # Stop textual UI if it exists so error is visible to user.
            if hasattr(self, "interface") and hasattr(self.interface, "exit"):
                try:
                    self.interface.exit()
                except Exception as exit_err:
                    # Best-effort cleanup: log failure to exit interface but do not mask the original error.
                    sys.stderr.write(f"Failed to cleanly exit interface: {exit_err}\n")
            raise

        # Initialize C64 state (sets memory config $01 = 0x37)
        self._initialize_c64()

        # Set CPU PC from reset vector (after ROMs are loaded and memory is initialized)
        # Use _read_word to ensure we read from KERNAL ROM correctly
        reset_addr = self.cpu._read_word(0xFFFC)
        self.cpu.state.pc = reset_addr
        if self.rich_interface:
            self.rich_interface.add_debug_log(f"🔄 CPU reset vector: ${reset_addr:04X}")

    def _initialize_c64(self) -> None:
        """Initialize C64 to a known state"""
        # Initialize RAM with C64-like pattern (real C64 DRAM has pattern: half $00, half $FF in 64-byte blocks)
        # For debugging, zero $0002-$03FF as per RAMTAS at $FD50
        # Pattern: 64 bytes of $00, then 64 bytes of $FF, repeating
        for addr in range(0x0002, 0x0400):
            # RAMTAS zeros this area
            self.memory.ram[addr] = 0x00

        # Initialize rest of RAM with pattern (real C64 DRAM characteristic)
        # Pattern: 64 bytes of $00, then 64 bytes of $FF, repeating
        for addr in range(0x0400, 0x10000):
            if addr < 0x0800 or (addr >= 0xA000 and addr < 0xC000) or addr >= 0xE000:
                # Skip screen/color memory ($0400-$07FF) and ROM areas
                # Apply pattern to other RAM areas
                block = (addr // 64) % 2
                if block == 0:
                    self.memory.ram[addr] = 0x00
                else:
                    self.memory.ram[addr] = 0xFF

        # Write to $0000 during reset (as JSC64 does)
        # This is part of the 6510 processor port initialization
        self.memory.ram[0x00] = 0x2F

        # Memory configuration register ($01)
        # Bits 0-2: Memory configuration
        # 0x37 = %00110111 = BASIC ROM + KERNAL ROM + I/O enabled
        self.memory.ram[0x01] = 0x37
        self.memory.invalidate_6510_port_read_cache()

        # Initialize screen memory with spaces (don't pre-fill - let KERNAL/BASIC do it)
        # The C64 typically clears screen during initialization
        for addr in range(SCREEN_MEM, SCREEN_MEM + 1000):
            self.memory.ram[addr] = 0x20  # Space character

        # Initialize color memory (default: light blue = 14, but we'll use white = 1)
        for addr in range(COLOR_MEM, COLOR_MEM + 1000):
            # Default C64 power-on text is light blue on blue.
            # Color RAM is 4-bit and lives in I/O space; it is handled by MemoryMap.
            self.memory.ram[addr] = 0x0E  # Light blue

        # Initialize VIC registers (simplified)
        # VIC register $D018: Screen and character memory
        # Bits 7-4 (vm): video matrix base (1 → $0400)
        # Bits 3-1 (cb): char base (2 → offset $1000 within VIC bank, which is
        #                 the char ROM window in banks 0 and 2)
        # $D018 = %00010101 = $15 → screen at $0400, chars at char ROM (uppercase)
        # Seed key VIC state so early frames render like a real C64, even before ROM init.
        # Use memory-mapped I/O writes so the values land in the VIC register model.
        self.memory.poke_vic(0x18, 0x15)  # Screen at $0400, chars at char ROM ($1000)
        self.memory.poke_vic(0x20, 0x0E)  # Border: light blue
        self.memory.poke_vic(0x21, 0x06)  # Background: blue

        # Initialize stack pointer
        self.cpu.state.sp = 0xFF

        # Initialize zero-page variables used by KERNAL
        # $C3-$C4: Temporary pointer used by vector copy routine
        # Typically initialized to point to RAM vector area (0x0314)
        self.memory.ram[0xC3] = 0x14  # Temporary pointer (low)
        self.memory.ram[0xC4] = 0x03  # Temporary pointer (high) - points to $0314

        # Initialize some zero-page variables
        self.memory.ram[0x0288] = 0x0E  # Cursor color (light blue)
        self.memory.ram[0x0286] = 0x0E  # Current text color (light blue)
        # Cursor blink (machine-controlled; UI should follow this)
        # bit0 = enabled, bit7 = visible
        self.memory.ram[BLNSW] = 0x81  # Set bit 7 for initial visibility and bit 0 for enabled
        self.memory.ram[BLNCT] = 0

        # Initialize cursor position (points to screen start)
        # $D1/$D2 store the cursor address (low/high bytes)
        self.memory.ram[0xD1] = SCREEN_MEM & 0xFF  # Cursor address low byte
        self.memory.ram[0xD2] = (SCREEN_MEM >> 8) & 0xFF  # Cursor address high byte
        # Also initialize cursor row/col variables
        self.memory.ram[CURSOR_ROW_ADDR] = 0  # Cursor row (0-24)
        self.memory.ram[CURSOR_COL_ADDR] = 0  # Cursor column (0-39)

        # Initialize KERNAL reset vector at $8000-$8001 to point to BASIC cold start
        # The KERNAL does JMP ($8000) to jump to BASIC after initialization
        # BASIC cold start is typically at $A483 (standard C64 BASIC entry point)
        basic_cold_start = 0xA483
        self.memory.ram[0x8000] = basic_cold_start & 0xFF
        self.memory.ram[0x8001] = (basic_cold_start >> 8) & 0xFF

        # Initialize BASIC pointers for empty program
        basic_start = 0x0801
        # $2B/$2C: Start of BASIC program (VARPTR)
        self.memory.ram[0x002B] = basic_start & 0xFF
        self.memory.ram[0x002C] = (basic_start >> 8) & 0xFF
        # $2D/$2E: End of BASIC program (VAREND)
        self.memory.ram[0x002D] = basic_start & 0xFF
        self.memory.ram[0x002E] = (basic_start >> 8) & 0xFF
        # $2F/$30: Start of BASIC arrays (ARRPTR)
        self.memory.ram[0x002F] = basic_start & 0xFF
        self.memory.ram[0x0030] = (basic_start >> 8) & 0xFF
        # $31/$32: End of BASIC arrays (ARREND)
        self.memory.ram[0x0031] = basic_start & 0xFF
        self.memory.ram[0x0032] = (basic_start >> 8) & 0xFF
        # $33/$34: End of free memory (STREND) - should point to end of available memory
        # This will be set by MEMTOP routine, but initialize to a safe value
        # MEMTOP typically returns $9FFF for 64K system
        memtop = 0x9FFF  # Default top of BASIC RAM
        self.memory.ram[0x0033] = memtop & 0xFF
        self.memory.ram[0x0034] = (memtop >> 8) & 0xFF

        # Mark end of BASIC program (empty program marker)
        # $0801-$0802: Link to next line ($00 $00 = end of program)
        # This is CRITICAL - if this is not $00 $00, BASIC will try to execute garbage as a program
        # The link pointer must be $00 $00 to indicate no program
        # NOTE: This will be overwritten when a PRG file is loaded at $0801
        self.memory.ram[0x0801] = 0x00
        self.memory.ram[0x0802] = 0x00

        # Also ensure $0803+ is cleared to prevent garbage being interpreted as tokens
        # Clear a reasonable amount of BASIC program area
        # NOTE: This will be overwritten when a PRG file is loaded
        for addr in range(0x0803, 0x0900):
            self.memory.ram[addr] = 0x00

        # Initialize current line number for direct mode
        # $39/$3A: Current line number (low/high)
        # $3A = $FF means direct mode (no line number)
        self.memory.ram[0x0039] = 0x00  # Low byte
        self.memory.ram[0x003A] = 0xFF  # High byte = $FF means direct mode

        # Initialize keyboard buffer (for GETIN)
        self.memory.ram[0xC6] = 0  # Number of characters in keyboard buffer
        # Clear keyboard buffer area ($0277-$0280)
        for i in range(10):
            self.memory.ram[0x0277 + i] = 0

        # Initialize BASIC input buffer (for CHRIN keyboard input)
        # $0200-$0258: BASIC input buffer (89 bytes)
        # $029B: Input buffer read pointer (0 = empty, >0 = chars available)
        # $029C: Line editing length counter (temporary, during line editing)
        self.memory.ram[0x029B] = 0  # Input buffer pointer (0 = empty)
        self.memory.ram[0x029C] = 0  # Line editing length (0 = no line being edited)
        # Clear BASIC input buffer
        for i in range(89):
            self.memory.ram[0x0200 + i] = 0

        # Initialize zero-page status register $6C (used by KERNAL error handler)
        # This is typically initialized to 0 on boot
        # The KERNAL checks this at $FE6E with SBC $6C - if result is 0, it halts
        self.memory.ram[0x6C] = 0  # Status register (typically 0 = no error)

        # Initialize KERNAL vectors to defaults
        # These are copied from KERNAL ROM during RESTOR routine
        # We initialize them here to prevent crashes during boot

        # KERNAL RAM vectors ($0300-$0334)
        # These should match the default values from KERNAL ROM
        kernal_vectors = {
            0x0300: 0xE45B,  # CINT - Initialize screen editor
            0x0302: 0xFE4C,  # IOINIT - Initialize I/O
            0x0304: 0xFDA3,  # RAMTAS - Initialize RAM
            0x0306: 0xED50,  # RESTOR - Restore KERNAL vectors
            0x0308: 0xFD4C,  # VECTOR - Change KERNAL vectors
            0x030A: 0x15FD,  # SETMSG - Set system error display
            0x030C: 0xED1A,  # LSTNSA - Send LIST to serial bus
            0x030E: 0xFD4C,  # TALKSA - Send TALK to serial bus
            0x0310: 0x18FE,  # MEMTOP - Set top of memory
            0x0312: 0x4CB9,  # MEMBOT - Set bottom of memory
            0x0314: 0xEA31,  # IRQ - IRQ handler
            0x0316: 0xFE66,  # BRK - BRK handler
            0x0318: 0xFE47,  # NMI - NMI handler
            0x031A: 0xFE4C,  # OPEN - Open file
            0x031C: 0x34FE,  # CLOSE - Close file
            0x031E: 0x4C87,  # CHKIN - Set input channel
            0x0320: 0xEA4C,  # CHKOUT - Set output channel
            0x0322: 0x21FE,  # CLRCHN - Clear channels
            0x0324: 0x4C13,  # CHRIN - Input character ($FFCF)
            0x0326: 0xEE4C,  # CHROUT - Output character
            0x0328: 0xDDED,  # STOP - Check stop key
            0x032A: 0x4CEF,  # GETIN - Get character from keyboard
            0x032C: 0xED4C,  # CLALL - Clear file table
            0x032E: 0xFEED,  # UDTIM - Update clock
            0x0330: 0x4C0C,  # SCREEN - Get screen size
            0x0332: 0xED4C,  # PLOT - Set cursor position
            0x0334: 0x09ED,  # IOBASE - Get I/O base address
        }

        for addr, value in kernal_vectors.items():
            self.memory.ram[addr] = value & 0xFF
            self.memory.ram[addr + 1] = (value >> 8) & 0xFF

        # Initialize CIA1 timers (typical C64 boot values)
        # Timer A is used for jiffy clock (exactly 60Hz)
        # PAL C64: ~1.022727 MHz CPU, so 60Hz = 17045.45 cycles
        # We use 17045 for accuracy
        if self.memory.video_standard == "pal":
            cpu_hz = 1022727  # PAL C64 CPU frequency
        else:
            cpu_hz = 985248   # NTSC C64 CPU frequency

        jiffy_cycles = cpu_hz // 60  # Exact 60Hz timing
        self.memory.cia1_timer_a.latch = jiffy_cycles
        self.memory.cia1_timer_a.counter = jiffy_cycles
        self.memory.cia1_timer_a.running = True   # Enable jiffy clock
        self.memory.cia1_timer_a.irq_enabled = True

        # Timer B can be used for other purposes
        self.memory.cia1_timer_b.latch = 0xFFFF
        self.memory.cia1_timer_b.counter = 0xFFFF

        if self.rich_interface:
            self.rich_interface.add_debug_log("🎮 C64 initialized")

    def set_video_standard(self, standard: str) -> None:
        self.memory.video_standard = standard
        self.cpu.apply_video_standard_geometry()
        if self.sid:
            self.sid.set_video_standard(standard)

    def shutdown(self) -> None:
        if self.sid:
            self.sid.close()
            self.sid = None
            self.memory.sid = None

    def save_snapshot(self, path, *, note: str = "") -> str:
        """Save a snapshot of the current emulator state to *path*.

        See :mod:`c64py.snapshot` for the file format and caveats (ROMs,
        IEC/disk, and SID internal state are intentionally not captured).
        """
        from .snapshot import save_snapshot as _save
        out = _save(self, path, note=note)
        msg = f"💾 Snapshot saved: {out} (cycle={int(self.current_cycles)})"
        if self.interface and hasattr(self.interface, "add_debug_log"):
            self.interface.add_debug_log(msg)
        print(msg, flush=True)
        return str(out)

    def load_snapshot(self, path) -> None:
        """Replace the current emulator state with a snapshot from *path*."""
        from .snapshot import load_snapshot as _load, describe_payload
        payload = _load(self, path)
        msg = f"📥 Snapshot loaded: {path} — {describe_payload(payload)}"
        if self.interface and hasattr(self.interface, "add_debug_log"):
            self.interface.add_debug_log(msg)
        print(msg, flush=True)

    def request_runtime_snapshot(self, path, *, note: str = "") -> None:
        """Queue a snapshot to be written on the CPU thread (Alt+S / signal).

        Writing from a UI thread while the CPU thread is mid-instruction would
        race the ``ram`` / VIC register arrays; the request is serviced between
        instructions by :meth:`_service_snapshot_requests`.
        """
        self._snapshot_runtime_request = (str(path), str(note))

    def _service_snapshot_requests(self) -> None:
        """Save queued snapshot (Alt+S / --save-snapshot-at-cycle) in-loop."""
        if getattr(self, "_snapshot_runtime_request", None) is not None:
            path, note = self._snapshot_runtime_request
            self._snapshot_runtime_request = None
            try:
                self.save_snapshot(path, note=note)
            except Exception as exc:
                err = f"❌ Snapshot save failed: {exc}"
                print(err, flush=True)
                if self.interface and hasattr(self.interface, "add_debug_log"):
                    self.interface.add_debug_log(err)
        sched = getattr(self, "_snapshot_at_cycle", None)
        if sched is not None:
            target_cycle, path, note = sched
            if self.current_cycles >= target_cycle:
                self._snapshot_at_cycle = None
                try:
                    self.save_snapshot(path, note=note)
                except Exception as exc:
                    print(f"❌ Scheduled snapshot failed: {exc}", flush=True)

    def load_prg(self, prg_path: str) -> None:
        """Load a PRG file into memory"""
        with open(prg_path, "rb") as f:
            data = f.read()

        if len(data) < 2:
            raise ValueError("PRG file too small")

        load_addr = data[0] | (data[1] << 8)
        prg_data = data[2:]

        # Write PRG data to memory
        for i, byte_val in enumerate(prg_data):
            addr = (load_addr + i) & 0xFFFF
            self.memory.write(addr, byte_val)

        self.program_loaded = True
        end_addr = load_addr + len(prg_data)
        print(f"Loaded PRG: {len(prg_data)} bytes at ${load_addr:04X}, end at ${end_addr:04X}")

        # If loaded at $0801 (BASIC), set up BASIC pointers
        if load_addr == 0x0801:
            # Set BASIC start pointer ($2B/$2C) - points to start of program
            self.memory.ram[0x002B] = 0x01
            self.memory.ram[0x002C] = 0x08

            # Set BASIC end pointer ($2D/$2E) - points to end of program
            # This should point to the address AFTER the $00 $00 end marker
            self.memory.ram[0x002D] = end_addr & 0xFF
            self.memory.ram[0x002E] = (end_addr >> 8) & 0xFF

            # Set variable/array pointers ($2F-$32) - same as end, no variables yet
            # ARYTAB ($2F/$30) - start of arrays
            self.memory.ram[0x002F] = end_addr & 0xFF
            self.memory.ram[0x0030] = (end_addr >> 8) & 0xFF
            # STREND ($31/$32) - end of arrays/start of free RAM
            self.memory.ram[0x0031] = end_addr & 0xFF
            self.memory.ram[0x0032] = (end_addr >> 8) & 0xFF

            # Debug: Log the BASIC pointers
            if self.interface:
                self.interface.add_debug_log(f"📝 BASIC start: ${self.memory.ram[0x002B] | (self.memory.ram[0x002C] << 8):04X}")
                self.interface.add_debug_log(f"📝 BASIC end: ${self.memory.ram[0x002D] | (self.memory.ram[0x002E] << 8):04X}")
                # Check if program has proper end marker
                if end_addr >= 2:
                    end_marker_low = self.memory.read(end_addr - 2)
                    end_marker_high = self.memory.read(end_addr - 1)
                    if end_marker_low == 0x00 and end_marker_high == 0x00:
                        self.interface.add_debug_log("✅ Program has proper $00 $00 end marker")
                    else:
                        self.interface.add_debug_log(f"⚠️ Program end marker: ${end_marker_low:02X} ${end_marker_high:02X} (expected $00 $00)")
                # Show first few bytes of program
                first_bytes = [f"${self.memory.read(0x0801 + i):02X}" for i in range(min(16, len(prg_data)))]
                self.interface.add_debug_log(f"📝 First bytes at $0801: {', '.join(first_bytes)}")

    def _inject_run_command(self) -> None:
        """Inject 'RUN' command into keyboard buffer for autorun."""
        
        # Put in keyboard buffer (raw keypresses)
        # Clear buffer first
        for i in range(10):
            self.memory.write(KEYBOARD_BUFFER_BASE + i, 0)

        # Write "RUN" + RETURN
        full_command = b"RUN\x0D"
        for i, char in enumerate(full_command):
            self.memory.write(KEYBOARD_BUFFER_BASE + i, char)

        # Set buffer length
        self.memory.write(KEYBOARD_BUFFER_LEN_ADDR, len(full_command))
        
        if self.interface:
            self.interface.add_debug_log("🏃 Injected 'RUN' command into keyboard buffer")

    def _fire_inject_key_rule(self, rule: InjectKeyRule, cpu_cycles: int) -> None:
        """Apply one ``--inject-keys`` rule (keyboard + optional joystick hold)."""
        from .keyboard_inject import expand_inject_payload

        kb, j1, j2, hold = expand_inject_payload(rule.payload_raw)
        dropped = 0
        for b in kb:
            if not self.send_petscii(int(b)):
                dropped += 1
        if dropped and self.interface:
            self.interface.add_debug_log(
                f"⌨️ inject-keys: {dropped} byte(s) dropped (keyboard buffer full)"
            )
        until = cpu_cycles + max(hold, 0)
        if j1:
            self.memory.arm_joystick_inject(1, j1, until)
        if j2:
            self.memory.arm_joystick_inject(2, j2, until)
        if self.interface:
            self.interface.add_debug_log(
                f"⌨️ inject-keys fired at cycle {cpu_cycles}: {rule.payload_raw!r}"
            )

    def _process_scheduled_inject_keys(self, cpu_cycles: int, wall_seconds: float) -> None:
        for rule in self.inject_key_rules:
            if rule.fired:
                continue
            if rule.when_cycles is not None:
                if cpu_cycles >= rule.when_cycles:
                    rule.fired = True
                    self._fire_inject_key_rule(rule, cpu_cycles)
            elif rule.when_seconds is not None and wall_seconds >= rule.when_seconds:
                rule.fired = True
                self._fire_inject_key_rule(rule, cpu_cycles)

    def _inject_load_directory_command(self, device: int = 8) -> None:
        """Inject 'LOAD"$",device' command into keyboard buffer to list disk directory."""
        
        # Put in keyboard buffer (raw keypresses)
        # Clear buffer first
        for i in range(10):
            self.memory.write(KEYBOARD_BUFFER_BASE + i, 0)

        # Write 'LOAD"$",8' + RETURN
        # PETSCII: L O A D " $ " , 8 RETURN (RETURN = 0x0D)
        command = f'LOAD"$",{device}\r'  # \r is carriage return (0x0D)
        command_bytes = command.encode('ascii')
        
        for i, char in enumerate(command_bytes):
            self.memory.write(KEYBOARD_BUFFER_BASE + i, char)

        # Set buffer length
        self.memory.write(KEYBOARD_BUFFER_LEN_ADDR, len(command_bytes))
        
        if self.interface:
            self.interface.add_debug_log(f"💾 Injected 'LOAD\"$\",{device}' command into keyboard buffer")

    def attach_disk(self, disk_path: str, device: int = 8) -> None:
        """Attach a D64 disk image to the TCP drive server for *device*.

        Sends an ``attach_disk`` RPC to the connected :class:`TcpDriveClient`.
        Falls back to a no-op (with a log warning) when no TCP client is
        registered for *device* — this preserves the previous call-site
        signature.

        Args:
            disk_path: Path to D64 disk image file
            device: Device number (8-11, default 8)
        """
        if device < 8 or device > 11:
            raise ValueError(f"Invalid device number: {device} (must be 8-11)")
        client = self.iec_drives.get(device)
        if client is None or not isinstance(client, TcpDriveClient):
            if self.interface:
                self.interface.add_debug_log(
                    f"⚠ attach_disk: no TCP client for device {device}, ignoring"
                )
            return
        ok = client.attach_disk_remote(disk_path)
        if self.interface:
            if ok:
                self.interface.add_debug_log(
                    f"💾 Disk attached on drive {device}: {disk_path}"
                )
            else:
                self.interface.add_debug_log(
                    f"❌ attach_disk RPC failed for device {device}"
                )

    def detach_disks(self) -> None:
        """Detach disk images from all connected TCP drive servers."""
        for device, client in self.iec_drives.items():
            if isinstance(client, TcpDriveClient):
                client.detach_disk_remote()
                if self.interface:
                    self.interface.add_debug_log(f"💾 Detached disk from drive {device}")

    def get_drive(self, device: int) -> Optional[TcpDriveClient]:
        """Return the TCP drive client for *device*, or None."""
        client = self.iec_drives.get(device)
        if isinstance(client, TcpDriveClient):
            return client
        return None
    
    def initialize_iec_bus(self, tcp_drives: Optional[Dict[int, str]] = None,
                           rom_dir: Optional[str] = None) -> bool:
        """Initialize IEC bus with TCP-connected drive clients.

        Each entry in ``tcp_drives`` maps a device number (8–11) to a
        ``"host:port"`` string.  A ``TcpDriveClient`` is created for each
        entry and attached to the ``IECBus``.  If ``tcp_drives`` is omitted
        the bus is created with no drives attached (drives can be added later
        via :meth:`attach_tcp_drive`).

        ``rom_dir`` is accepted for API compatibility but is no longer used —
        ROMs live in the standalone ``c1541_emulator`` server process.

        Args:
            tcp_drives: Optional mapping of device# → "host:port", e.g.
                        ``{8: "localhost:6408", 9: "localhost:6409"}``
            rom_dir: Ignored (kept for back-compat).

        Returns:
            True if the IEC bus was successfully initialized.
        """
        if self.iec_bus is not None:
            self._sync_iec_kernal_tap()
            return self.use_iec_bus

        self.iec_bus = IECBus()
        self.memory.iec_bus = self.iec_bus

        if tcp_drives:
            for device, addr in tcp_drives.items():
                self.attach_tcp_drive(device, addr)

        self.use_iec_bus = True
        self.cpu.kernal_disk_hook_vectors = False
        self.memory.iec_disk_full_impl = False
        if self.interface:
            n = len(self.iec_drives)
            self.interface.add_debug_log(
                f"✓ IEC serial bus initialized ({n} TCP drive(s) attached)"
            )
        self._sync_iec_kernal_tap()
        return True

    def _sync_iec_kernal_tap(self) -> None:
        """Install :class:`~c64py.iec_kernal_bridge.KernalIecTap` when TCP drives are attached."""
        m = self.memory
        if not getattr(self, "use_iec_bus", False) or self.iec_bus is None:
            m.iec_kernal_tap = None
            return
        has_tcp = any(isinstance(d, TcpDriveClient) for d in self.iec_drives.values())
        if has_tcp:
            if m.iec_kernal_tap is None:
                from .iec_kernal_bridge import KernalIecTap

                m.iec_kernal_tap = KernalIecTap()
        else:
            m.iec_kernal_tap = None

    def attach_tcp_drive(self, device: int, addr: str) -> bool:
        """Attach a TCP drive client for ``device`` at ``host:port``.

        Creates a :class:`TcpDriveClient`, attempts to connect, and registers
        it on the :class:`IECBus`.  The IEC bus must have been created first
        (call :meth:`initialize_iec_bus` without arguments if needed).

        Args:
            device: IEC device number (8–11).
            addr:   ``"host:port"`` string, e.g. ``"localhost:6408"``.

        Returns:
            True if the connection succeeded.
        """
        if self.iec_bus is None:
            self.initialize_iec_bus()

        if ":" not in addr:
            raise ValueError(f"addr must be 'host:port', got: {addr!r}")
        host, port_str = addr.rsplit(":", 1)
        port = int(port_str)

        # Detach any existing client for this device first.
        existing = self.iec_drives.get(device)
        if existing is not None and isinstance(existing, TcpDriveClient):
            existing.disconnect()
            self.iec_bus.detach_device(existing)

        client = TcpDriveClient(device_number=device, host=host, port=port)
        ok = client.connect()
        self.iec_bus.attach_device(client)
        self.iec_drives[device] = client
        if self.interface:
            status = "connected" if ok else "pending (server not yet reachable)"
            self.interface.add_debug_log(
                f"💾 Drive {device} → {addr} [{status}]"
            )
        self._sync_iec_kernal_tap()
        return ok

    def _spawn_local_drive(self, disk_path: Optional[str], device: int = 8,
                           tier: str = "fast",
                           dos_rom_path: Optional[str] = None) -> str:
        """Launch a headless c1541_emulator subprocess and connect to it.

        Returns the "host:port" string that was attached. Tracks the child in
        ``self._spawned_drives`` so it can be cleaned up on emulator shutdown.
        """
        import socket as _socket
        import subprocess
        import time
        import atexit

        if not hasattr(self, "_spawned_drives"):
            self._spawned_drives: list = []
            atexit.register(self._terminate_spawned_drives)

        # Pick an ephemeral free port.
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        import sys as _sys
        cmd = [
            _sys.executable,
            "-m", "c64py.drives.c1541_emulator",
            "--interface", "headless",
            "--emulation", tier,
            "--device", str(device),
            "--port", str(port),
        ]
        if disk_path:
            cmd += ["--disk", disk_path]
        if dos_rom_path:
            cmd += ["--dos-rom", dos_rom_path]
        proc = subprocess.Popen(cmd)
        self._spawned_drives.append(proc)

        # Wait for the port to accept connections.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with _socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            proc.terminate()
            raise RuntimeError(f"drive subprocess did not open port {port}")

        addr = f"localhost:{port}"
        self.attach_tcp_drive(device, addr)
        return addr

    def _terminate_spawned_drives(self) -> None:
        """Terminate all auto-spawned drive subprocesses."""
        for proc in getattr(self, "_spawned_drives", []):
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    def _kernal_hook_rts_return(self) -> None:
        """Pop return address from stack like RTS after a simulated KERNAL vector call."""
        sp = self.cpu.state.sp
        ret_addr_low = self.memory.read(0x0100 + ((sp + 1) & 0xFF))
        ret_addr_high = self.memory.read(0x0100 + ((sp + 2) & 0xFF))
        ret_addr = ret_addr_low | (ret_addr_high << 8)
        self.cpu.state.pc = (ret_addr + 1) & 0xFFFF
        self.cpu.state.sp = (sp + 2) & 0xFF

    def _handle_kernal_load(self) -> bool:
        """Handle KERNAL LOAD operation via TCP fast_load RPC.

        Intercepts PC=$FFD5 for disk devices (8-11), forwards to the connected
        TcpDriveClient, writes file bytes directly into C64 RAM, and returns via
        synthetic RTS.  Returns True if the call was handled (even on error).

        KERNAL LOAD calling convention:
        - A: 0 = LOAD, 1 = VERIFY
        - X: Load address low byte (if secondary address = 0)
        - Y: Load address high byte (if secondary address = 0)
        - $B7: Filename length
        - $BB-$BC: Filename pointer
        - $BA: Device number
        - $B9: Secondary address (0 = use address in X/Y, 1 = use address from file)
        """
        if not self.kernal_load_shortcut_enabled:
            return False
        if self.cpu.state.pc != 0xFFD5:
            return False
        device = self.memory.read(0xBA)
        if device < 8 or device > 11:
            return False

        verify = self.cpu.state.a != 0
        secondary_addr = self.memory.read(0xB9)
        filename_len = self.memory.read(0xB7)
        filename_ptr = self.memory.read(0xBB) | (self.memory.read(0xBC) << 8)
        filename_bytes = [self.memory.read((filename_ptr + i) & 0xFFFF)
                          for i in range(filename_len)]
        filename = ''.join(chr(b) if 32 <= b < 127 else '?' for b in filename_bytes)

        client = self.get_drive(device)
        if client is None:
            if self.interface:
                self.interface.add_debug_log(f"❌ No drive client for device {device}")
            self.cpu.state.a = 5           # BASIC 5 = DEVICE NOT PRESENT
            self.memory.write(0x90, 0x00)
            self.cpu.state.p |= 0x01
            self._kernal_hook_rts_return()
            return True

        if self.interface:
            self.interface.add_debug_log(
                f"🔧 KERNAL LOAD: device={device}, file='{filename}', verify={verify}"
            )

        file_data, err, dos_ft = client.fast_load(filename, secondary_addr)
        dos_ft = dos_ft if dos_ft is not None else 2

        if err is not None:
            drive_code = err[0] if err else 74
            drive_msg  = err[1] if err else "DRIVE NOT READY"
            if self.interface:
                self.interface.add_debug_log(
                    f"❌ LOAD '{filename}' failed: {drive_code},{drive_msg}"
                )
            # Map 1541 DOS error codes to BASIC/KERNAL error codes:
            #   BASIC 4 = FILE NOT FOUND
            #   BASIC 5 = DEVICE NOT PRESENT
            # (BASIC 8 = MISSING FILE NAME — do NOT use for disk errors)
            kernal_err = 4 if drive_code in (62, 63, 64) else 5
            self.cpu.state.a = kernal_err
            self.memory.write(0x90, 0x00)  # clear ST — error is in A/carry
            self.cpu.state.p |= 0x01       # carry set = error
            self._kernal_hook_rts_return()
            return True

        if secondary_addr == 1:
            # Force-load: caller supplies the load address via X/Y registers.
            load_addr = self.cpu.state.x | (self.cpu.state.y << 8)
            # PRG: first two bytes of file are load address and are skipped.
            # SEQ / USR / REL: entire payload goes to the caller's address.
            if dos_ft in (1, 3, 4):
                data = bytes(file_data)
            else:
                data = bytes(file_data[2:]) if len(file_data) >= 2 else bytes(file_data)
        elif dos_ft == 4 and len(file_data) >= 2:
            load_addr = file_data[0] | (file_data[1] << 8)
            data = bytes(file_data[2:])
        else:
            # Normal load (secondary 0 or 2): load address comes from first 2
            # bytes of the PRG, matching real KERNAL behaviour.
            if len(file_data) >= 2:
                load_addr = file_data[0] | (file_data[1] << 8)
                data = bytes(file_data[2:])
            else:
                load_addr = 0x0801
                data = bytes(file_data)

        if verify:
            mismatch = False
            for i, byte_val in enumerate(data):
                if self.memory.read((load_addr + i) & 0xFFFF) != byte_val:
                    mismatch = True
                    break
            if mismatch:
                if self.interface:
                    self.interface.add_debug_log(
                        f"❌ VERIFY '{filename}' mismatch at offset {i}"
                    )
                self.cpu.state.a = 28  # BASIC BAD DATA (VERIFY failed)
                self.memory.write(0x90, 0x01)
                self.cpu.state.p |= 0x01
                self._kernal_hook_rts_return()
                return True
        else:
            for i, byte_val in enumerate(data):
                self.memory.write((load_addr + i) & 0xFFFF, byte_val)

        end_addr = (load_addr + len(data)) & 0xFFFF
        self.cpu.state.x = end_addr & 0xFF
        self.cpu.state.y = (end_addr >> 8) & 0xFF
        self.cpu.state.p &= ~0x01
        self.memory.write(0x90, 0x00)

        if filename == "$" and load_addr == 0x0801:
            self.memory.write(0x002D, end_addr & 0xFF)
            self.memory.write(0x002E, (end_addr >> 8) & 0xFF)
            self.memory.write(0x002F, end_addr & 0xFF)
            self.memory.write(0x0030, (end_addr >> 8) & 0xFF)
            self.memory.write(0x0031, end_addr & 0xFF)
            self.memory.write(0x0032, (end_addr >> 8) & 0xFF)

        if self.interface:
            self.interface.add_debug_log(
                f"✅ {'Verified' if verify else 'Loaded'} {len(data)} bytes "
                f"at ${load_addr:04X}-${end_addr:04X}"
            )

        self._kernal_hook_rts_return()
        return True

    def _handle_kernal_save(self) -> bool:
        """Handle KERNAL SAVE operation for virtual disk drives.
        
        This intercepts SAVE calls when PC is at $FFD8 and device is 8-11.
        Skipped when :attr:`use_iec_bus` is True.
        Returns True if SAVE was handled, False otherwise.
        
        KERNAL SAVE calling convention:
        - A: Zero page pointer to start address (low byte)
        - X: Start address low byte  
        - Y: Start address high byte
        - $B7: Filename length
        - $BB-$BC: Filename pointer
        - $BA: Device number
        - $AE-$AF: End address + 1
        """
        if not self.kernal_load_shortcut_enabled:
            return False
        if self.cpu.state.pc != 0xFFD8:
            return False
        device = self.memory.read(0xBA)
        if device < 8 or device > 11:
            return False

        filename_len = self.memory.read(0xB7)
        filename_ptr = self.memory.read(0xBB) | (self.memory.read(0xBC) << 8)
        filename_bytes = [self.memory.read((filename_ptr + i) & 0xFFFF)
                          for i in range(filename_len)]
        filename = ''.join(chr(b) if 32 <= b < 127 else '?' for b in filename_bytes)

        client = self.get_drive(device)
        if client is None:
            if self.interface:
                self.interface.add_debug_log(f"❌ No drive client for device {device}")
            self.cpu.state.a = 5           # BASIC 5 = DEVICE NOT PRESENT
            self.memory.write(0x90, 0x00)
            self.cpu.state.p |= 0x01
            self._kernal_hook_rts_return()
            return True

        start_addr = self.cpu.state.x | (self.cpu.state.y << 8)
        end_addr = self.memory.read(0xAE) | (self.memory.read(0xAF) << 8)

        if self.interface:
            self.interface.add_debug_log(
                f"🔧 KERNAL SAVE: device={device}, file='{filename}', "
                f"${start_addr:04X}-${end_addr:04X}"
            )

        file_data = bytearray([start_addr & 0xFF, (start_addr >> 8) & 0xFF])
        for addr in range(start_addr, end_addr):
            file_data.append(self.memory.read(addr & 0xFFFF))

        ok, err = client.fast_save(filename, bytes(file_data))
        if ok:
            if self.interface:
                self.interface.add_debug_log(
                    f"✅ Saved {len(file_data)} bytes to '{filename}'"
                )
            self.cpu.state.p &= ~0x01
            self.memory.write(0x90, 0x00)
        else:
            drive_code = err[0] if err else 74
            drive_msg  = err[1] if err else "DRIVE NOT READY"
            if self.interface:
                self.interface.add_debug_log(
                    f"❌ SAVE '{filename}' failed: {drive_code},{drive_msg}"
                )
            # Map 1541 DOS error codes to BASIC/KERNAL error codes:
            #   BASIC 4 = FILE NOT FOUND   (62 FILE NOT FOUND)
            #   BASIC 5 = DEVICE NOT PRESENT (74 DRIVE NOT READY, 72 DISK FULL, etc.)
            kernal_err = 4 if drive_code in (62, 63) else 5
            self.cpu.state.a = kernal_err
            self.memory.write(0x90, 0x00)  # clear ST — error is in A/carry
            self.cpu.state.p |= 0x01       # carry set = error

        self._kernal_hook_rts_return()
        return True

    def _step_iec_drives(self, host_cycles: int) -> None:
        """Advance 1541 CPUs in lockstep with C64 CPU cycles (IEC accurate mode).

        The previous cap (128) starved the drive when the Rust batch reported thousands
        of cycles per quantum, which broke KERNAL serial I/O (e.g. stuck on SEARCHING).
        """
        if not self.use_iec_bus:
            return
        n = max(1, int(host_cycles))
        for d in self.iec_drives.values():
            d.step(n)

    def run_cpu_instruction_quantum(
        self, cycles_before: int, max_cycles: Optional[int] = None
    ) -> int:
        """Run one logical CPU step: KERNAL disk hooks, then batch or :meth:`CPU6502.step`."""
        if self._handle_kernal_load():
            return 0
        if self._handle_kernal_save():
            return 0
        return self.cpu.cpu_step_quantum(
            self.udp_debug, self.vice_trace, cycles_before, max_cycles
        )

    def _screen_update_worker(self) -> None:
        """Worker to update screen at ~60Hz (NTSC C64 rate)."""
        import time

        frame_time = 1.0 / 60.0  # Target 60Hz
        last_time = time.time()
        while self.running:
            now = time.time()
            if now - last_time >= frame_time:
                last_time = now
                # Call graphics update if available
                if hasattr(self, 'graphics') and self.graphics:
                    self.graphics.update()
                # Call generic screen update callback (used by Textual UI)
                if self.screen_update_callback:
                    try:
                        self.screen_update_callback()
                    except Exception:
                        pass  # Ignore errors during callback (UI might be shutting down)
            time.sleep(0.001)  # Yield to other threads

    def run(self, max_cycles: Optional[int] = None) -> None:
        """Run the emulator"""
        self.running = True
        self.reset_speed_throttle()
        # Resume absolute cycle count from the current state. Fresh runs have
        # current_cycles == 0, so this is a no-op; snapshot loads keep moving
        # forward from the restored cycle count and --max-cycles stays absolute.
        cycles = int(self.current_cycles)
        last_pc = None
        stuck_count = 0
        pc_history = []  # Track recent PCs for debugging

        # Start screen update thread
        self.screen_update_thread = threading.Thread(target=self._screen_update_worker, daemon=True)
        self.screen_update_thread.start()

        # Log start of execution
        if self.udp_debug and self.udp_debug.enabled:
            self.udp_debug.send('execution_start', {
                'max_cycles': max_cycles,
                'initial_pc': self.cpu.state.pc,
                'initial_pc_hex': f'${self.cpu.state.pc:04X}'
            })

        # Main CPU emulation loop (runs as fast as possible)
        last_time = time.time()
        last_cycle_check = 0
        inject_wall_t0 = time.perf_counter()

        while self.running:
            # Drain host key queue on the CPU thread only (avoids races with KERNAL CHRIN).
            self.sync_keyboard_host_queue()
            pc = self.cpu.state.pc

            # Load program if pending (after BASIC boot completes)
            if self.prg_file_path and not hasattr(self, '_program_loaded_after_boot'):
                # BASIC is ready - load the program now (after boot has completed)
                # Wait until we're past boot sequence
                if cycles > BASIC_BOOT_CYCLES:
                    try:
                        self.load_prg(self.prg_file_path)
                        self.prg_file_path = None  # Clear path after loading
                        self._program_loaded_after_boot = True
                        if self.interface:
                            self.interface.add_debug_log("💾 Program loaded after BASIC boot completed")
                        # Inject "RUN" command into keyboard buffer for autorun
                        self._inject_run_command()
                    except Exception as e:
                        if self.interface:
                            self.interface.add_debug_log(f"❌ Failed to load program: {e}")
                        self.prg_file_path = None  # Clear path even on error

            # Attach disk if pending (after BASIC boot completes)
            if self.disk_image_path and not hasattr(self, '_disk_attached_after_boot'):
                # BASIC is ready - attach disk now (after boot has completed)
                # Wait until we're past boot sequence
                if cycles > BASIC_BOOT_CYCLES:
                    try:
                        self.attach_disk(self.disk_image_path, device=8)
                        self.disk_image_path = None  # Clear path after attaching
                        self._disk_attached_after_boot = True
                        if self.interface:
                            self.interface.add_debug_log("💾 Disk attached after BASIC boot completed")
                        # Inject LOAD"$",8 command into keyboard buffer to list directory
                        self._inject_load_directory_command(device=8)
                    except Exception as e:
                        if self.interface:
                            self.interface.add_debug_log(f"❌ Failed to attach disk: {e}")
                        self.disk_image_path = None  # Clear path even on error

            # Auto-spawned drive: disk is already in the remote process;
            # just inject LOAD"$",8 after BASIC boot to list the directory.
            if (hasattr(self, '_auto_spawned_drive_device')
                    and not hasattr(self, '_auto_spawned_dir_injected')
                    and cycles > BASIC_BOOT_CYCLES):
                device = self._auto_spawned_drive_device
                self._inject_load_directory_command(device=device)
                self._auto_spawned_dir_injected = True
                if self.interface:
                    self.interface.add_debug_log(
                        f"💾 Auto-spawned drive {device}: injected LOAD\"$\",{device}"
                    )

            cmd_queue = self._monitor_cmd_queue
            if cmd_queue is not None:
                try:
                    while True:
                        item = cmd_queue.get_nowait()
                        if not item:
                            continue
                        if item[0] == "STEP":
                            self.cpu._monitor_force_single = True
                            self._monitor_pending_step_ack = True
                        elif item[0] == "GO":
                            self.cpu._monitor_force_single = False
                except queue.Empty:
                    pass

            step_cycles = self.run_cpu_instruction_quantum(cycles, max_cycles)
            reply_queue = self._monitor_reply_queue
            if reply_queue is not None and self._monitor_pending_step_ack:
                self._monitor_pending_step_ack = False
                st = self.cpu.state
                reply_queue.put(
                    f"PC=${st.pc:04X} A=${st.a:02X} X=${st.x:02X} Y=${st.y:02X} "
                    f"SP=${st.sp:02X} P=${st.p:02X} CYCLES={cycles} STEP_CYCLES={step_cycles}\r\n"
                )
            if step_cycles == 0:
                continue

            cycles += step_cycles
            self.current_cycles = cycles
            self._step_iec_drives(step_cycles)
            if self.cpu.state.pc in self.monitor_breakpoints:
                self.cpu._monitor_force_single = True
            self.memory.sync_joystick_inject(cycles)
            self._process_scheduled_inject_keys(
                cycles, time.perf_counter() - inject_wall_t0
            )
            self._service_snapshot_requests()
            self.throttle_emulation_if_needed(cycles)

            # Check if we've reached max cycles
            if max_cycles is not None and cycles >= max_cycles:
                if hasattr(self, 'autoquit') and self.autoquit:
                    self.running = False
                    stop_reason = "max_cycles_autoquit"
                else:
                    stop_reason = "max_cycles_reached"
                break

            # Textual interface updates automatically, no manual updates needed

            # Calculate cycles per second periodically
            if cycles - last_cycle_check >= 100000:
                current_time = time.time()
                elapsed = current_time - last_time
                if elapsed > 0:
                    self.cycles_per_second = (cycles - last_cycle_check) / elapsed
                last_time = current_time
                last_cycle_check = cycles

            # Detect if we're stuck (but ignore if CPU is stopped - that's expected)
            if self.cpu.state.stopped:
                # CPU is stopped (KIL instruction) - this is expected, just break
                debug_msg = f"PC stuck at ${self.cpu.state.pc:04X} (KIL instruction) - stopping"
                if self.debug:
                    debug_msg_full = f"🛑 CPU stopped at PC=${self.cpu.state.pc:04X} (KIL instruction)"
                    if self.rich_interface:
                        self.rich_interface.add_debug_log(debug_msg_full)
                # Always print to stdout for consistency with Textual mode
                print(debug_msg, flush=True)
                break
            elif self.cpu.state.pc == last_pc:
                # Check if we're in a graphics mode wait loop
                mode_info = self.memory.get_display_mode()
                in_graphics_mode = (
                    mode_info['bitmap_mode']
                    or mode_info.get('multicolor', False)
                    or self.memory.is_sprite_enabled(0)
                )
                
                # When the KERNAL ROM is running, input waits can loop inside the ROM.
                if self.memory.kernal_rom and ROM_KERNAL_START <= self.cpu.state.pc < ROM_KERNAL_END:
                    stuck_count = 0
                # CHRIN ($FFCF) blocks when keyboard buffer is empty - this is expected behavior
                elif self.cpu.state.pc != 0xFFCF:
                    # Check if it's a simple wait loop (JMP to self or nearby)
                    opcode = self.memory.read(self.cpu.state.pc)
                    if opcode == 0x4C:  # JMP absolute
                        target_low = self.memory.read(self.cpu.state.pc + 1)
                        target_high = self.memory.read(self.cpu.state.pc + 2)
                        target = target_low | (target_high << 8)
                        # Allow JMP * in graphics mode (infinite wait loop)
                        if in_graphics_mode and abs(target - self.cpu.state.pc) <= 10:
                            stuck_count = 0
                        else:
                            stuck_count += 1
                    else:
                        stuck_count += 1
                    
                    if stuck_count > 1000:
                        debug_msg1 = f"PC stuck at ${self.cpu.state.pc:04X} for {stuck_count} steps - stopping"
                        if self.debug:
                            debug_msg2 = "  This usually means an opcode is not implemented or not advancing PC correctly"
                        if self.rich_interface:
                            self.rich_interface.add_debug_log(debug_msg1)
                            if self.debug:
                                self.rich_interface.add_debug_log(debug_msg2)
                        # Always print stuck message to stdout
                        print(debug_msg1, flush=True)
                        # Don't try to advance - this masks the real problem
                        # Instead, stop execution to prevent infinite loops
                        self.running = False
                        break
                else:
                    # PC is at CHRIN - reset stuck count since blocking is expected
                    stuck_count = 0
            else:
                stuck_count = 0
            last_pc = self.cpu.state.pc
            pc_history.append(self.cpu.state.pc)
            if len(pc_history) > 20:  # Keep last 20 PCs
                pc_history.pop(0)

            # Periodic status logging (less frequent to avoid overhead)
            if self.debug and cycles % 100000 == 0:
                state = self.get_cpu_state()
                debug_msg = f"🔄 Cycles: {cycles}, PC=${state['pc']:04X}, A=${state['a']:02X}"
                if self.rich_interface:
                    self.rich_interface.add_debug_log(debug_msg)

            # Log periodic status if UDP debug is enabled (less frequent)
            if self.udp_debug and self.udp_debug.enabled and cycles % 100000 == 0:
                state = self.get_cpu_state()
                self.udp_debug.send('status', {
                    'cycles': cycles,
                    'pc': state['pc'],
                    'pc_hex': f'${state["pc"]:04X}',
                    'a': state['a'],
                    'x': state['x'],
                    'y': state['y'],
                    'sp': state['sp'],
                    'p': state['p']
                })

            # Debug: Log when entering key boot routines
            if self.debug and pc in [0xFDA3, 0xFD50, 0xFD15, 0xFF5B]:
                routine_name = {
                    0xFDA3: "IOINIT",
                    0xFD50: "RAMTAS",
                    0xFD15: "RESTOR",
                    0xFF5B: "CINT"
                }.get(pc, "UNKNOWN")
                if self.rich_interface:
                    self.rich_interface.add_debug_log(f"🔧 ENTERING {routine_name} at PC=${pc:04X}")
                else:
                    print(f"🔧 ENTERING {routine_name} at cycle {cycles}, PC=${pc:04X}")
                if pc == 0xFD15:  # RESTOR
                    # Check stack contents
                    sp = self.cpu.state.sp
                    if sp < 0xFF:
                        ret_low = self.memory.read(0x100 + ((sp + 1) & 0xFF))
                        ret_high = self.memory.read(0x100 + ((sp + 2) & 0xFF))
                        return_addr = ret_low | (ret_high << 8)
                        debug_msg = f"   Stack SP=${sp:02X}, return addr=${return_addr:04X}"
                        if self.rich_interface:
                            self.rich_interface.add_debug_log(debug_msg)
                        print(debug_msg)
                elif pc == 0xFF5B:  # CINT - log opcodes it executes
                    print(f"   CINT will execute opcodes...")

            # Debug: Show raster line during CINT
            if self.debug and pc >= 0xFF5B and pc <= 0xFFFF:
                if cycles % 10000 == 0:  # Log every 10k cycles during CINT
                    raster = self.memory.raster_line
                    print(f"📺 CINT: raster=${raster:03X}, cycle={cycles}")

            # Debug: Log when PC reaches dangerous areas
            if self.debug and pc == 0x0000:
                debug_msg = f"🚨 DANGER: PC reached $0000"
                if self.rich_interface:
                    self.rich_interface.add_debug_log(debug_msg)
                print(f"{debug_msg} at cycle {cycles}")
                # Show recent PC history
                history_msg = f"Recent PCs: {[f'${p:04X}' for p in pc_history[-10:]]}"
                if self.rich_interface:
                    self.rich_interface.add_debug_log(history_msg)
                print(f"   {history_msg}")

            # Debug: Log RTS from boot routines
            if self.debug and pc == 0x60 and last_pc in [0xFDA3, 0xFD50, 0xFD15, 0xFF5B]:  # RTS
                routine_name = {
                    0xFDA3: "IOINIT",
                    0xFD50: "RAMTAS",
                    0xFD15: "RESTOR",
                    0xFF5B: "CINT"
                }.get(last_pc, "UNKNOWN")
                if self.rich_interface:
                    self.rich_interface.add_debug_log(f"✅ COMPLETED {routine_name}")
                print(f"✅ COMPLETED {routine_name} at cycle {cycles}")

            # Debug: Log post-boot sequence (only if debug enabled)
            if self.debug:
                if pc == 0xFCFE:  # CLI
                    print(f"🔓 CLI (enable interrupts) at cycle {cycles}")
                    print(f"   Next PC should be FCFF, I flag was {self.cpu.state.p & 0x04}")
                elif pc == 0xFCFF:  # JMP ($A000)
                    a000_low = self.memory.read(0xA000)
                    a000_high = self.memory.read(0xA001)
                    jump_target = a000_low | (a000_high << 8)
                    print(f"🏃 JMP ($A000) -> ${jump_target:04X} at cycle {cycles}")
                elif pc == 0xE394:  # BASIC cold start entry point
                    print(f"📚 Entered BASIC cold start at ${pc:04X} (cycle {cycles})")

        # Determine stop reason
        stop_reason = "unknown"
        if self.cpu.state.stopped:
            stop_reason = "cpu_stopped"
        elif max_cycles is not None and cycles >= max_cycles:
            stop_reason = "max_cycles_reached"
        elif not self.running:
            stop_reason = "stuck_pc"

        # Log end of execution
        if self.udp_debug and self.udp_debug.enabled:
            self.udp_debug.send('execution_end', {
                'total_cycles': cycles,
                'final_pc': self.cpu.state.pc,
                'final_pc_hex': f'${self.cpu.state.pc:04X}',
                'stop_reason': stop_reason,
                'cpu_stopped': self.cpu.state.stopped,
                'max_cycles': max_cycles,
                'running': self.running
            })

        # Final screen update
        self._update_text_screen()

        self.service_exit_snapshot(stop_reason)

    def service_exit_snapshot(self, stop_reason: str = "unknown") -> None:
        """Write the ``--save-snapshot-at-exit`` file if one was requested.

        Safe to call multiple times; second call is a no-op once the
        ``_snapshot_at_exit`` slot has been consumed. Called by every run
        loop that can stop the emulator (CPU KIL, stuck-PC detection,
        max-cycles, autoquit), so the snapshot is produced regardless of
        which UI path (``C64.run()``, ``GraphicsUI``, ``RichUI``) is
        driving the CPU thread.
        """
        at_exit = getattr(self, "_snapshot_at_exit", None)
        if at_exit is None:
            return
        path, note = at_exit
        self._snapshot_at_exit = None
        try:
            self.save_snapshot(path, note=note or f"at_exit reason={stop_reason}")
        except Exception as exc:
            print(f"❌ Exit snapshot failed: {exc}", flush=True)

    def _petscii_to_screen_code(self, petscii_char: int) -> int:
        """Convert PETSCII character to C64 screen code"""
        if petscii_char < 32:
            # Control characters and symbols
            return petscii_char
        elif petscii_char < 64:
            # A-Z, symbols
            return petscii_char
        elif petscii_char < 96:
            # a-z (convert to screen codes 33-58)
            return petscii_char - 64
        elif petscii_char < 128:
            # More symbols and graphics
            return petscii_char - 32
        elif petscii_char < 160:
            # Reverse graphics
            return petscii_char - 128
        elif petscii_char < 192:
            # More symbols
            return petscii_char - 64
        else:
            # Uppercase graphics
            return petscii_char - 128

    # Precomputed screen code to ASCII lookup table (0-127)
    _SCREEN_CODE_TO_ASCII = None
    _SCREEN_CODE_TABLE_LOCK = threading.Lock()
    
    @classmethod
    def _init_screen_code_table(cls):
        """Initialize the screen code to ASCII lookup table (thread-safe)."""
        # Double-checked locking pattern for thread-safe lazy initialization
        if cls._SCREEN_CODE_TO_ASCII is not None:
            return
        
        with cls._SCREEN_CODE_TABLE_LOCK:
            # Check again inside the lock in case another thread initialized it
            if cls._SCREEN_CODE_TO_ASCII is not None:
                return
            
            table = [' '] * 128
            table[0] = '@'
            for i in range(1, 27):  # 0x01-0x1A -> A-Z
                table[i] = chr(ord('A') + i - 1)
            for i in range(0x1B, 0x20):  # [\]^_
                table[i] = chr(ord('[') + i - 0x1B)
            table[0x20] = ' '
            punct = '!"#$%&\'()*+,-./'
            for i, ch in enumerate(punct):
                table[0x21 + i] = ch
            for i in range(0x30, 0x3A):  # 0-9
                table[i] = chr(ord('0') + i - 0x30)
            for i in range(0x3A, 0x41):  # : ; < = > ? @
                table[i] = chr(i)
            for i in range(0x41, 0x5B):  # A-Z
                table[i] = chr(i)
            for i in range(0x5B, 0x60):  # [\]^_
                table[i] = chr(ord('[') + i - 0x5B)
            for i in range(0x60, 0x7F):
                table[i] = chr(i - 0x60) if i - 0x60 <= 0x1F else chr(i)
            table[0x7F] = chr(0x7F)
            cls._SCREEN_CODE_TO_ASCII = table

    def _update_text_screen(self) -> bool:
        """Update text screen from screen memory (thread-safe).
        
        Returns True if screen was updated, False if unchanged (dirty-check optimization).
        Uses NumPy for fast operations when available, falls back to pure Python.
        Supports bitmap mode rendering as ASCII art.
        """
        # Check if we're in bitmap mode
        mode_info = self.memory.get_render_display_mode()
        
        if mode_info['bitmap_mode']:
            # Render bitmap mode as ASCII art
            return self._update_bitmap_screen_ascii(mode_info)
        
        # Text mode rendering (original code)
        # Ensure lookup table is initialized
        self._init_screen_code_table()
        
        vic_bank = self.memory.get_render_vic_bank_base()
        screen_base = (vic_bank + mode_info['screen_base']) & 0xFFFF
        color_base = COLOR_MEM
        
        # Fast dirty-check using bytes comparison
        current_screen_bytes = bytes(self.memory.ram[(screen_base + i) & 0xFFFF] for i in range(1000))
        current_color_bytes = bytes(self.memory.ram[color_base:color_base + 1000])
        cursor_color = self.memory.ram[0x0286] & 0x0F
        
        # Fast comparison using bytes
        if (current_screen_bytes == self._prev_screen_data and 
            current_color_bytes == self._prev_color_data and
            not self._screen_dirty):
            return False  # Nothing changed, skip expensive update
        
        # Update cache
        self._prev_screen_data = current_screen_bytes
        self._prev_color_data = current_color_bytes
        self._screen_dirty = False

        lookup = self._SCREEN_CODE_TO_ASCII
        
        with self.screen_lock:
            if HAS_NUMPY:
                # NumPy vectorized path
                current_screen = np.frombuffer(current_screen_bytes, dtype=np.uint8)
                current_color = np.frombuffer(current_color_bytes, dtype=np.uint8)
                screen_2d = current_screen.reshape(25, 40)
                color_2d = current_color.reshape(25, 40)
                
                self.text_reversed[:] = (screen_2d & 0x80) != 0
                char_codes = screen_2d & 0x7F
                self.text_colors[:] = color_2d & 0x0F
                self.text_colors[self.text_reversed] = cursor_color
                
                for row in range(25):
                    for col in range(40):
                        self.text_screen[row, col] = lookup[char_codes[row, col]]
            else:
                # Pure Python fallback path
                for row in range(25):
                    for col in range(40):
                        idx = row * 40 + col
                        raw_code = current_screen_bytes[idx]
                        color_code = current_color_bytes[idx] & 0x0F
                        
                        reversed_char = bool(raw_code & 0x80)
                        char_code = raw_code & 0x7F
                        
                        self.text_reversed[row][col] = reversed_char
                        self.text_colors[row][col] = cursor_color if reversed_char else color_code
                        self.text_screen[row][col] = lookup[char_code]
        
        return True  # Screen was updated
    
    def _update_bitmap_screen_ascii(self, mode_info: dict) -> bool:
        """Render bitmap mode as ASCII art (simplified).
        
        Uses Unicode block characters to represent bitmap pixels.
        Each 8x8 bitmap cell is converted to a 2x2 character block.
        """
        # Unicode block characters for different pixel densities
        BLOCKS = [' ', '░', '▒', '▓', '█']
        
        vic_bank = self.memory.get_vic_bank_base()
        bitmap_base = (vic_bank + mode_info['bitmap_base']) & 0xFFFF
        screen_base = (vic_bank + mode_info['screen_base']) & 0xFFFF
        
        # For dirty checking, we'll sample the bitmap
        # (full check would be too expensive for 8000 bytes)
        sample_bytes = bytes(self.memory.ram[(bitmap_base + i) & 0xFFFF] for i in range(100))
        if hasattr(self, '_prev_bitmap_sample') and sample_bytes == self._prev_bitmap_sample:
            return False
        self._prev_bitmap_sample = sample_bytes
        
        with self.screen_lock:
            # Process 40x25 character blocks
            # Each block represents an 8x8 pixel area
            # We'll convert to 2x2 character representation (4x4 pixels per char)
            for char_row in range(25):
                for char_col in range(40):
                    char_index = char_row * 40 + char_col
                    
                    # Get bitmap data for this 8x8 block
                    bitmap_offset = (bitmap_base + char_index * 8) & 0xFFFF
                    
                    # Sample pixels: count set pixels in top-left 4x4 quadrant
                    # This gives us a rough density for ASCII representation
                    # pixel_count ranges from 0 (no pixels) to 16 (all pixels)
                    # We map this to 5 density levels: 0-3, 4-7, 8-11, 12-15, 16
                    pixel_count = 0
                    for y in range(4):
                        byte = self.memory.ram[(bitmap_offset + y) & 0xFFFF]
                        # Count bits in upper nibble (left 4 pixels)
                        pixel_count += bin(byte >> 4).count('1')
                    
                    # Map pixel count to block character
                    # 0 pixels = ' ', 16 pixels = '█'
                    density = min(4, pixel_count // 4)
                    char = BLOCKS[density]
                    
                    # Get colors from screen RAM
                    color_data = self.memory.ram[(screen_base + char_index) & 0xFFFF]
                    if mode_info['multicolor']:
                        # In multicolor mode, use lower nibble for foreground
                        fg_color = color_data & 0x0F
                    else:
                        # In hires mode, use upper nibble for foreground
                        fg_color = (color_data >> 4) & 0x0F
                    
                    # Update text screen
                    if HAS_NUMPY:
                        self.text_screen[char_row, char_col] = char
                        self.text_colors[char_row, char_col] = fg_color
                        self.text_reversed[char_row, char_col] = False
                    else:
                        self.text_screen[char_row][char_col] = char
                        self.text_colors[char_row][char_col] = fg_color
                        self.text_reversed[char_row][char_col] = False
        
        return True

    @classmethod
    def _c64_color_to_rich_rgb(cls, color_code: int) -> str:
        """Convert a C64 color code (0-15) to a Rich rgb(...) string."""
        r, g, b = cls._C64_PALETTE_RGB[color_code & 0x0F]
        return f"rgb({r},{g},{b})"

    def _render_text_screen_rich(self) -> Text:
        """Render text screen as a Rich Text renderable with C64 colors.
        
        Optimized to batch consecutive characters with same style.
        """
        # VIC-II background color (applies to the whole screen in standard text mode)
        background_color = self.memory.peek_vic(0x21) & 0x0F
        bg_style = self._c64_color_to_rich_rgb(background_color)
        border_color = self.memory.peek_vic(0x20) & 0x0F
        border_style = self._c64_color_to_rich_rgb(border_color)
        border_cell_style = f"{border_style} on {border_style}"

        with self.screen_lock:
            screen_text = Text()
            full_cols = SCREEN_COLS + BORDER_WIDTH * 2

            # Top border (single append per line)
            top_border = (" " * full_cols + "\n") * BORDER_HEIGHT
            if top_border:
                screen_text.append(top_border, style=border_cell_style)

            for row in range(SCREEN_ROWS):
                # Left border
                screen_text.append(" " * BORDER_WIDTH, style=border_cell_style)
                
                # Batch consecutive characters with same style
                batch_chars = []
                batch_style = None
                
                for col in range(SCREEN_COLS):
                    char = self.text_screen[row][col]
                    fg = self.text_colors[row][col] & 0x0F
                    reversed_char = self.text_reversed[row][col]
                    
                    # Compute style for this character
                    fg_rgb = self._c64_color_to_rich_rgb(fg)
                    if reversed_char:
                        cell_style = f"{bg_style} on {fg_rgb}"
                    else:
                        cell_style = f"{fg_rgb} on {bg_style}"
                    
                    # If style changed, flush batch
                    if cell_style != batch_style:
                        if batch_chars:
                            screen_text.append("".join(batch_chars), style=batch_style)
                        batch_chars = [char]
                        batch_style = cell_style
                    else:
                        batch_chars.append(char)
                
                # Flush remaining batch
                if batch_chars:
                    screen_text.append("".join(batch_chars), style=batch_style)
                
                # Right border
                screen_text.append(" " * BORDER_WIDTH, style=border_cell_style)
                if row < (SCREEN_ROWS - 1):
                    screen_text.append("\n")

            # Bottom border (single append)
            screen_text.append("\n")
            bottom_border = (" " * full_cols + "\n") * (BORDER_HEIGHT - 1) + " " * full_cols
            if bottom_border:
                screen_text.append(bottom_border, style=border_cell_style)
            return screen_text

    def render_text_screen(self, no_colors: bool = False) -> Union[str, Text]:
        """Render the current text screen.

        - If `no_colors` is True, returns plain text (for server/CLI).
        - Otherwise returns a Rich `Text` renderable with C64 BASIC/VIC colors.
        """
        if no_colors:
            with self.screen_lock:
                return "\n".join("".join(self.text_screen[row]) for row in range(25))
        return self._render_text_screen_rich()

    def get_cursor_position(self) -> Tuple[int, int, int]:
        """Return cursor row, column, and absolute address."""
        row = self.memory.read(CURSOR_ROW_ADDR)
        col = self.memory.read(CURSOR_COL_ADDR)
        row = max(0, min(row, 24))
        col = max(0, min(col, 39))
        cursor_addr = SCREEN_MEM + row * 40 + col
        return row, col, cursor_addr

    def read_screen_line_codes(self, row: int) -> List[int]:
        """Read raw screen codes for a given row."""
        row = max(0, min(row, 24))
        line_start = SCREEN_MEM + row * 40
        return [self.memory.read(line_start + col) for col in range(40)]

    def extract_line_codes(self, row: int) -> List[int]:
        """Extract a line with trailing spaces removed."""
        codes = self.read_screen_line_codes(row)
        last_non_space = -1
        for i in range(39, -1, -1):
            if codes[i] != 0x20:
                last_non_space = i
                break
        if last_non_space == -1:
            return []
        return codes[:last_non_space + 1]

    def get_current_line(self) -> Tuple[int, int, List[int]]:
        """Get cursor position and the current screen line codes."""
        row, col, _ = self.get_cursor_position()
        line_codes = self.extract_line_codes(row)
        return row, col, line_codes

    def _enqueue_keyboard_buffer(self, petscii_code: int) -> bool:
        """Queue a key for the CPU thread to place in the KERNAL buffer (thread-safe)."""
        code = petscii_code & 0xFF
        try:
            self._keyboard_incoming.put_nowait(code)
        except queue.Full:
            return False
        return True

    def sync_keyboard_host_queue(self) -> None:
        """CPU-thread only: move queued keys into the 10-byte KERNAL buffer when space exists."""
        while True:
            kb_buf_len = self.memory.read(KEYBOARD_BUFFER_LEN_ADDR)
            if kb_buf_len >= KEYBOARD_BUFFER_SIZE:
                break
            try:
                code = self._keyboard_incoming.get_nowait()
            except queue.Empty:
                break
            self.memory.write(KEYBOARD_BUFFER_BASE + kb_buf_len, code)
            self.memory.write(KEYBOARD_BUFFER_LEN_ADDR, kb_buf_len + 1)

    def send_petscii(self, petscii_code: int) -> bool:
        """Send a PETSCII key to the KERNAL keyboard queue."""
        return self._enqueue_keyboard_buffer(petscii_code & 0xFF)

    def send_petscii_sequence(self, codes: List[int]) -> None:
        """Send multiple PETSCII codes to the KERNAL keyboard queue."""
        for code in codes:
            self.send_petscii(code)

    def _render_with_rich(self) -> str:
        """Render screen using Rich library for better formatting"""

        # Read C64 colors from memory
        background_color = self.memory.peek_vic(0x21) & 0x0F  # Background color
        border_color = self.memory.peek_vic(0x20) & 0x0F      # Border color

        # C64 color to ANSI 256 color mapping (better color approximation)
        c64_to_ansi256 = {
            0: 0,     # Black
            1: 15,    # White
            2: 196,   # Red
            3: 51,    # Cyan
            4: 129,   # Purple
            5: 46,    # Green
            6: 21,    # Blue
            7: 226,   # Yellow
            8: 208,   # Orange
            9: 94,    # Brown
            10: 201,  # Pink
            11: 240,  # Dark grey
            12: 250,  # Grey
            13: 118,  # Light green
            14: 39,   # Light blue
            15: 252   # Light grey
        }

        # Get ANSI color codes
        bg_ansi = c64_to_ansi256.get(background_color, 0)
        border_ansi = c64_to_ansi256.get(border_color, 15)

        # C64 color to Rich color mapping (fallback)
        c64_colors = {
            0: "black",      # Black
            1: "white",      # White
            2: "red",        # Red
            3: "cyan",       # Cyan
            4: "purple",     # Purple
            5: "green",      # Green
            6: "blue",       # Blue
            7: "yellow",     # Yellow
            8: "bright_red", # Orange
            9: "bright_magenta",  # Brown
            10: "bright_magenta", # Pink
            11: "bright_cyan",    # Dark gray
            12: "bright_white",   # Medium gray
            13: "bright_green",   # Light green
            14: "bright_blue",    # Light blue
            15: "bright_white"    # Light gray
        }

        console = Console(legacy_windows=False)
        with self.screen_lock:
            # Create a text object for the entire screen
            screen_text = Text()

            for row in range(25):
                for col in range(40):
                    char = self.text_screen[row][col]
                    color = self.text_colors[row][col]

                    # Get Rich color name
                    rich_color = c64_colors.get(color, "white")

                    # Add character with color
                    screen_text.append(char, style=f"bold {rich_color}")

                # Add newline at end of row
                if row < 24:  # Don't add newline after last row
                    screen_text.append("\n")

            # Render to string
            with console.capture() as capture:
                console.print(screen_text)
            return capture.get()

    def _render_with_ansi(self, no_colors: bool = False) -> str:
        """Render text screen with ANSI colors (fallback)"""

        # Read C64 colors from memory
        background_color = self.memory.peek_vic(0x21) & 0x0F  # Background color
        border_color = self.memory.peek_vic(0x20) & 0x0F      # Border color

        # C64 color to ANSI 256 color mapping
        c64_to_ansi256 = {
            0: 0,     # Black
            1: 15,    # White
            2: 196,   # Red
            3: 51,    # Cyan
            4: 129,   # Purple
            5: 46,    # Green
            6: 21,    # Blue
            7: 226,   # Yellow
            8: 208,   # Orange
            9: 94,    # Brown
            10: 201,  # Pink
            11: 240,  # Dark grey
            12: 250,  # Grey
            13: 118,  # Light green
            14: 39,   # Light blue
            15: 252   # Light grey
        }

        # Get ANSI 256 color codes
        bg_ansi = c64_to_ansi256.get(background_color, 0)
        border_ansi = c64_to_ansi256.get(border_color, 15)

        # Fallback ANSI color mapping for foreground
        c64_colors = {
            0: 30,   # Black
            1: 37,   # White
            2: 31,   # Red
            3: 36,   # Cyan
            4: 35,   # Purple (magenta)
            5: 32,   # Green
            6: 34,   # Blue
            7: 33,   # Yellow
            8: 31,   # Orange (red)
            9: 35,   # Brown (magenta)
            10: 35,  # Pink (magenta)
            11: 90,  # Dark gray
            12: 37,  # Medium gray (white)
            13: 92,  # Light green
            14: 94,  # Light blue
            15: 97   # Light gray
        }

        with self.screen_lock:
            lines = []
            # Add border/background color to entire screen
            bg_escape = f'\033[48;5;{bg_ansi}m' if not no_colors else ''
            reset = '\033[0m' if not no_colors else ''

            for row in range(25):
                line = []
                if not no_colors:
                    line.append(bg_escape)  # Background color for entire line

                for col in range(40):
                    char = self.text_screen[row][col]

                    if no_colors:
                        line.append(char)
                    else:
                        color = self.text_colors[row][col]
                        # Apply ANSI 256 foreground color
                        fg_ansi = c64_to_ansi256.get(color, 15)
                        colored_char = f'\033[38;5;{fg_ansi}m{char}'
                        line.append(colored_char)

                if not no_colors:
                    line.append(reset)  # Reset colors at end of line

                lines.append(''.join(line))
            return '\n'.join(lines)

    def dump_memory(self, start: int = 0x0000, end: int = 0x10000) -> bytes:
        """Dump memory range as bytes"""
        return bytes(self.memory.ram[start:end])

    def get_cpu_state(self) -> Dict:
        """Get current CPU state"""
        return {
            'pc': self.cpu.state.pc,
            'a': self.cpu.state.a,
            'x': self.cpu.state.x,
            'y': self.cpu.state.y,
            'sp': self.cpu.state.sp,
            'p': self.cpu.state.p,
            'cycles': self.cpu.state.cycles
        }

    def set_cpu_state(self, state: Dict) -> None:
        """Set CPU state"""
        if 'pc' in state:
            self.cpu.state.pc = state['pc'] & 0xFFFF
        if 'a' in state:
            self.cpu.state.a = state['a'] & 0xFF
        if 'x' in state:
            self.cpu.state.x = state['x'] & 0xFF
        if 'y' in state:
            self.cpu.state.y = state['y'] & 0xFF
        if 'sp' in state:
            self.cpu.state.sp = state['sp'] & 0xFF
        if 'p' in state:
            self.cpu.state.p = state['p'] & 0xFF
