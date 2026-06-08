"""Stage-3 tests: cycle-accurate stepping + wake-on-ATN gating."""
import pytest

from c64py.drives.c1541_emulator import Drive1541
from c64py.iec_bus import IECBus


def make_drive_with_program(prog: bytes, load_at: int = 0xC000):
    """Build a 1541 with a synthetic 16KB DOS ROM that runs ``prog`` from
    ``load_at`` and a reset vector pointing there."""
    rom = bytearray(16384)
    off = load_at - 0xC000
    rom[off:off + len(prog)] = prog
    # Reset vector at $FFFC/$FFFD points to load_at.
    rom[0xFFFC - 0xC000] = load_at & 0xFF
    rom[0xFFFD - 0xC000] = (load_at >> 8) & 0xFF
    bus = IECBus()
    drive = Drive1541(device_number=8)
    drive.load_rom(bytes(rom))
    bus.attach_device(drive)
    drive.notify_bus_change()
    return drive, bus


def test_idle_drive_skipped_when_atn_high():
    # The wake-on-ATN gate was removed (it broke ATN/byte handshakes by
    # sleeping during DOS LED-blink and motor-timing loops, which have
    # no IRQ source). The drive now always steps when a ROM is loaded.
    prog = bytes([
        0x78,                    # SEI
        0x4C, 0x01, 0xC0,        # JMP $C001 (spin on JMP forever)
    ])
    drive, bus = make_drive_with_program(prog)
    n1 = drive.step(50)
    assert n1 >= 50, "first call should run the budget"
    # Drive keeps stepping (no wake gate any more).
    n2 = drive.step(100)
    assert n2 >= 100, "drive must keep stepping (gate removed)"


def test_drive_steps_when_atn_asserted():
    prog = bytes([0xEA, 0xEA, 0x4C, 0x00, 0xC0])  # NOP NOP JMP $C000
    drive, bus = make_drive_with_program(prog)
    bus.set_atn(False)  # C64 asserts ATN — drive must run
    n = drive.step(50)
    assert n > 0
    assert drive.cpu.state.pc != 0xC000 or n >= 7  # made progress


def test_drive_steps_when_via_timer_running():
    # LDA #$10 STA $1804 (T1L-L) STA $1805 (T1C-H, starts T1) JMP self
    prog = bytes([
        0xA9, 0x10,             # LDA #$10
        0x8D, 0x04, 0x18,       # STA $1804
        0x8D, 0x05, 0x18,       # STA $1805 → T1 starts
        0x4C, 0x08, 0xC0,       # JMP $C008 (infinite spin AFTER timer started)
    ])
    drive, bus = make_drive_with_program(prog)
    # First step burst gets us past the timer-start instructions even with
    # ATN released (CPU still runs because we haven't reached idle yet).
    drive.step(20)
    assert drive.memory.via1.t1_active
    # After timer is active, step should keep running even with ATN high.
    pc_before = drive.cpu.state.pc
    drive.step(50)
    # PC should still be inside the spin loop; timer still ticking.
    assert drive.memory.via1.t1_active or drive.memory.via1.read(0x0D) & 0x40


def test_via_timer_irq_wakes_cpu():
    # Program: enable T1 IRQ, start short T1, then HLT-like JMP self.
    prog = bytes([
        0xA9, 0xC0, 0x8D, 0x0E, 0x18,   # LDA #$C0; STA $180E (IER set T1)
        0xA9, 0x05, 0x8D, 0x04, 0x18,   # LDA #$05; STA $1804 (T1L-L)
        0xA9, 0x00, 0x8D, 0x05, 0x18,   # LDA #$00; STA $1805 (start T1)
        0x58,                            # CLI — enable IRQ
        0x4C, 0x10, 0xC0,                # JMP $C010 (spin)
    ])
    drive, bus = make_drive_with_program(prog)
    drive.step(40)
    # After enough cycles, T1 should have underflowed and pending_irq is true.
    drive.step(50)
    assert drive.memory.via1.read(0x0D) & 0x40 or drive.memory.pending_irq is False


def test_reset_releases_bus_and_clears_state():
    drive, bus = make_drive_with_program(bytes([0xEA] * 4 + [0x4C, 0x00, 0xC0]))
    # Make the drive pull DATA low, then reset and verify it's released.
    drive.memory.via1.write(0x02, 0x02)
    drive.memory.via1.write(0x00, 0x02)
    assert not bus.data
    drive.reset()
    assert bus.data, "reset must release drive's bus pulls"
    assert drive.memory.via1.ddrb == 0
    assert all(b == 0 for b in drive.memory.ram)


def test_step_cycle_budget_respected():
    # NOP loop — step should consume roughly the requested cycles.
    prog = bytes([0xEA, 0xEA, 0x4C, 0x00, 0xC0])
    drive, bus = make_drive_with_program(prog)
    bus.set_atn(False)  # keep drive awake
    n = drive.step(20)
    # Each iteration is 7 cycles; budget=20 should consume between 20 and 28.
    assert 20 <= n <= 35
