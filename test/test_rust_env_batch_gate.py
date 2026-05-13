"""Env vars that gate Rust ``run_fast_batch`` (see docs/DEBUGGING.md)."""

from __future__ import annotations

import os
import sys

import pytest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from c64py import _core  # noqa: E402
from c64py.cpu import CPU6502  # noqa: E402
from c64py.iec_bus import IECBus  # noqa: E402
from c64py.iec_kernal_bridge import KernalIecTap  # noqa: E402
from c64py.memory import MemoryMap  # noqa: E402

_RUST_SKIP = not _core.is_available


@pytest.mark.skipif(_RUST_SKIP, reason="c64py_rust_core not built for this interpreter")
def test_c64py_use_rust_fast_zero_disables_rust_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("C64PY_USE_RUST_FAST", "0")
    mem = MemoryMap()
    cpu = CPU6502(mem, accurate_vic=False, rust_hybrid_vic=False)
    assert cpu._rust_fast_batch_usable() is False


@pytest.mark.skipif(_RUST_SKIP, reason="c64py_rust_core not built for this interpreter")
def test_c64py_rust_hybrid_vic_zero_disables_rust_batch_for_accurate_rust(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("C64PY_RUST_HYBRID_VIC", "0")
    mem = MemoryMap()
    cpu = CPU6502(mem, accurate_vic=True, rust_hybrid_vic=True)
    assert cpu._rust_hybrid_vic_effective() is False
    assert cpu._rust_fast_batch_usable() is False


@pytest.mark.skipif(_RUST_SKIP, reason="c64py_rust_core not built for this interpreter")
def test_iec_wire_decode_tap_still_allows_rust_fast_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """IEC wire decode relies on Rust CIA2 replay in ``_core.run_fast_batch``, not on disabling Rust."""
    monkeypatch.delenv("C64PY_USE_RUST_FAST", raising=False)
    mem = MemoryMap()
    mem.ram = bytearray(mem.ram)
    mem.iec_bus = IECBus()
    mem.iec_kernal_tap = KernalIecTap(wire_decode_bus=mem.iec_bus)
    cpu = CPU6502(mem, accurate_vic=False, rust_hybrid_vic=False)
    assert cpu._rust_fast_batch_usable() is True


@pytest.mark.skipif(_RUST_SKIP, reason="c64py_rust_core not built for this interpreter")
def test_iec_tap_without_wire_decode_still_allows_rust_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("C64PY_USE_RUST_FAST", raising=False)
    mem = MemoryMap()
    mem.ram = bytearray(mem.ram)
    mem.iec_kernal_tap = KernalIecTap()
    cpu = CPU6502(mem, accurate_vic=False, rust_hybrid_vic=False)
    assert cpu._rust_fast_batch_usable() is True


def test_c64py_rust_hybrid_vic_respects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Effective hybrid flag does not require the Rust extension."""
    monkeypatch.setenv("C64PY_RUST_HYBRID_VIC", "0")
    mem = MemoryMap()
    cpu = CPU6502(mem, accurate_vic=True, rust_hybrid_vic=True)
    assert cpu._rust_hybrid_vic_effective() is False
    monkeypatch.delenv("C64PY_RUST_HYBRID_VIC", raising=False)
    assert cpu._rust_hybrid_vic_effective() is True
