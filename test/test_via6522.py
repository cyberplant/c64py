"""Tests for the 6522 VIA implementation."""
import pytest

from c64py.via6522 import VIA6522, IFR_T1, IFR_T2, IFR_CA1, IFR_CB1


def test_reset_state():
    via = VIA6522()
    assert via.read(0x0D) == 0  # IFR
    assert not via.irq_pending
    assert via.read(0x02) == 0  # DDRB
    assert via.read(0x03) == 0  # DDRA


def test_t1_one_shot_fires_irq_once():
    via = VIA6522()
    # IER bit 7 set + T1 bit -> enable T1 IRQ
    via.write(0x0E, 0x80 | 0x40)
    # ACR: T1 one-shot (bit6=0)
    via.write(0x0B, 0x00)
    # Latch low, then high triggers transfer + start
    via.write(0x06, 0x09)   # T1L-L = 9
    via.write(0x05, 0x00)   # T1C-H = 0 -> count from 9
    assert via.t1_active

    # 5 cycles: still counting (9 -> 4)
    via.tick(5)
    assert not via.irq_pending

    # Past underflow (9 -> -1): IRQ fires once
    via.tick(20)
    assert via.irq_pending
    assert via.read(0x0D) & IFR_T1

    # Clear by reading T1C-L
    via.read(0x04)
    assert not (via.read(0x0D) & IFR_T1)
    # Continue ticking — one-shot does NOT re-fire
    via.tick(100)
    assert not (via.read(0x0D) & IFR_T1)


def test_t1_free_run_repeats():
    via = VIA6522()
    via.write(0x0E, 0x80 | 0x40)         # IER: T1 enable
    via.write(0x0B, 0x40)                # ACR: T1 free-run
    via.write(0x06, 0x04)                # latch low
    via.write(0x05, 0x00)                # high+start; period = 4+2 = 6
    # First underflow at ~5 cycles, second at ~11
    via.tick(20)
    # IRQ should still be pending; clear and check it re-fires
    assert via.irq_pending
    via.write(0x0D, 0x7F)  # clear all IFR
    assert not via.irq_pending
    via.tick(8)
    assert via.irq_pending


def test_t2_one_shot_timer():
    via = VIA6522()
    via.write(0x0E, 0x80 | 0x20)
    via.write(0x0B, 0x00)  # T2 in timed mode (bit5=0)
    via.write(0x08, 0x10)
    via.write(0x09, 0x00)  # start, count from 0x0010 = 16
    via.tick(10)
    assert not via.irq_pending
    via.tick(20)
    assert via.irq_pending
    assert via.read(0x0D) & IFR_T2
    via.read(0x08)  # reading T2C-L clears
    assert not (via.read(0x0D) & IFR_T2)


def test_ca1_falling_edge_sets_ifr():
    via = VIA6522()
    via.write(0x0E, 0x80 | 0x02)  # IER: CA1
    via.write(0x0C, 0x00)         # PCR: CA1 active falling
    via.set_ca1(True)             # idle high
    assert not via.irq_pending
    via.set_ca1(False)            # falling edge
    assert via.irq_pending
    assert via.read(0x0D) & IFR_CA1
    # Reading port A handshake reg clears CA1 IFR
    via.read(0x01)
    assert not (via.read(0x0D) & IFR_CA1)


def test_ca1_rising_edge_when_pcr_bit0_set():
    via = VIA6522()
    via.write(0x0E, 0x80 | 0x02)
    via.write(0x0C, 0x01)         # PCR: CA1 active rising
    via.set_ca1(False)
    via.set_ca1(False)            # no edge
    assert not via.irq_pending
    via.set_ca1(True)             # rising edge
    assert via.irq_pending


def test_pa_latch_on_ca1_edge():
    via = VIA6522()
    via.write(0x0B, 0x01)         # ACR bit0: PA latch
    via.write(0x0C, 0x00)         # CA1 active falling
    via.set_pa_in(0xAA)
    via.set_ca1(True)             # idle high — no edge
    via.set_ca1(False)            # falling edge: latch IRA from pa_in
    via.set_pa_in(0x55)           # changes pin but latched IRA stays
    assert via.read(0x0F) == 0xAA


def test_port_b_output_via_ddrb():
    captured = {}

    def cb(orb, ddrb):
        captured["orb"] = orb
        captured["ddrb"] = ddrb

    via = VIA6522(on_pb_write=cb)
    via.write(0x02, 0xF0)         # DDRB: high nibble outputs
    assert captured == {"orb": 0, "ddrb": 0xF0}
    via.write(0x00, 0xA5)         # ORB
    assert captured["orb"] == 0xA5
    assert via.get_pb_out() == (0xA5 & 0xF0) | (0x0F)  # input bits hi-Z
    # Now drive the input bits low externally
    via.set_pb_in(0x00)
    assert via.read(0x00) == ((0xA5 & 0xF0) | (0x00 & 0x0F))


def test_ier_set_and_clear():
    via = VIA6522()
    via.write(0x0E, 0x80 | 0x42)  # set T1 + CA1
    assert (via.read(0x0E) & 0x7F) == 0x42
    via.write(0x0E, 0x40)         # clear T1
    assert (via.read(0x0E) & 0x7F) == 0x02


def test_ifr_top_bit_aggregates():
    via = VIA6522()
    via.write(0x0E, 0x80 | 0x40)
    via.write(0x06, 0x01)
    via.write(0x05, 0x00)
    via.tick(10)
    assert via.read(0x0D) & 0x80  # bit7 mirrors active enabled IRQ
    via.read(0x04)                # clear T1
    assert not (via.read(0x0D) & 0x80)
