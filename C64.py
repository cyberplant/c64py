#!/usr/bin/env python3
"""
C64 Emulator - Text mode Python implementation

A Commodore 64 emulator focused on text mode operation.
Can load and run PRG files, dump memory, and communicate via TCP/UDP.

Usage:
    python C64.py [program.prg]
    python C64.py --tcp-port 1234
    python C64.py program.prg --udp-port 1235
"""

from __future__ import annotations

import argparse
import functools
import os
import sys
import time

# Handle both direct execution and module import
try:
    from .debug import UdpDebugLogger
    from .emulator import C64
    from .server import EmulatorServer
    from .constants import (
        BLNCT,
        BLNSW,
        CURSOR_COL_ADDR,
        CURSOR_ROW_ADDR,
        INPUT_BUFFER_INDEX_ADDR,
        INPUT_BUFFER_LEN_ADDR,
        KEYBOARD_BUFFER_BASE,
        KEYBOARD_BUFFER_LEN_ADDR,
        SCREEN_MEM,
    )
except ImportError:
    # When run directly, add parent directory to path
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from c64py.debug import UdpDebugLogger
    from c64py.emulator import C64
    from c64py.server import EmulatorServer
    from c64py.constants import (
        BLNCT,
        BLNSW,
        CURSOR_COL_ADDR,
        CURSOR_ROW_ADDR,
        INPUT_BUFFER_INDEX_ADDR,
        INPUT_BUFFER_LEN_ADDR,
        KEYBOARD_BUFFER_BASE,
        KEYBOARD_BUFFER_LEN_ADDR,
        SCREEN_MEM,
    )


def _show_speed(start_time: float, cycles: int) -> None:
    """Display emulation speed statistics."""
    import time
    elapsed = time.perf_counter() - start_time
    if elapsed > 0 and cycles > 0:
        mhz = cycles / elapsed / 1e6
        print(f"\n=== Emulation Speed ===")
        print(f"Cycles: {cycles:,}")
        print(f"Time:   {elapsed:.2f}s")
        print(f"Speed:  {mhz:.2f} MHz ({mhz/1.0:.0%} of C64)")


