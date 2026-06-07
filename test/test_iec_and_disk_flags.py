"""IEC peer-line helpers, disk emulation wiring, and logical IEC byte delivery."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from c64py.emulator import C64  # noqa: E402
from c64py.iec_bus import IECBus  # noqa: E402
from c64py.memory import MemoryMap  # noqa: E402
from c64py.roms import find_drive_rom  # noqa: E402


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


def test_kernal_load_save_hooks_skipped_when_iec_bus_active() -> None:
    emu = C64(interface_factory=lambda _e: None)
    emu.use_iec_bus = True
    emu.cpu.state.pc = 0xFFD5
    assert emu._handle_kernal_load() is False
    emu.cpu.state.pc = 0xFFD8
    assert emu._handle_kernal_save() is False


def test_rust_stop_pcs_omit_disk_vectors_when_kernal_disk_hooks_off() -> None:
    from c64py.cpu import CPU6502
    from c64py.memory import MemoryMap

    cpu = CPU6502(MemoryMap())
    assert 0xFFD5 in cpu._rust_delegate_stop_pcs()
    assert 0xFFD8 in cpu._rust_delegate_stop_pcs()
    cpu.kernal_disk_hook_vectors = False
    stops = cpu._rust_delegate_stop_pcs()
    assert 0xFFD5 not in stops
    assert 0xFFD8 not in stops
    assert 0xFFD2 in stops

    cpu.memory.iec_bus = object()  # IEC active, stub mode (not full KERNAL disk in Rust)
    stops = cpu._rust_delegate_stop_pcs()
    assert 0xFFD5 in stops
    assert 0xFFD8 in stops
    cpu.memory.iec_disk_full_impl = True
    stops = cpu._rust_delegate_stop_pcs()
    assert 0xFFD5 not in stops
    assert 0xFFD8 not in stops


def test_initialize_iec_idempotent(tmp_path) -> None:
    """Without drive ROMs in the given dir, IEC init fails; must not scan other paths."""
    empty = tmp_path / "roms"
    empty.mkdir()
    with patch("c64py.roms.iter_candidate_rom_dirs", return_value=[]):
        emu = C64(interface_factory=lambda _e: None)
        assert emu.initialize_iec_bus(rom_dir=str(empty)) is False
        assert emu.initialize_iec_bus(rom_dir=str(empty)) is False


def test_find_drive_rom_vice_plus_name_16k_dos_only(tmp_path) -> None:
    """VICE DRIVES/ ``dos1541-…+….bin`` is normally the full 16 KiB DOS image only."""
    dos_only = bytes((i * 3) & 0xFF for i in range(16384))
    (tmp_path / "dos1541-325302-01+901229-05.bin").write_bytes(dos_only)
    assert find_drive_rom("dos1541", str(tmp_path)) == dos_only
    assert find_drive_rom("serial1541", str(tmp_path)) is None
    (tmp_path / "901229-05.bin").write_bytes(b"Z" * 8192)
    assert find_drive_rom("serial1541", str(tmp_path)) == b"Z" * 8192


def test_find_drive_rom_vice_combined_24k(tmp_path) -> None:
    """Rare 24 KiB single file: DOS (16 KiB) + serial (8 KiB)."""
    dos_part = bytes((i & 0xFF) for i in range(16384))
    ser_part = bytes(((i + 17) & 0xFF) for i in range(8192))
    combined = dos_part + ser_part
    assert len(combined) == 24576
    (tmp_path / "dos1541-325302-01+901229-05.bin").write_bytes(combined)

    assert find_drive_rom("dos1541", str(tmp_path)) == dos_part
    assert find_drive_rom("serial1541", str(tmp_path)) == ser_part


def test_find_drive_rom_vice_combined_glob_revision(tmp_path) -> None:
    """Glob dos1541*+*.bin with 24 KiB still splits DOS + serial."""
    dos_part = b"D" * 16384
    ser_part = b"S" * 8192
    (tmp_path / "dos1541-325302-01+901229-07.bin").write_bytes(dos_part + ser_part)
    assert find_drive_rom("dos1541", str(tmp_path)) == dos_part
    assert find_drive_rom("serial1541", str(tmp_path)) == ser_part
