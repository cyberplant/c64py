"""KERNAL TCP IEC fast hooks (OPEN / CHKIN / CHKOUT / CLRCHN / BASIN / CHROUT / CIOUT)."""

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
    assert 0xF9ED in stops
    assert 0xFDF9 in stops
    assert 0xFFC0 in stops
    assert 0xFFC3 in stops
    assert 0xFFCF in stops


def test_chkout_resolves_lfn_from_x_when_a_is_scratch() -> None:
    """BASIC may leave garbage in A and hold the logical file number in X at $FFC9."""
    from c64py.kernal_tcp_iec_hooks import FAT, LAT, SAT, handle_kernal_tcp_iec
    from c64py.drives.tcp_drive_client import TcpDriveClient

    class SpyTcp(TcpDriveClient):
        def __init__(self) -> None:
            super().__init__(8, "localhost", 1)

        def connect(self) -> bool:
            return True

    emu = C64(interface_factory=lambda _e: None)
    emu.use_iec_bus = True
    emu.kernal_load_shortcut_enabled = True
    bus = IECBus()
    emu.iec_bus = bus
    spy = SpyTcp()
    bus.attach_device(spy)
    emu.iec_drives[8] = spy
    idx = 0
    emu.memory.write(LAT + idx, 1)
    emu.memory.write(FAT + idx, 8)
    emu.memory.write(SAT + idx, 15)
    emu.cpu.state.pc = 0xFFC9
    emu.cpu.state.a = 0x22
    emu.cpu.state.x = 1
    assert handle_kernal_tcp_iec(emu) is True
    assert emu.memory.read(0x9A) == 8


def test_clrchn_vector_is_ffcc_not_ffc6() -> None:
    """CLRCHN is $FFCC (JMP $0322); $FFC6 is CHKIN — hooks must match."""
    from c64py.kernal_tcp_iec_hooks import handle_kernal_tcp_iec
    from c64py.drives.tcp_drive_client import TcpDriveClient

    class SpyTcp(TcpDriveClient):
        def __init__(self) -> None:
            super().__init__(8, "localhost", 1)

        def connect(self) -> bool:
            return True

    emu = C64(interface_factory=lambda _e: None)
    emu.use_iec_bus = True
    emu.kernal_load_shortcut_enabled = True
    bus = IECBus()
    emu.iec_bus = bus
    spy = SpyTcp()
    bus.attach_device(spy)
    emu.iec_drives[8] = spy
    emu.memory.write(0x9A, 8)
    emu.memory.write(0x99, 8)
    emu.cpu.state.pc = 0xFFCC
    assert handle_kernal_tcp_iec(emu) is True
    assert emu.memory.read(0x9A) == 3
    assert emu.memory.read(0x99) == 0


def test_chkin_vector_ffc6_sets_dfltn() -> None:
    from c64py.kernal_tcp_iec_hooks import FAT, LAT, SAT, handle_kernal_tcp_iec
    from c64py.drives.tcp_drive_client import TcpDriveClient

    class SpyTcp(TcpDriveClient):
        def __init__(self) -> None:
            super().__init__(8, "localhost", 1)

        def connect(self) -> bool:
            return True

    emu = C64(interface_factory=lambda _e: None)
    emu.use_iec_bus = True
    emu.kernal_load_shortcut_enabled = True
    bus = IECBus()
    emu.iec_bus = bus
    spy = SpyTcp()
    bus.attach_device(spy)
    emu.iec_drives[8] = spy
    idx = 0
    emu.memory.write(LAT + idx, 1)
    emu.memory.write(FAT + idx, 8)
    emu.memory.write(SAT + idx, 15)
    emu.cpu.state.pc = 0xFFC6
    emu.cpu.state.a = 1
    assert handle_kernal_tcp_iec(emu) is True
    assert emu.memory.read(0x99) == 8


def test_open_hook_uses_setlfs_zp_fa_sa_order() -> None:
    """SETLFS stores device in $B9 and secondary in $BA (not the LOAD vector layout)."""
    from c64py.kernal_tcp_iec_hooks import FAT, LAT, SAT, handle_kernal_tcp_iec
    from c64py.drives.tcp_drive_client import TcpDriveClient

    class SpyTcp(TcpDriveClient):
        def __init__(self) -> None:
            super().__init__(8, "localhost", 1)

        def connect(self) -> bool:
            return True

    emu = C64(interface_factory=lambda _e: None)
    emu.use_iec_bus = True
    emu.kernal_load_shortcut_enabled = True
    bus = IECBus()
    emu.iec_bus = bus
    spy = SpyTcp()
    bus.attach_device(spy)
    emu.iec_drives[8] = spy
    emu.cpu.state.pc = 0xFFC0
    emu.memory.write(0xB8, 1)
    emu.memory.write(0xB9, 8)
    emu.memory.write(0xBA, 15)
    emu.memory.write(0xB7, 0)
    assert handle_kernal_tcp_iec(emu) is True
    idx = next(i for i in range(10) if emu.memory.read(LAT + i) == 1)
    assert emu.memory.read(FAT + idx) == 8
    assert emu.memory.read(SAT + idx) == 15