def main():
    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))

    ap = argparse.ArgumentParser(description="C64 Emulator")
    ap.add_argument("prg_file", nargs="?", help="PRG file to load and run")
    ap.add_argument(
        "--rom-dir",
        default=None,
        help="Directory containing ROM files (default: auto-detect common locations)",
    )
    ap.add_argument("--tcp-port", type=int, help="TCP port for control interface")
    ap.add_argument("--udp-port", type=int, help="UDP port for control interface")
    ap.add_argument("--max-cycles", type=int, default=None, help="Maximum cycles to run (default: unlimited)")
    ap.add_argument("--dump-memory", help="Dump memory to file after execution")
    ap.add_argument("--debug", action="store_true", help="Enable debug output")
    ap.add_argument("--udp-debug", action="store_true", help="Send debug events via UDP")
    ap.add_argument("--autoquit", action="store_true", help="Automatically quit when max cycles is reached")
    ap.add_argument("--udp-debug-port", type=int, default=64738, help="UDP port for debug events (default: 64738)")
    ap.add_argument("--udp-debug-host", type=str, default="127.0.0.1", help="UDP host for debug events (default: 127.0.0.1)")
    ap.add_argument("--screen-update-interval", type=float, default=0.1, help="Screen update interval in seconds (default: 0.1)")
    ap.add_argument("--video-standard", choices=["pal", "ntsc"], default="pal", help="Video standard (pal or ntsc, default: pal)")
    ap.add_argument("--no-colors", action="store_true", help="Disable ANSI color output")
    ap.add_argument("--fullscreen", action="store_true", help="Show only C64 screen output (no debug panel or status bar)")
    ap.add_argument("--graphics", action="store_true", help="Render output in a pygame graphics window")
    ap.add_argument("--graphics-scale", type=int, default=2, help="Graphics window scale factor (default: 2)")
    ap.add_argument("--graphics-fps", type=int, default=30, help="Graphics target FPS (default: 30)")
    ap.add_argument("--graphics-border", type=int, default=None, help="Graphics border size in pixels (default: 32)")
    ap.add_argument("--turbo", action="store_true", help="Run at maximum speed (no speed limiting)")
    ap.add_argument("--benchmark", action="store_true", help="Run benchmark (implies --turbo --autoquit --no-colors)")

    args = ap.parse_args()
    
    # --benchmark implies other flags and loads benchmark PRG
    if args.benchmark:
        args.turbo = True
        args.autoquit = True
        args.no_colors = True
        if args.max_cycles is None:
            args.max_cycles = 15_000_000  # Enough cycles for benchmark to complete
        # Auto-load benchmark PRG if no file specified
        if args.prg_file is None:
            benchmark_prg = os.path.join(script_dir, "programs", "benchmark.prg")
            if os.path.exists(benchmark_prg):
                args.prg_file = benchmark_prg
            else:
                print(f"Warning: Benchmark PRG not found at {benchmark_prg}")
                print("Run: compile.sh to build it")
    
    # Track start time for speed calculation
    start_time = time.perf_counter()

    interface_factory = None
    if args.graphics:
        try:
            from .graphics import PygameInterface
        except ImportError:
            from c64py.graphics import PygameInterface
        interface_factory = functools.partial(
            PygameInterface,
            max_cycles=args.max_cycles,
            scale=args.graphics_scale,
            fps=args.graphics_fps,
            border_size=args.graphics_border,
        )

    emu = C64(interface_factory=interface_factory)
    emu.debug = args.debug
    emu.autoquit = args.autoquit
    emu.turbo = args.turbo
    emu.screen_update_interval = args.screen_update_interval
    emu.no_colors = args.no_colors
    if args.debug:
        emu.cpu.enable_trace(1024)
    supports_ui_logs = hasattr(emu.interface, "fullscreen")
    if supports_ui_logs:
        emu.interface.fullscreen = args.fullscreen
    show_ui_logs = (not args.fullscreen) if supports_ui_logs else True
    if args.debug and show_ui_logs:
        emu.interface.add_debug_log("🐛 Debug mode enabled")

    # Setup UDP debug logging if requested
    if args.udp_debug:
        emu.udp_debug = UdpDebugLogger(port=args.udp_debug_port, host=args.udp_debug_host)
        emu.udp_debug.enable()
        if show_ui_logs:
            emu.interface.add_debug_log(f"📡 UDP debug logging enabled: {args.udp_debug_host}:{args.udp_debug_port}")
        # Test UDP connection
        try:
            test_msg = {'type': 'test', 'message': 'UDP debug initialized'}
            emu.udp_debug.send('test', test_msg)
            if show_ui_logs:
                emu.interface.add_debug_log("✅ UDP test message sent successfully")
        except Exception as e:
            if show_ui_logs:
                emu.interface.add_debug_log(f"❌ UDP test failed: {e}")

    # Pass UDP debug logger to memory
    if emu.udp_debug:
        emu.memory.udp_debug = emu.udp_debug

    # Set video standard
    emu.memory.video_standard = args.video_standard
    if show_ui_logs:
        emu.interface.add_debug_log(f"📺 Video standard: {args.video_standard.upper()}")

    # Load ROMs (auto-detect common locations if not provided).
    # Import ROM helper with support for both package and script execution.
    try:
        from .roms import ensure_roms_available
    except ImportError:
        from c64py.roms import ensure_roms_available

    try:
        explicit_rom_dir = args.rom_dir
        if explicit_rom_dir and not os.path.isabs(explicit_rom_dir):
            # Backward-compatible: interpret relative paths relative to the repo root
            # (this script lives in c64py/, so parent is project root).
            parent_dir = os.path.dirname(script_dir)
            explicit_rom_dir = os.path.normpath(os.path.join(parent_dir, explicit_rom_dir))

        rom_dir_path = ensure_roms_available(
            explicit_rom_dir,
            allow_prompt=True,
            require_char_rom=args.graphics,
        )
        emu.load_roms(str(rom_dir_path), require_char_rom=args.graphics)
        if show_ui_logs:
            emu.interface.add_debug_log(f"💾 ROM directory: {rom_dir_path}")
    except Exception as e:
        # Ensure UI is not left running, then show a clear error.
        try:
            if hasattr(emu, "interface") and hasattr(emu.interface, "exit"):
                emu.interface.exit()
        except Exception:
            # Ignore errors during cleanup so we don't mask the original ROM loading failure.
            pass
        print(f"ERROR: {e}")
        sys.exit(1)

    # Store PRG file path for loading after boot (BASIC boot clears $0801-$0802)
    if args.prg_file:
        emu.prg_file_path = args.prg_file
        if show_ui_logs:
            emu.interface.add_debug_log(f"📂 PRG file will be loaded after BASIC boot: {args.prg_file}")

    # Initialize CPU (use _read_word to ensure correct byte order and ROM mapping)
    reset_vector = emu.cpu._read_word(0xFFFC)
    emu.cpu.state.pc = reset_vector
    if show_ui_logs:
        emu.interface.add_debug_log(f"🔄 Reset vector: ${reset_vector:04X}")

    if args.debug and show_ui_logs:
        emu.interface.add_debug_log(f"🖥️ Initial CPU state: PC=${emu.cpu.state.pc:04X}, A=${emu.cpu.state.a:02X}, X=${emu.cpu.state.x:02X}, Y=${emu.cpu.state.y:02X}")
        emu.interface.add_debug_log(f"💾 Memory config ($01): ${emu.memory.ram[0x01]:02X}")
        emu.interface.add_debug_log(f"📺 Screen memory sample ($0400-$040F): {[hex(emu.memory.ram[0x0400 + i]) for i in range(16)]}")

    # Start server if requested (runs in parallel with UI)
    server = None
    if args.tcp_port or args.udp_port:
        server = EmulatorServer(emu, tcp_port=args.tcp_port, udp_port=args.udp_port)
        server.start()
        if show_ui_logs:
            emu.interface.add_debug_log("📡 TCP/UDP server started")
            emu.interface.add_debug_log("📡 Server commands: STATUS, STEP, RUN, MEMORY, DUMP, SCREEN, LOAD")
        print("Server started on port(s): ", end="")
        if args.tcp_port:
            print(f"TCP:{args.tcp_port}", end="")
        if args.tcp_port and args.udp_port:
            print(", ", end="")
        if args.udp_port:
            print(f"UDP:{args.udp_port}", end="")
        print()

    # Start graphics interface if requested
    if args.graphics:
        emu.interface.max_cycles = args.max_cycles
        if show_ui_logs:
            emu.interface.add_debug_log("🎨 Graphics interface active")
        try:
            emu.interface.run()
        finally:
            if hasattr(emu.interface, "_get_last_log_lines"):
                last_lines = emu.interface._get_last_log_lines(20)
                if last_lines:
                    print("\n=== Last log messages ===")
                    for line in last_lines:
                        print(line)
        if server:
            server.running = False
        # Show emulation speed
        _show_speed(start_time, emu.current_cycles)
        return

    # Start Textual interface (unless explicitly disabled with --no-colors)
    if not args.no_colors:
        emu.interface.max_cycles = args.max_cycles
        # fullscreen flag already set earlier
        if show_ui_logs:
            emu.interface.add_debug_log("🚀 C64 Emulator started")
            emu.interface.add_debug_log("🎨 Textual interface with TCSS active")
        try:
            emu.interface.run()  # This will block and run the Textual app
        finally:
            # Capture and print last log lines after UI shuts down
            if hasattr(emu.interface, '_get_last_log_lines'):
                last_lines = emu.interface._get_last_log_lines(20)
                if last_lines:
                    print("\n=== Last log messages ===")
                    for line in last_lines:
                        print(line)
        # After UI closes, stop server if running
        if server:
            server.running = False
        # Show emulation speed
        _show_speed(start_time, emu.current_cycles)
        return  # Exit after Textual interface closes

    # This code should never be reached since Textual blocks
    # But if --no-colors is used, we fall through here
    try:
        print("Running emulator...")
        emu.run(args.max_cycles)
    except KeyboardInterrupt:
        print("\nStopping emulator...")
        emu.running = False
        if server:
            server.running = False

    if args.debug:
        chrout_count = getattr(emu.cpu, "chrout_count", None)
        if chrout_count is not None:
            print(f"DEBUG: CHROUT calls: {chrout_count}")
        cursor_row = emu.memory.read(CURSOR_ROW_ADDR)
        cursor_col = emu.memory.read(CURSOR_COL_ADDR)
        blnsw = emu.memory.read(BLNSW)
        blnct = emu.memory.read(BLNCT)
        print(f"DEBUG: Cursor row={cursor_row} col={cursor_col} BLNSW=${blnsw:02X} BLNCT=${blnct:02X}")
        first_line = [emu.memory.read(SCREEN_MEM + i) for i in range(40)]
        hex_line = " ".join(f"{code:02X}" for code in first_line)
        ascii_line = "".join(chr(code) if 0x20 <= code <= 0x7E else "." for code in first_line)
        print(f"DEBUG: Screen row0 hex: {hex_line}")
        print(f"DEBUG: Screen row0 ascii: {ascii_line}")
        vic_d018 = emu.memory.peek_vic(0x18) & 0xFF
        screen_base = ((vic_d018 >> 4) & 0x0F) * 0x0400
        non_space = 0
        for i in range(1000):
            if emu.memory.read(screen_base + i) != 0x20:
                non_space += 1
        print(f"DEBUG: VIC $D018=${vic_d018:02X} screen_base=${screen_base:04X} non_space={non_space}")
        if screen_base != SCREEN_MEM:
            base_line = [emu.memory.read(screen_base + i) for i in range(40)]
            base_hex = " ".join(f"{code:02X}" for code in base_line)
            base_ascii = "".join(chr(code) if 0x20 <= code <= 0x7E else "." for code in base_line)
            print(f"DEBUG: Screen base row0 hex: {base_hex}")
            print(f"DEBUG: Screen base row0 ascii: {base_ascii}")
        kb_len = emu.memory.read(KEYBOARD_BUFFER_LEN_ADDR)
        kb_codes = [emu.memory.read(KEYBOARD_BUFFER_BASE + i) for i in range(kb_len)]
        kb_hex = " ".join(f"{code:02X}" for code in kb_codes)
        kb_ascii = "".join(chr(code) if 0x20 <= code <= 0x7E else "." for code in kb_codes)
        print(f"DEBUG: Keyboard buffer len={kb_len} hex: {kb_hex}")
        print(f"DEBUG: Keyboard buffer ascii: {kb_ascii}")
        input_idx = emu.memory.read(INPUT_BUFFER_INDEX_ADDR)
        input_len = emu.memory.read(INPUT_BUFFER_LEN_ADDR)
        print(f"DEBUG: Input buffer idx={input_idx} len={input_len}")
        trace_entries = emu.cpu.get_trace()
        if trace_entries:
            print(f"DEBUG: Last {len(trace_entries)} instructions:")
            for entry in trace_entries:
                print(
                    "DEBUG: "
                    f"CYC={entry['cycles']} PC=${entry['pc']:04X} OP=${entry['opcode']:02X} "
                    f"OP1=${entry['op1']:02X} OP2=${entry['op2']:02X} "
                    f"A=${entry['a']:02X} X=${entry['x']:02X} Y=${entry['y']:02X} "
                    f"SP=${entry['sp']:02X} P=${entry['p']:02X}"
                )

    # Dump memory if requested
    if args.dump_memory:
        memory_dump = emu.dump_memory()
        with open(args.dump_memory, 'wb') as f:
            f.write(bytes([0x00, 0x00]))  # PRG header
            f.write(memory_dump)
        print(f"Memory dumped to {args.dump_memory}")

    # Show final screen (only if Rich was not used)
    if not server or not server.running:
        if args.no_colors:
            # Only show final screen if colors are disabled
            emu._update_text_screen()
            print("\nFinal Screen output:")
            print(emu.render_text_screen(no_colors=True))

    # Textual interface handles its own cleanup

    # Stop screen update thread
    emu.running = False
    if emu.screen_update_thread and emu.screen_update_thread.is_alive():
        emu.screen_update_thread.join(timeout=1.0)

    # Show emulation speed
    _show_speed(start_time, emu.current_cycles)

    # Close UDP debug logger (flush all pending messages)
    if emu.udp_debug:
        emu.udp_debug.close()


if __name__ == "__main__":
    main()
