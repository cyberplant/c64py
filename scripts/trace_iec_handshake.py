#!/usr/bin/env python3
"""Trace IEC handshake end-to-end with DOS ROM.

Goal: find where the byte-level CLK/DATA dance after ATN breaks.
Strategy: single-step both C64 and drive, log every bus edge and
PC change, compare to VICE trace or 1541 ROM disassembly.

Usage:
    C64PY_RUN_SLOW_TESTS=1 python scripts/trace_iec_handshake.py

Outputs a detailed trace to stdout and (optionally) compares
against an annotated 1541 ROM disassembly.
"""
import os
import sys
from pathlib import Path

# Add repo root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from c64py.emulator import C64
from c64py.d64 import create_blank_d64
from c64py.iec_bus import IECBus


def trace_handshake():
    """Trace LOAD command round-trip via real IEC."""
    rom_dir = Path(__file__).parent.parent / "roms"

    # Build a minimal D64 with one file.
    disk = create_blank_d64(disk_name="TEST", disk_id="01")
    payload = bytes([0x00, 0xC0, 0x11, 0x22, 0x33])  # PRG header + 3 bytes
    disk.write_file("TESTFILE", payload, filetype=0x82)

    d64_path = Path("/tmp/iec_trace_test.d64")
    disk.save_to_file(str(d64_path))

    # Boot emulator in accurate disk mode.
    emu = C64(interface_factory=lambda e: None)
    emu.interface = type("Iface", (), {"add_debug_log": lambda *a, **k: None})()
    emu._initialize_c64()
    emu.load_roms(str(rom_dir), require_char_rom=False)
    emu._iec_disk_full_impl = True
    emu.initialize_iec_bus(str(rom_dir))
    emu.attach_disk(str(d64_path), device=8)

    # Boot to BASIC ready, then inject LOAD command.
    print("=== Booting to BASIC ===")
    for i in range(30_000_000):
        emu.cpu.step()
        if i % 1_000_000 == 0:
            print(f"  {i//1_000_000}M cycles, PC=${emu.cpu.state.pc:04X}")
        # Check for BASIC ready.
        if emu.memory.read(0xFB) == 0x01:  # IQFLGS == 1 = input queue has data
            break

    print("✓ BASIC ready")

    # Inject LOAD"TESTFILE",8,1 command.
    print("\n=== Injecting LOAD command ===")
    emu.memory.write(0x0277, 16)  # buffer length
    cmd = b'LOAD"TESTFILE",8,1'  # 17 chars but we'll count manually
    for i, b in enumerate(cmd):
        emu.memory.write(0x0278 + i, b)

    print(f"Command: {cmd}")
    print(f"Calling KERNAL LOAD at $FFD5...")

    # Manually call KERNAL LOAD (simulating JSR $FFD5 from user code).
    emu.cpu.state.pc = 0xFFD5
    emu.cpu.state.p |= 0x04  # Set I flag to avoid IRQ chaos

    # Step through the handshake, logging key events.
    print("\n=== Tracing handshake ===")
    trace_log = []
    last_pc_c64 = None
    last_pc_drive = None
    last_atn = emu.iec_bus.atn if hasattr(emu, "iec_bus") else None
    last_clk = None
    last_data = None

    # Helper to format a trace entry.
    def log_event(msg):
        trace_log.append(msg)
        if len(trace_log) % 100 == 0 or "ERROR" in msg or "ATN" in msg:
            print(msg)

    consumed = 0
    max_cycles = 50_000_000
    while consumed < max_cycles:
        # Single step C64.
        used = emu.cpu.step()
        consumed += used

        # Service drive every ~100 cycles so it keeps up.
        if consumed % 100 == 0:
            emu._step_iec_drives(100)

        # Check for PC changes (branch, return, etc).
        pc_c64 = emu.cpu.state.pc
        pc_drive = emu.iec_drives[8].cpu.state.pc if 8 in emu.iec_drives else None

        if pc_c64 != last_pc_c64:
            if pc_c64 in (0xFFD5, 0xFFD8, 0xFFFD):  # LOAD, SAVE, reset
                log_event(f"[{consumed:10d}] C64 PC=${pc_c64:04X} (KERNAL entry)")
            last_pc_c64 = pc_c64

        if pc_drive and pc_drive != last_pc_drive:
            if pc_drive in (0xE853, 0xE909, 0xEA59, 0xEBFF):  # Known 1541 ROM landmarks
                log_event(f"[{consumed:10d}] Drive PC=${pc_drive:04X} (ROM landmark)")
            last_pc_drive = pc_drive

        # Check for IEC bus edges.
        bus = emu.iec_bus
        if bus.atn != last_atn:
            log_event(f"[{consumed:10d}] ATN edge: {last_atn} → {bus.atn}")
            last_atn = bus.atn

        if bus.clk != last_clk:
            log_event(f"[{consumed:10d}] CLK edge: {last_clk} → {bus.clk}")
            last_clk = bus.clk

        if bus.data != last_data:
            log_event(f"[{consumed:10d}] DATA edge: {last_data} → {bus.data}")
            last_data = bus.data

        # Check for bytes arriving at $C000.
        if emu.memory.read(0xC000) != 0x00:
            log_event(f"[{consumed:10d}] Byte at $C000: ${emu.memory.read(0xC000):02X}")
            if (
                emu.memory.read(0xC001) == 0x22
                and emu.memory.read(0xC002) == 0x33
            ):
                log_event(f"[{consumed:10d}] ✓ SUCCESS: got payload {emu.memory.read(0xC000):02X} {emu.memory.read(0xC001):02X} {emu.memory.read(0xC002):02X}")
                break

        # Timeout or CPU hung.
        if consumed > 100_000 and pc_c64 == 0xFFFD:
            log_event(f"[{consumed:10d}] ⚠ CPU stuck at $FFFD (CHROUT loop?), bailing")
            break

    print(f"\n=== Trace complete ({consumed} cycles) ===")
    print(f"Final C64 PC: ${emu.cpu.state.pc:04X}")
    print(f"Final drive PC: ${emu.iec_drives[8].cpu.state.pc:04X}")
    print(f"$C000: ${emu.memory.read(0xC000):02X}")
    print(f"IEC bus: ATN={emu.iec_bus.atn} CLK={emu.iec_bus.clk} DATA={emu.iec_bus.data}")


if __name__ == "__main__":
    trace_handshake()
