"""Regression: drive activity LED reads VIA2 PB3, not VIA1 PB3.

VIA1 PB3 is the IEC CLK-out line on the 1541 — it toggles based on serial
bus handshakes, not drive activity, so reading it as the LED was wrong.
The actual 1541 drive LED is wired to VIA2 ($1C00) port B bit 3, active
high in the latch.
"""

from c64py.drives.c1541_emulator import Drive1541


def test_led_off_at_reset():
    drv = Drive1541(device_number=8)
    # Default DDRB has bit 3 cleared (input) -> LED off regardless of ORB.
    assert drv.led_on is False


def test_led_follows_via2_pb3_when_output():
    drv = Drive1541(device_number=8)
    via2 = drv.memory.via2
    # Configure PB3 as output, drive high -> LED on.
    via2.write(0x2, 0x08)  # DDRB
    via2.write(0x0, 0x08)  # ORB bit 3 set
    assert drv.led_on is True
    # Clear ORB bit 3 -> LED off.
    via2.write(0x0, 0x00)
    assert drv.led_on is False


def test_led_off_when_pb3_is_input():
    drv = Drive1541(device_number=8)
    via2 = drv.memory.via2
    # ORB bit 3 set, but DDRB bit 3 cleared (input): LED stays off.
    via2.write(0x0, 0x08)
    via2.write(0x2, 0x00)
    assert drv.led_on is False


def test_led_unaffected_by_via1_pb3():
    """VIA1 PB3 (IEC CLK out) must not influence the LED."""
    drv = Drive1541(device_number=8)
    via1 = drv.memory.via1
    via1.write(0x2, 0x08)  # DDRB
    via1.write(0x0, 0x08)  # ORB
    # VIA2 untouched -> LED must remain off.
    assert drv.led_on is False
