"""Snapshot save/load round-trip and resume-parity tests."""

from __future__ import annotations

import os
import sys

import pytest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from c64py.cpu import CPU6502  # noqa: E402
from c64py.memory import MemoryMap  # noqa: E402
from c64py.snapshot import (  # noqa: E402
    SNAPSHOT_MAGIC,
    SNAPSHOT_VERSION,
    SnapshotError,
    apply_payload,
    build_payload,
    load_snapshot,
    save_snapshot,
)


class _FakeEmu:
    """Minimal duck-typed Emulator: CPU + memory + cycles counter.

    Snapshot code only reads ``memory``, ``cpu``, ``current_cycles``,
    ``vic_emulation`` and optionally ``interface``. Using the real Emulator
    pulls in Textual/pygame; a minimal stand-in keeps the unit test fast and
    isolated from UI side effects.
    """

    vic_emulation = "fast"
    interface = None

    def __init__(self) -> None:
        self.memory = MemoryMap()
        self.cpu = CPU6502(self.memory, interface=None, accurate_vic=False)
        self.current_cycles = 0


def _install_tight_loop(mem: MemoryMap, base: int = 0x0800) -> None:
    # LDX #10 / DEX / BNE -3 / LDA #$42 / NOP
    prog = [0xA2, 0x0A, 0xCA, 0xD0, 0xFD, 0xA9, 0x42, 0xEA]
    for i, b in enumerate(prog):
        mem.ram[base + i] = b


def _run_n_steps(emu: _FakeEmu, n: int) -> None:
    for _ in range(n):
        step_cycles = emu.cpu.step()
        emu.current_cycles += step_cycles


def test_build_payload_has_expected_shape() -> None:
    emu = _FakeEmu()
    _install_tight_loop(emu.memory)
    emu.cpu.state.pc = 0x0800
    emu.cpu.state.sp = 0xFD

    payload = build_payload(emu, note="unit")

    assert payload["magic"] == SNAPSHOT_MAGIC
    assert payload["version"] == SNAPSHOT_VERSION
    assert payload["note"] == "unit"
    assert payload["cpu"]["pc"] == 0x0800
    assert payload["cpu"]["sp"] == 0xFD
    ram = payload["memory"]["ram"]
    assert isinstance(ram, (bytes, bytearray)) and len(ram) == 0x10000
    vic = payload["memory"]["vic_regs"]
    assert isinstance(vic, (bytes, bytearray)) and len(vic) == 0x40
    assert "raster_line" in payload["memory"]
    assert "cia1_timer_a" in payload["memory"]
    assert set(payload["vic_engine"]).issuperset({
        "raster_line", "cycles_per_line", "num_raster_lines"
    })


def test_apply_payload_restores_cpu_and_ram() -> None:
    src = _FakeEmu()
    _install_tight_loop(src.memory)
    src.cpu.state.pc = 0x0800
    src.cpu.state.a = 0x11
    src.cpu.state.x = 0x22
    src.cpu.state.y = 0x33
    src.cpu.state.sp = 0xFD
    src.memory.ram[0x1234] = 0xAB
    src.memory._vic_regs[0x11] = 0x9B
    src.memory.raster_line = 123
    src.memory.cia1_timer_a.latch = 0x4321
    src.current_cycles = 5000

    payload = build_payload(src)

    dst = _FakeEmu()
    # Make dst clearly different so we can see restore took effect.
    dst.cpu.state.pc = 0xDEAD
    dst.cpu.state.a = 0xFF
    dst.memory.ram[0x1234] = 0x00
    dst.memory.raster_line = 0
    dst.current_cycles = 0

    apply_payload(dst, payload)

    assert dst.cpu.state.pc == 0x0800
    assert dst.cpu.state.a == 0x11
    assert dst.cpu.state.x == 0x22
    assert dst.cpu.state.y == 0x33
    assert dst.cpu.state.sp == 0xFD
    assert dst.memory.ram[0x1234] == 0xAB
    assert dst.memory._vic_regs[0x11] == 0x9B
    assert dst.memory.raster_line == 123
    assert dst.memory.cia1_timer_a.latch == 0x4321
    assert dst.current_cycles == 5000


