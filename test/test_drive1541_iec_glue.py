"""Stage-2 tests: VIA1 wired to IEC bus on Drive1541."""
import pytest

from c64py.drives.c1541_emulator import Drive1541
from c64py.iec_bus import IECBus


@pytest.fixture
def drive_on_bus():
    bus = IECBus()
    drive = Drive1541(device_number=8)
    # Provide a 16KB ROM so memory map is sane (zero-filled is fine for
    # this stage — we don't step the CPU).
    drive.load_rom(b"\x00" * 16384)
    bus.attach_device(drive)
    drive.notify_bus_change()
    return drive, bus


def test_idle_bus_reads_all_high(drive_on_bus):
    drive, bus = drive_on_bus
    pb = drive.memory.via1.read(0x00)
    # PB0 (DATA), PB2 (CLK) high. PB6 (ATN-IN) is INVERTED through a 7406
    # on the 1541 schematic, so an idle (high) ATN line reads LOW at PB6.
    assert pb & 0x01  # DATA in
    assert pb & 0x04  # CLK in
    assert not (pb & 0x40), "ATN released → PB6 reads LOW (inverter)"


def test_c64_pulls_atn_low_via1_sees_it(drive_on_bus):
    drive, bus = drive_on_bus
    bus.set_atn(False)
    pb = drive.memory.via1.read(0x00)
    # PB6 is inverted: bus low → PB6 reads HIGH.
    assert pb & 0x40, "PB6 should be HIGH when ATN line is low (inverter)"
    # CA1 also passes through the inverter, so DOS programs PCR for
    # rising-edge IRQ.
    drive.memory.via1.write(0x0E, 0x80 | 0x02)  # enable CA1
    drive.memory.via1.write(0x0C, 0x01)         # PCR: CA1 rising edge
    bus.set_atn(True)
    bus.set_atn(False)
    assert drive.memory.via1.irq_pending


def test_c64_pulls_clk_low_drive_sees_it(drive_on_bus):
    drive, bus = drive_on_bus
    bus.set_clk("c64", False)
    pb = drive.memory.via1.read(0x00)
    assert not (pb & 0x04)


def test_drive_pulls_data_low_appears_on_bus(drive_on_bus):
    drive, bus = drive_on_bus
    # DDRB: PB1 = output
    drive.memory.via1.write(0x02, 0x02)
    drive.memory.via1.write(0x00, 0x02)  # ORB PB1 = 1 -> pull DATA low
    assert not bus.data, "drive output should pull DATA low"
    # Release
    drive.memory.via1.write(0x00, 0x00)
    assert bus.data


def test_device_jumpers_for_devnum():
    bus = IECBus()
    for dev in (8, 9, 10, 11):
        d = Drive1541(device_number=dev)
        d.load_rom(b"\x00" * 16384)
        bus.attach_device(d)
        d.notify_bus_change()
        pb = d.memory.via1.read(0x00)
        # Decode PB4-5 as the inverse of (dev-8)
        jumper = (pb >> 4) & 0x03
        expected = ((dev - 8) & 0x03) ^ 0x03
        assert jumper == expected, f"dev {dev}: jumper={jumper:02b} expected={expected:02b}"
        bus.detach_device(d)


def test_drive_does_not_see_own_pull(drive_on_bus):
    drive, bus = drive_on_bus
    drive.memory.via1.write(0x02, 0x02)  # PB1 output
    drive.memory.via1.write(0x00, 0x02)  # pull DATA low
    # The drive itself should still see DATA "high" on its input pin
    # (its own pull is excluded from peer view). Real chip: the pin IS low
    # because the drive is pulling it low, but for our peer-state model the
    # drive's own contribution doesn't appear on PB0 input.
    pb = drive.memory.via1.read(0x00)
    # DDRB controls per-bit read source for ORB; PB1 is output and reads
    # back ORB's bit value (=1). PB0 is input and reflects peer view.
    assert pb & 0x01, "drive should not see its own DATA pull on PB0"
