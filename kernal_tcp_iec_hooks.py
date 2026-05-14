"""KERNAL jump-table fast path for logical IEC over :class:`~c64py.drives.tcp_drive_client.TcpDriveClient`.

These hooks run only when :attr:`c64py.emulator.C64.kernal_load_shortcut_enabled` is on
and the target device is 8–11 with a TCP drive client attached. They mirror the KERNAL
calling convention at the official vectors and return via synthetic ``RTS`` without
executing KERNAL ROM, so the bit-level CIA2 wire decoder remains available when this
path is disabled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from c64py.emulator import C64

# Logical file tables (KERNAL)
LAT = 0x259
FAT = 0x263
SAT = 0x26D
LDTND = 0x98
DFLTN = 0x99
DFLTO = 0x9A


def _slot_for_lfn(mem, lfn: int) -> int:
    for i in range(10):
        if mem.read(LAT + i) == lfn:
            return i
    return -1


def _first_free_slot(mem) -> int:
    for i in range(10):
        if mem.read(LAT + i) == 0:
            return i
    return -1


def _iec_error(emu: "C64", code: int) -> None:
    emu.cpu.state.a = code & 0xFF
    emu.memory.write(0x90, 0x00)
    emu.cpu.state.p |= 0x01


def _iec_ok(emu: "C64") -> None:
    emu.cpu.state.p &= ~0x01
    emu.memory.write(0x90, 0x00)


def handle_kernal_tcp_iec(emu: "C64") -> bool:
    """If PC is a handled KERNAL vector and the target is a TCP disk device, emulate and RTS.

    Returns True when the instruction at the vector was consumed (caller should not
    execute the KERNAL opcode).
    """
    if not emu.kernal_load_shortcut_enabled or not getattr(emu, "use_iec_bus", False):
        return False
    bus = emu.iec_bus
    if bus is None:
        return False

    pc = emu.cpu.state.pc & 0xFFFF

    if pc == 0xFFC0:
        return _hook_open(emu, bus)
    if pc == 0xFFC3:
        return _hook_close(emu, bus)
    if pc == 0xFFC6:
        return _hook_clrchn(emu, bus)
    if pc == 0xFFC9:
        return _hook_chkout(emu, bus)
    if pc == 0xFFCC:
        return _hook_chkin(emu, bus)
    if pc == 0xFFCF:
        return _hook_basin(emu, bus)
    if pc == 0xFFD2:
        return _hook_ciout(emu, bus)
    return False


def _tcp_client(emu: "C64", device: int):
    if device < 8 or device > 11:
        return None
    return emu.get_drive(device)


def _hook_open(emu: "C64", bus) -> bool:
    # SETLFS ($FFBA): A→$B8 (LA), X→$B9 (FA device), Y→$BA (SA secondary).
    lfn = emu.memory.read(0xB8)
    device = emu.memory.read(0xB9)
    secondary = emu.memory.read(0xBA)
    client = _tcp_client(emu, device)
    if client is None:
        return False

    fn_len = emu.memory.read(0xB7)
    fn_ptr = emu.memory.read(0xBB) | (emu.memory.read(0xBC) << 8)
    fname = bytes(
        emu.memory.read((fn_ptr + i) & 0xFFFF) for i in range(fn_len)
    )

    idx_existing = _slot_for_lfn(emu.memory, lfn)
    idx = idx_existing if idx_existing >= 0 else _first_free_slot(emu.memory)
    if idx < 0:
        _iec_error(emu, 6)  # too many files
        emu._kernal_hook_rts_return()
        return True

    was_empty = emu.memory.read(LAT + idx) == 0

    bus.unlisten()
    bus.untalk()
    if not bus.send_command(0x20 | device):
        _iec_error(emu, 5)
        emu._kernal_hook_rts_return()
        return True
    if not bus.open_channel(secondary, fname if fn_len else b""):
        _iec_error(emu, 5)
        emu._kernal_hook_rts_return()
        return True

    emu.memory.write(LAT + idx, lfn & 0xFF)
    emu.memory.write(FAT + idx, device & 0xFF)
    emu.memory.write(SAT + idx, secondary & 0xFF)
    if was_empty:
        n = emu.memory.read(LDTND) & 0xFF
        emu.memory.write(LDTND, min(10, n + 1))

    _iec_ok(emu)
    emu._kernal_hook_rts_return()
    return True


def _hook_close(emu: "C64", bus) -> bool:
    lfn = emu.cpu.state.a & 0xFF
    idx = _slot_for_lfn(emu.memory, lfn)
    if idx < 0:
        _iec_ok(emu)
        emu._kernal_hook_rts_return()
        return True

    device = emu.memory.read(FAT + idx)
    secondary = emu.memory.read(SAT + idx)
    client = _tcp_client(emu, device)
    if client is None:
        return False

    bus.unlisten()
    bus.untalk()
    bus.send_command(0x20 | device)
    bus.close_channel(secondary)
    bus.unlisten()

    emu.memory.write(LAT + idx, 0)
    emu.memory.write(FAT + idx, 0)
    emu.memory.write(SAT + idx, 0)
    n = max(0, (emu.memory.read(LDTND) & 0xFF) - 1)
    emu.memory.write(LDTND, n)

    _iec_ok(emu)
    emu._kernal_hook_rts_return()
    return True


def _hook_clrchn(emu: "C64", bus) -> bool:
    # Always safe to release the logical IEC layer; only skip when no TCP drives exist
    # so local accurate IEC keeps exclusive bus ownership.
    if not emu.iec_drives:
        return False
    if not any(_tcp_client(emu, d) is not None for d in range(8, 12)):
        return False

    bus.unlisten()
    bus.untalk()
    emu.memory.write(DFLTN, 0)
    emu.memory.write(DFLTO, 3)
    _iec_ok(emu)
    emu._kernal_hook_rts_return()
    return True


def _hook_chkout(emu: "C64", bus) -> bool:
    lfn = emu.cpu.state.a & 0xFF
    idx = _slot_for_lfn(emu.memory, lfn)
    if idx < 0:
        return False
    device = emu.memory.read(FAT + idx)
    secondary = emu.memory.read(SAT + idx)
    if _tcp_client(emu, device) is None:
        return False

    bus.unlisten()
    bus.untalk()
    bus.send_command(0x20 | device)
    bus.send_command(0x60 | secondary)

    emu.memory.write(DFLTO, device & 0xFF)
    _iec_ok(emu)
    emu._kernal_hook_rts_return()
    return True


def _hook_chkin(emu: "C64", bus) -> bool:
    lfn = emu.cpu.state.a & 0xFF
    idx = _slot_for_lfn(emu.memory, lfn)
    if idx < 0:
        return False
    device = emu.memory.read(FAT + idx)
    secondary = emu.memory.read(SAT + idx)
    if _tcp_client(emu, device) is None:
        return False

    bus.unlisten()
    bus.untalk()
    bus.send_command(0x40 | device)
    bus.send_command(0x60 | secondary)

    emu.memory.write(DFLTN, device & 0xFF)
    _iec_ok(emu)
    emu._kernal_hook_rts_return()
    return True


def _hook_basin(emu: "C64", bus) -> bool:
    if (emu.memory.read(DFLTN) & 0xFF) < 8 or (emu.memory.read(DFLTN) & 0xFF) > 11:
        return False
    device = emu.memory.read(DFLTN) & 0xFF
    if _tcp_client(emu, device) is None:
        return False

    raw = bus.receive_byte()
    if raw is None:
        _iec_error(emu, 5)
        emu._kernal_hook_rts_return()
        return True
    if isinstance(raw, tuple):
        byte, eoi = raw
        emu.cpu.state.a = byte & 0xFF
        st = emu.memory.read(0x90) & 0xFF
        if eoi:
            st |= 0x40
        else:
            st &= ~0x40
        emu.memory.write(0x90, st)
    else:
        emu.cpu.state.a = int(raw) & 0xFF

    _iec_ok(emu)
    emu._kernal_hook_rts_return()
    return True


def _hook_ciout(emu: "C64", bus) -> bool:
    if (emu.memory.read(DFLTO) & 0xFF) < 8 or (emu.memory.read(DFLTO) & 0xFF) > 11:
        return False
    device = emu.memory.read(DFLTO) & 0xFF
    if _tcp_client(emu, device) is None:
        return False

    ch = emu.cpu.state.a & 0xFF
    if not bus.send_byte(ch, eoi=False):
        _iec_error(emu, 5)
        emu._kernal_hook_rts_return()
        return True

    _iec_ok(emu)
    emu._kernal_hook_rts_return()
    return True


def kernal_tcp_iec_stop_pcs() -> tuple[int, ...]:
    """PC values where the Rust fast batch must yield for TCP IEC hooks (sorted)."""
    return (
        0xFFC0,
        0xFFC3,
        0xFFC6,
        0xFFC9,
        0xFFCC,
        0xFFCF,
        0xFFD2,
    )
