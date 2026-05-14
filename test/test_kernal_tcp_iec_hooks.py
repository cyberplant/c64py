"""KERNAL TCP IEC fast hooks (OPEN / CHROUT gate / CLRCHN)."""

from c64py.cpu import CPU6502
from c64py.emulator import C64
from c64py.iec_bus import IECBus
from c64py.memory import MemoryMap


def test_rust_delegate_includes_tcp_iec_vectors_when_memory_flag_set() -> None:
    m = MemoryMap()
    m.kernal_rom = bytes(8192)  # avoid ``kernal_rom is None`` extra stop PCs
    cpu = CPU6502(m)
    cpu.kernal_disk_hook_vectors = False
    assert 0xFFC0 not in cpu._rust_delegate_stop_pcs()
    m.kernal_tcp_iec_vectors = True
    stops = cpu._rust_delegate_stop_pcs()
    assert 0xFFC0 in stops
    assert 0xFFC3 in stops
    assert 0xFFCF in stops


def test_ciout_hook_skips_when_dflto_is_screen() -> None:
    from c64py.kernal_tcp_iec_hooks import handle_kernal_tcp_iec

    emu = C64(interface_factory=lambda _e: None)
    emu.use_iec_bus = True
    emu.kernal_load_shortcut_enabled = True
    emu.iec_bus = IECBus()
    emu.memory.write(0x9A, 3)
    emu.cpu.state.pc = 0xFFD2
    emu.cpu.state.a = ord("X")
    assert handle_kernal_tcp_iec(emu) is False


def test_ciout_hook_sends_byte_when_tcp_drive_attached() -> None:
    from c64py.kernal_tcp_iec_hooks import handle_kernal_tcp_iec
    from c64py.drives.tcp_drive_client import TcpDriveClient

    class SpyTcp(TcpDriveClient):
        def __init__(self) -> None:
            super().__init__(8, "localhost", 1)
            self.received: list[int] = []

        def connect(self) -> bool:
            return True

        def iec_receive_byte(self, byte: int, eoi: bool = False) -> None:
            self.received.append(int(byte) & 0xFF)

    emu = C64(interface_factory=lambda _e: None)
    emu.use_iec_bus = True
    emu.kernal_load_shortcut_enabled = True
    bus = IECBus()
    emu.iec_bus = bus
    spy = SpyTcp()
    bus.attach_device(spy)
    emu.iec_drives[8] = spy
    emu.memory.write(0x9A, 8)
    emu.cpu.state.pc = 0xFFD2
    emu.cpu.state.a = 0x42
    bus.send_command(0x20 | 8)
    bus.send_command(0x60 | 2)
    assert handle_kernal_tcp_iec(emu) is True
    assert spy.received and spy.received[-1] == 0x42


def test_cia2_timer_b_underflow_sets_icr() -> None:
    from c64py.cpu import CPU6502
    from c64py.memory import MemoryMap

    m = MemoryMap()
    cpu = CPU6502(m)
    m.cia2_timer_b.latch = 10
    m.cia2_timer_b.counter = 10
    m.cia2_timer_b.running = True
    m.cia2_timer_b.irq_enabled = True
    cpu._update_cia_timers(15, recompute_irq=True)
    assert (m.cia2_icr & 0x02) != 0
    assert (m.cia2_icr & 0x80) != 0


def test_cia2_icr_read_clears() -> None:
    m = MemoryMap()
    m.iec_bus = IECBus()
    m.cia2_icr = 0x82
    v = m.read(0xDD0D)
    assert v == 0x82
    assert m.cia2_icr == 0