def test_open_hook_heals_load_style_fa_in_ba() -> None:
    """If $B9/$BA look like SA=15 and FA=8 (LOAD layout), still open on device 8."""
    from c64py.kernal_tcp_iec_hooks import FAT, LAT, SAT, handle_kernal_tcp_iec
    from c64py.drives.tcp_drive_client import TcpDriveClient

    class SpyTcp(TcpDriveClient):
        def __init__(self) -> None:
            super().__init__(8, "localhost", 1)

        def connect(self) -> bool:
            return True

    emu = C64(interface_factory=lambda _e: None)
    emu.use_iec_bus = True
    emu.kernal_load_shortcut_enabled = True
    bus = IECBus()
    emu.iec_bus = bus
    spy = SpyTcp()
    bus.attach_device(spy)
    emu.iec_drives[8] = spy
    emu.cpu.state.pc = 0xFFC0
    emu.memory.write(0xB8, 1)
    emu.memory.write(0xB9, 15)
    emu.memory.write(0xBA, 8)
    emu.memory.write(0xB7, 0)
    assert handle_kernal_tcp_iec(emu) is True
    idx = next(i for i in range(10) if emu.memory.read(LAT + i) == 1)
    assert emu.memory.read(FAT + idx) == 8
    assert emu.memory.read(SAT + idx) == 15


def test_chkout_heals_swapped_fat_sat_from_bad_open() -> None:
    """CHKOUT repairs FAT=15 SAT=8 so PRINT# can use the TCP CIOUT hook."""
    from c64py.kernal_tcp_iec_hooks import FAT, LAT, SAT, handle_kernal_tcp_iec
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
    idx = 0
    emu.memory.write(LAT + idx, 1)
    emu.memory.write(FAT + idx, 15)
    emu.memory.write(SAT + idx, 8)
    bus.send_command(0x20 | 8)
    bus.send_command(0xF0 | 15)
    bus.unlisten()
    emu.cpu.state.pc = 0xFFC9
    emu.cpu.state.a = 1
    assert handle_kernal_tcp_iec(emu) is True
    assert emu.memory.read(FAT + idx) == 8
    assert emu.memory.read(SAT + idx) == 15
    assert emu.memory.read(0x9A) == 8
    emu.cpu.state.pc = 0xFFD2
    emu.cpu.state.a = ord("Z")
    assert handle_kernal_tcp_iec(emu) is True
    assert spy.received and spy.received[-1] == ord("Z")


def test_ciout_hook_sends_byte_from_f9ed_bsout_vector() -> None:
    """BASIC PRINT# calls BSOUT ($F9ED) per character after CHKOUT."""
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
    emu.cpu.state.pc = 0xF9ED
    emu.cpu.state.a = 0x33
    bus.send_command(0x20 | 8)
    bus.send_command(0x60 | 15)
    assert handle_kernal_tcp_iec(emu) is True
    assert spy.received and spy.received[-1] == 0x33


def test_ciout_hook_sends_byte_from_fdf9_ciout_vector() -> None:
    """KERNAL CIOUT ($FDF9) is what PRINT# usually calls per character, not CHROUT."""
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
    emu.cpu.state.pc = 0xFDF9
    emu.cpu.state.a = 0x55
    bus.send_command(0x20 | 8)
    bus.send_command(0x60 | 15)
    assert handle_kernal_tcp_iec(emu) is True
    assert spy.received and spy.received[-1] == 0x55


def test_ciout_hook_skips_when_dflto_is_screen() -> None:
    from c64py.kernal_tcp_iec_hooks import handle_kernal_tcp_iec

    emu = C64(interface_factory=lambda _e: None)
    emu.use_iec_bus = True
    emu.kernal_load_shortcut_enabled = True
    emu.iec_bus = IECBus()
    emu.memory.write(0x9A, 3)
    emu.cpu.state.a = ord("X")
    emu.cpu.state.pc = 0xFDF9
    assert handle_kernal_tcp_iec(emu) is False
    emu.cpu.state.pc = 0xFFD2
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
