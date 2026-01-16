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
import os
import sys
import time

# Handle both direct execution and module import
try:
    from .debug import UdpDebugLogger
    from .emulator import C64
    from .server import EmulatorServer
except ImportError:
    # When run directly, add parent directory to path
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from c64py.debug import UdpDebugLogger
    from c64py.emulator import C64
    from c64py.server import EmulatorServer


def main():
    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))

    ap = argparse.ArgumentParser(description="C64 Emulator (text mode)")
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

    args = ap.parse_args()

    emu = C64()
    emu.debug = args.debug
    emu.autoquit = args.autoquit
    emu.screen_update_interval = args.screen_update_interval
    emu.no_colors = args.no_colors
    # Set fullscreen mode early so interface knows about it
    emu.interface.fullscreen = args.fullscreen
    if args.debug and not args.fullscreen:
        emu.interface.add_debug_log("🐛 Debug mode enabled")

    # Setup UDP debug logging if requested
    if args.udp_debug:
        emu.udp_debug = UdpDebugLogger(port=args.udp_debug_port, host=args.udp_debug_host)
        emu.udp_debug.enable()
        emu.interface.add_debug_log(f"📡 UDP debug logging enabled: {args.udp_debug_host}:{args.udp_debug_port}")
        # Test UDP connection
        try:
            test_msg = {'type': 'test', 'message': 'UDP debug initialized'}
            emu.udp_debug.send('test', test_msg)
            emu.interface.add_debug_log("✅ UDP test message sent successfully")
        except Exception as e:
            emu.interface.add_debug_log(f"❌ UDP test failed: {e}")

    # Pass UDP debug logger to memory
    if emu.udp_debug:
        emu.memory.udp_debug = emu.udp_debug

    # Set video standard
    emu.memory.video_standard = args.video_standard
    if not args.fullscreen:
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

        rom_dir_path = ensure_roms_available(explicit_rom_dir, allow_prompt=True)
        emu.load_roms(str(rom_dir_path))
        if not args.fullscreen:
            emu.interface.add_debug_log(f"💾 ROM directory: {rom_dir_path}")
    except Exception as e:
        # Ensure UI is not left running, then show a clear error.
        try:
            if hasattr(emu, "interface") and hasattr(emu.interface, "exit"):
                emu.interface.exit()
        except Exception:
            pass
        print(f"ERROR: {e}")
        sys.exit(1)

    # Store PRG file path for loading after boot (BASIC boot clears $0801-$0802)
    if args.prg_file:
        emu.prg_file_path = args.prg_file
        if not args.fullscreen:
            emu.interface.add_debug_log(f"📂 PRG file will be loaded after BASIC boot: {args.prg_file}")

    # Initialize CPU (use _read_word to ensure correct byte order and ROM mapping)
    reset_vector = emu.cpu._read_word(0xFFFC)
    emu.cpu.state.pc = reset_vector
    if not args.fullscreen:
        emu.interface.add_debug_log(f"🔄 Reset vector: ${reset_vector:04X}")

    if args.debug and not args.fullscreen:
        emu.interface.add_debug_log(f"🖥️ Initial CPU state: PC=${emu.cpu.state.pc:04X}, A=${emu.cpu.state.a:02X}, X=${emu.cpu.state.x:02X}, Y=${emu.cpu.state.y:02X}")
        emu.interface.add_debug_log(f"💾 Memory config ($01): ${emu.memory.ram[0x01]:02X}")
        emu.interface.add_debug_log(f"📺 Screen memory sample ($0400-$040F): {[hex(emu.memory.ram[0x0400 + i]) for i in range(16)]}")

    # Start server if requested (runs in parallel with UI)
    server = None
    if args.tcp_port or args.udp_port:
        server = EmulatorServer(emu, tcp_port=args.tcp_port, udp_port=args.udp_port)
        server.start()
        if not args.fullscreen:
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

    # Start Textual interface (unless explicitly disabled with --no-colors)
    if not args.no_colors:
        emu.interface.max_cycles = args.max_cycles
        # fullscreen flag already set earlier
        if not args.fullscreen:
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

    # Close UDP debug logger (flush all pending messages)
    if emu.udp_debug:
        emu.udp_debug.close()


if __name__ == "__main__":
    main()
