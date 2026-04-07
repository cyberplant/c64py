"""IEC peer-line helpers, disk emulation wiring, and logical IEC byte delivery."""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from c64py.emulator import C64  # noqa: E402
from c64py.iec_bus import IECBus  # noqa: E402
from c64py.memory import MemoryMap  # noqa: E402


class _FakeIECDevice:
    device_number = 8
    iec_bus = None

    def __init__(self) -> None:
        self.listening = False
        self.current_channel = 0
        self.command_buffer: list[int] = []

    def on_listen(self) -> None:
        self.listening = True

    def on_unlisten(self) -> None:
        self.listening = False

    def on_talk(self) -> None:
        pass

    def on_untalk(self) -> None:
        pass

    def on_secondary_address(self, channel: int) -> None:
        self.current_channel = channel

    def on_atn_changed(self, _state: bool) -> None:
        pass

    def receive_byte(self, byte: int) -> None:
        self.command_buffer.append(byte)

    def send_byte(self):
        return None


def test_peer_clk_excludes_c64() -> None:
    bus = IECBus()
    bus.set_clk("c64", True)
    bus.set_clk("drive8", False)
    assert bus.peer_clk_high() is False
    bus.set_clk("drive8", True)
    assert bus.peer_clk_high() is True


def test_memory_apply_cia2_to_iec_bus() -> None:
    bus = IECBus()
    mem = MemoryMap()
    mem.iec_bus = bus
    mem.cia2_pra = 0xFF
    mem.apply_cia2_port_a_to_iec_bus()
    assert bus.atn is True
    mem.cia2_pra = 0xFF & ~0x08
    mem.apply_cia2_port_a_to_iec_bus()
    assert bus.atn is False


def test_iec_send_byte_reaches_listener() -> None:
    bus = IECBus()
    d = _FakeIECDevice()
    bus.attach_device(d)
    bus.send_command(0x28)  # LISTEN 8
    bus.send_command(0x6F)  # secondary 15
    assert d.listening
    assert d.current_channel == 15
    bus.send_byte(0x42)
    assert list(d.command_buffer) == [0x42]


def test_c64_disk_emulation_default_fast() -> None:
    emu = C64(interface_factory=lambda _e: None)
    assert emu.disk_emulation == "fast"


def test_c64_disk_emulation_accurate_ctor() -> None:
    emu = C64(interface_factory=lambda _e: None, disk_emulation="accurate")
    assert emu.disk_emulation == "accurate"


def test_initialize_iec_idempotent() -> None:
    emu = C64(interface_factory=lambda _e: None)
    assert emu.initialize_iec_bus(rom_dir="__no_such_rom_dir__") is False
    assert emu.initialize_iec_bus(rom_dir="__no_such_rom_dir__") is False
