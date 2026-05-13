"""Unit tests for :mod:`c64py.iec_wire_decode` (A3a–A3c partial)."""

from __future__ import annotations

from c64py.iec_bus import IECBus
from c64py.iec_kernal_bridge import KernalIecTap
from c64py.iec_wire_decode import IecAtnWireDecoder
from c64py.memory import MemoryMap


class _IecRxRecorder:
    """Minimal bus device that records :meth:`iec_receive_byte` calls."""

    device_number = 8
    iec_bus = None

    def __init__(self) -> None:
        self.rx: list[tuple[int, bool]] = []

    def on_atn_changed(self, _atn_state: bool) -> None:
        pass

    def on_listen(self) -> None:
        pass

    def on_unlisten(self) -> None:
        pass

    def on_talk(self) -> None:
        pass

    def on_untalk(self) -> None:
        pass

    def on_secondary_address(self, _channel: int) -> None:
        pass

    def iec_open_channel(self, _channel: int) -> None:
        pass

    def iec_close_channel(self, _channel: int) -> None:
        pass

    def iec_secondary(self, _channel: int, _kind: str) -> None:
        pass

    def iec_unlisten(self) -> None:
        pass

    def iec_untalk(self) -> None:
        pass

    def iec_receive_byte(self, byte: int, eoi: bool = False) -> None:
        self.rx.append((byte & 0xFF, bool(eoi)))


def _append_wired_byte(
    seq: list[tuple[bool, bool, bool]],
    atn_released: bool,
    byte_val: int,
) -> None:
    for bit_i in range(8):
        bit = (byte_val >> bit_i) & 1
        data_high = bit == 0
        seq.append((atn_released, True, data_high))
        seq.append((atn_released, False, data_high))
        seq.append((atn_released, True, data_high))


def _synthetic_open_with_filename(name: bytes) -> list[tuple[bool, bool, bool]]:
    """LISTEN 8 + OPEN 0 under ATN, ATN up, filename bytes, UNLISTEN under ATN."""
    seq: list[tuple[bool, bool, bool]] = [(True, True, True), (False, True, True)]
    for cmd in (0x28, 0xF0):
        _append_wired_byte(seq, False, cmd)
    seq.append((True, True, True))
    for b in name:
        _append_wired_byte(seq, True, b)
    seq.append((False, True, True))
    _append_wired_byte(seq, False, 0x3F)
    seq.append((True, True, True))
    return seq


def _synthetic_listen_data_byte_unlisten(data_byte: int = 0x41) -> list[tuple[bool, bool, bool]]:
    """LISTEN 8 + secondary DATA ch1, payload byte, UNLISTEN."""
    seq: list[tuple[bool, bool, bool]] = [(True, True, True), (False, True, True)]
    for cmd in (0x28, 0x61):
        _append_wired_byte(seq, False, cmd)
    seq.append((True, True, True))
    _append_wired_byte(seq, True, data_byte)
    seq.append((False, True, True))
    _append_wired_byte(seq, False, 0x3F)
    seq.append((True, True, True))
    return seq


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


def test_wire_decoder_open_filename_bytes():
    bus = IECBus()
    bus.attach_device(_IecRxRecorder())
    dec = IecAtnWireDecoder(bus)
    path = _synthetic_open_with_filename(b"AB")
    _feed_state_path(dec, path)
    assert dec.commands_delivered == [0x28, 0xF0, 0x3F]
    assert dec.bytes_sent == [(0x41, False), (0x42, True)]
    dev = bus.devices[0]
    assert isinstance(dev, _IecRxRecorder)
    assert dev.rx == [(0x41, False), (0x42, True)]


def test_wire_decoder_data_phase_one_byte():
    bus = IECBus()
    bus.attach_device(_IecRxRecorder())
    dec = IecAtnWireDecoder(bus)
    path = _synthetic_listen_data_byte_unlisten(0x55)
    _feed_state_path(dec, path)
    assert dec.commands_delivered == [0x28, 0x61, 0x3F]
    assert dec.bytes_sent == [(0x55, True)]
    dev = bus.devices[0]
    assert isinstance(dev, _IecRxRecorder)
    assert dev.rx == [(0x55, True)]


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
    assert dec.bytes_sent == []


def test_kernal_tap_open_filename_via_cia2():
    mem = MemoryMap()
    bus = IECBus()
    bus.attach_device(_IecRxRecorder())
    mem.iec_bus = bus
    mem.iec_kernal_tap = KernalIecTap(wire_decode_bus=bus)
    dec = mem.iec_kernal_tap._wire_decoder
    assert dec is not None
    states = _synthetic_open_with_filename(b"X")
    mem.cia2_pra = _bus_triple_to_cia2_pra(*states[0])
    mem.apply_cia2_port_a_to_iec_bus()
    for triple in states[1:]:
        mem.cia2_pra = _bus_triple_to_cia2_pra(*triple)
        mem.apply_cia2_port_a_to_iec_bus()
    assert dec.commands_delivered == [0x28, 0xF0, 0x3F]
    assert dec.bytes_sent == [(0x58, True)]
    dev = bus.devices[0]
    assert isinstance(dev, _IecRxRecorder)
    assert dev.rx == [(0x58, True)]
