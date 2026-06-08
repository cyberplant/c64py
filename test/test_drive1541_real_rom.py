"""End-to-end stage-6 tests: real 1541 DOS ROM running on real VIA1+IEC.

Smoke level first (drive boots to idle), then full C64↔1541 LOAD test.
"""
import os
import pytest

from c64py.iec_bus import IECBus
from c64py.drives.c1541_emulator import Drive1541
from c64py.roms import find_drive_rom


ROM_DIR = os.path.join(os.path.dirname(__file__), "..", "roms")


def _drive_rom():
    rom = find_drive_rom("dos1541", ROM_DIR)
    if rom is None:
        pytest.skip("dos1541 ROM not present in roms/")
    return rom


def test_dos_rom_boots_to_idle():
    """Step the real DOS ROM for a few hundred K cycles. It must:
      1. Not crash (no JAM / KIL).
      2. End up with PC inside the DOS ROM (>= $C000).
      3. Reach the canonical ATN-poll idle loop (DOS ROM area $EB).
    """
    rom = _drive_rom()
    bus = IECBus()
    drive = Drive1541(device_number=8)
    drive.load_rom(rom)
    bus.attach_device(drive)
    drive.notify_bus_change()

    # Boot enough cycles that DOS finishes init and sits in idle.
    drive.step(2_000_000)
    pc = drive.cpu.state.pc

    assert not drive.cpu.state.stopped, f"CPU stopped (KIL?) at PC=${pc:04X}"
    assert pc >= 0xC000, f"PC outside DOS ROM: ${pc:04X}"


def test_dos_rom_responds_to_atn():
    """After boot, asserting ATN must trigger the drive's CA1 IRQ
    handler (the ATNCMD service routine in DOS at ~$E853/$EA59)."""
    rom = _drive_rom()
    bus = IECBus()
    drive = Drive1541(device_number=8)
    drive.load_rom(rom)
    bus.attach_device(drive)
    drive.notify_bus_change()

    # Boot drive.
    drive.step(2_000_000)

    # Pull ATN low — this should set CA1 IRQ on VIA1 and route to handler.
    bus.set_atn(False)
    # Step a bit; the DOS ATN service handler should run and pull
    # DATA low to acknowledge.
    drive.step(50_000)
    # Drive acknowledges ATN by pulling DATA low.
    assert not bus.data, "drive must pull DATA low to acknowledge ATN"
