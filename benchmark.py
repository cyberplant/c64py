#!/usr/bin/env python3
"""Benchmark script for C64py emulator performance comparison."""

import os
import sys
import time

# Handle numpy availability for PyPy
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    print("NumPy not available - emulator may use fallback")

from c64py.emulator import C64


class DummyInterface:
    """Minimal interface for headless benchmarking."""
    def add_debug_log(self, msg): pass
    def exit(self): pass


def run_benchmark(max_cycles: int = 5_000_000, rom_dir: str = None):
    """Run CPU benchmark and report speed."""
    print(f"Python: {sys.version}")
    print(f"Implementation: {sys.implementation.name}")
    print(f"NumPy: {'available' if HAS_NUMPY else 'not available'}")
    print()

    emu = C64(interface_factory=lambda e: DummyInterface())
    emu.autoquit = True
    emu.screen_update_callback = None  # Disable screen updates for pure CPU benchmark

    # Try to load ROMs if available
    rom_dir = rom_dir or os.environ.get('ROM_DIR', '/roms')
    roms_loaded = False
    if os.path.isdir(rom_dir):
        try:
            emu.load_roms(rom_dir, require_char_rom=False)
            roms_loaded = True
            print(f"ROMs loaded from: {rom_dir}")
        except Exception as e:
            print(f"Could not load ROMs: {e}")
    
    if not roms_loaded:
        print("Running without ROMs (limited benchmark)")
    
    print(f"\nRunning CPU benchmark ({max_cycles:,} cycles)...")
    print("-" * 40)

    start = time.perf_counter()
    try:
        emu.run(max_cycles=max_cycles)
    except KeyboardInterrupt:
        print("\nBenchmark interrupted")
    except Exception as e:
        print(f"Error during run: {e}")
    
    elapsed = time.perf_counter() - start
    cycles = emu.current_cycles

    print("-" * 40)
    if elapsed > 0 and cycles > 0:
        mhz = cycles / elapsed / 1e6
        print(f"Cycles executed: {cycles:,}")
        print(f"Time elapsed:    {elapsed:.2f}s")
        print(f"Speed:           {mhz:.2f} MHz")
        print(f"C64 equivalent:  {mhz/1.0:.0%} of 1 MHz")
    else:
        print("No cycles executed - check ROM availability")

    return cycles, elapsed


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="C64py CPU Benchmark")
    parser.add_argument("--cycles", type=int, default=5_000_000,
                        help="Number of cycles to run (default: 5M)")
    parser.add_argument("--rom-dir", type=str, default=None,
                        help="Directory containing ROM files")
    args = parser.parse_args()
    
    run_benchmark(max_cycles=args.cycles, rom_dir=args.rom_dir)