def test_file_roundtrip(tmp_path) -> None:
    emu = _FakeEmu()
    _install_tight_loop(emu.memory)
    emu.cpu.state.pc = 0x0800
    emu.cpu.state.sp = 0xFD
    emu.memory.ram[0xC000] = 0x42
    _run_n_steps(emu, 10)
    pc_before = emu.cpu.state.pc
    cyc_before = emu.current_cycles

    path = tmp_path / "t.snap"
    save_snapshot(emu, path)
    assert path.exists() and path.stat().st_size > 0

    fresh = _FakeEmu()
    load_snapshot(fresh, path)
    assert fresh.cpu.state.pc == pc_before
    assert fresh.current_cycles == cyc_before
    assert fresh.memory.ram[0xC000] == 0x42


def test_resume_parity_matches_continuous_run(tmp_path) -> None:
    """Snapshot + reload + step must yield the same state as uninterrupted stepping."""
    golden = _FakeEmu()
    _install_tight_loop(golden.memory)
    golden.cpu.state.pc = 0x0800
    golden.cpu.state.sp = 0xFD
    golden.memory.ram[0x0801] = 0x06  # LDX #6 — finite loop so we terminate
    _run_n_steps(golden, 40)

    # Same program, snapshot midway, then resume.
    mid = _FakeEmu()
    _install_tight_loop(mid.memory)
    mid.cpu.state.pc = 0x0800
    mid.cpu.state.sp = 0xFD
    mid.memory.ram[0x0801] = 0x06
    _run_n_steps(mid, 15)

    path = tmp_path / "mid.snap"
    save_snapshot(mid, path)

    resumed = _FakeEmu()
    load_snapshot(resumed, path)
    _run_n_steps(resumed, 40 - 15)

    assert resumed.cpu.state.pc == golden.cpu.state.pc
    assert resumed.cpu.state.a == golden.cpu.state.a
    assert resumed.cpu.state.x == golden.cpu.state.x
    assert resumed.cpu.state.y == golden.cpu.state.y
    assert resumed.cpu.state.sp == golden.cpu.state.sp
    assert resumed.cpu.state.p == golden.cpu.state.p
    assert resumed.current_cycles == golden.current_cycles
    assert bytes(resumed.memory.ram) == bytes(golden.memory.ram)


def test_load_rejects_bad_magic(tmp_path) -> None:
    import pickle
    bad = tmp_path / "bad.snap"
    with open(bad, "wb") as f:
        pickle.dump({"magic": "NOT-SNAP", "version": 1}, f)
    emu = _FakeEmu()
    with pytest.raises(SnapshotError):
        load_snapshot(emu, bad)


def test_load_rejects_bad_version(tmp_path) -> None:
    import pickle
    bad = tmp_path / "bad.snap"
    with open(bad, "wb") as f:
        pickle.dump(
            {"magic": SNAPSHOT_MAGIC, "version": 9999, "cpu": {}, "memory": {}},
            f,
        )
    emu = _FakeEmu()
    with pytest.raises(SnapshotError):
        load_snapshot(emu, bad)


def test_ram_bytearray_identity_preserved_on_apply(tmp_path) -> None:
    """Rust fast-batch pins a reference to memory.ram; restoring must not rebind."""
    emu = _FakeEmu()
    _install_tight_loop(emu.memory)
    ram_id_before = id(emu.memory.ram)
    vic_id_before = id(emu.memory._vic_regs)

    payload = build_payload(emu)
    # Mutate source so apply_payload clearly writes.
    emu.memory.ram[0x0800] = 0xFF
    emu.memory._vic_regs[0x11] = 0xFF

    apply_payload(emu, payload)

    assert id(emu.memory.ram) == ram_id_before
    assert id(emu.memory._vic_regs) == vic_id_before
    assert emu.memory.ram[0x0800] == 0xA2  # first byte of LDX #..., restored
