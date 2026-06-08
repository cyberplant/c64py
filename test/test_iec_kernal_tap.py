"""Phase 0: KERNAL CIA2 → IEC line tap (see ``iec_kernal_bridge``)."""

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
