"""Unit tests for :mod:`c64py.iec_wire_decode` (A3a ATN command bytes)."""

from __future__ import annotations

from c64py.iec_bus import IECBus
from c64py.iec_kernal_bridge import KernalIecTap
from c64py.iec_wire_decode import IecAtnWireDecoder
from c64py.memory import MemoryMap


def _feed_state_path(decoder: IecAtnWireDecoder, states: list[tuple[bool, bool, bool]]) -> None:
    prev = states[0]
    for s in states[1:]:
        if s == prev:
            continue
        decoder.feed_transition(prev, s)
        prev = s


def _synthetic_atn_burst(commands: list[int]) -> list[tuple[bool, bool, bool]]:
    """Build a minimal resolved-bus path: idle → ATN held → per-byte 8 CLK lows (LSB first)."""
    seq: list[tuple[bool, bool, bool]] = [(True, True, True), (False, True, True)]
    for cmd in commands:
        for bit_i in range(8):
            bit = (cmd >> bit_i) & 1
            data_high = bit == 0
            seq.append((False, True, data_high))
            seq.append((False, False, data_high))
            seq.append((False, True, data_high))
    seq.append((True, True, True))
    return seq


def test_atn_wire_decoder_listen_open_unlisten():
    bus = IECBus()
    dec = IecAtnWireDecoder(bus)
    path = _synthetic_atn_burst([0x28, 0xF0, 0x3F])
    _feed_state_path(dec, path)
    assert dec.commands_delivered == [0x28, 0xF0, 0x3F]
    assert bus.current_listener is None
    assert bus.secondary_phase == "idle"


def test_atn_wire_decoder_talk_secondary_untalk():
    bus = IECBus()
    dec = IecAtnWireDecoder(bus)
    path = _synthetic_atn_burst([0x48, 0x60, 0x5F])
    _feed_state_path(dec, path)
    assert dec.commands_delivered == [0x48, 0x60, 0x5F]
    assert bus.current_talker is None


def _bus_triple_to_cia2_pra(atn_released: bool, clk_released: bool, data_released: bool) -> int:
    """Match :meth:`MemoryMap.apply_cia2_port_a_to_iec_bus` (bits 3..5)."""
    v = 0xFF
    if atn_released:
        v &= ~0x08
    else:
        v |= 0x08
    if clk_released:
        v &= ~0x10
    else:
        v |= 0x10
    if data_released:
        v &= ~0x20
    else:
        v |= 0x20
    return v


def test_kernal_tap_invokes_wire_decoder_on_cia2_changes():
    mem = MemoryMap()
    bus = IECBus()
    mem.iec_bus = bus
    mem.iec_kernal_tap = KernalIecTap(wire_decode_bus=bus)
    dec = mem.iec_kernal_tap._wire_decoder
    assert dec is not None

    states = _synthetic_atn_burst([0x28, 0x3F])
    mem.cia2_pra = _bus_triple_to_cia2_pra(*states[0])
    mem.apply_cia2_port_a_to_iec_bus()
    for triple in states[1:]:
        mem.cia2_pra = _bus_triple_to_cia2_pra(*triple)
        mem.apply_cia2_port_a_to_iec_bus()

    assert dec.commands_delivered == [0x28, 0x3F]
