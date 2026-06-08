"""Phase 0: KERNAL CIA2 → IEC line tap (see ``iec_kernal_bridge``)."""

import json
from pathlib import Path

from c64py.iec_bus import IECBus
from c64py.iec_kernal_bridge import KernalIecTap
from c64py.memory import MemoryMap


def test_kernal_iec_tap_counts_line_changes_only():
    mem = MemoryMap()
    mem.iec_bus = IECBus()
    mem.iec_kernal_tap = KernalIecTap()
    mem.cia2_pra = 0xFF
    mem.apply_cia2_port_a_to_iec_bus()
    mem.apply_cia2_port_a_to_iec_bus()
    assert mem.iec_kernal_tap.transition_count == 0

    # Clear CIA2 PRA bit 3 (ATN out): bus ATN line changes vs 0xFF.
    mem.cia2_pra = 0xF7
    mem.apply_cia2_port_a_to_iec_bus()
    assert mem.iec_kernal_tap.transition_count == 1
    ev = mem.iec_kernal_tap.recent_events()
    assert len(ev) == 1
    _cyc, atn, clk, data = ev[0]
    assert atn is True and clk is False and data is False  # from 0xF7 wiring


def test_kernal_iec_tap_writes_jsonl_from_env(monkeypatch, tmp_path):
    out_path = tmp_path / "tap.jsonl"
    monkeypatch.setenv("C64PY_IEC_TAP_JSONL", str(out_path))

    mem = MemoryMap()
    mem.iec_bus = IECBus()
    mem.iec_kernal_tap = KernalIecTap()

    for cycle, cia2_pra in (
        (100, 0xFF),  # Baseline only; the tap does not emit an initial state.
        (120, 0xF7),
        (130, 0xF7),  # Duplicate resolved line state; no transition.
        (150, 0xE7),
        (170, 0xC7),
    ):
        mem.debug_last_cycles = cycle
        mem.cia2_pra = cia2_pra
        mem.apply_cia2_port_a_to_iec_bus()

    tap = mem.iec_kernal_tap
    tap.flush()

    assert tap.transition_count == 3
    assert tap.recent_events() == [
        (120, True, False, False),
        (150, True, True, False),
        (170, True, True, True),
    ]

    expected_path = Path(__file__).parent / "fixtures" / "iec_tap_synthetic_fragment.jsonl"
    assert out_path.read_text(encoding="utf-8") == expected_path.read_text(encoding="utf-8")
    assert [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()] == [
        {"cyc": 120, "atn": True, "clk": False, "data": False},
        {"cyc": 150, "atn": True, "clk": True, "data": False},
        {"cyc": 170, "atn": True, "clk": True, "data": True},
    ]
    tap.close()


def test_kernal_tap_records_drive_clk_when_line_receiver_attached():
    mem = MemoryMap()
    bus = IECBus()
    mem.iec_bus = bus
    tap = KernalIecTap()
    mem.iec_kernal_tap = tap
    tap.attach_line_receiver(bus)

    class _D8:
        device_number = 8
        iec_bus = None

        def on_atn_changed(self, _s: bool) -> None:
            pass

        def notify_bus_change(self) -> None:
            pass

    bus.attach_device(_D8())
    mem.cia2_pra = 0xC7
    mem.apply_cia2_port_a_to_iec_bus()
    mem.apply_cia2_port_a_to_iec_bus()
    assert tap.transition_count == 0

    bus.set_clk("drive8", False)
    assert tap.transition_count == 1
    ev = tap.recent_events()[-1]
    assert ev[1] is True and ev[2] is False  # ATN released, CLK asserted (low)

    mem.debug_last_cycles = 5000
    bus.set_clk("drive8", True)
    assert tap.transition_count == 2
    assert tap.recent_events()[-1][0] == 5000
