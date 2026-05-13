"""Per-cycle VIC sampler (B2): visible-window capture after each VIC tick."""

from __future__ import annotations

from c64py.memory import MemoryMap
from c64py.video_beam import per_cycle_geometry


def test_per_cycle_capture_noop_when_disabled() -> None:
    mem = MemoryMap()
    mem.video_standard = "pal"
    mem.per_cycle_render_enabled = False
    mem.raster_line = 51
    mem.raster_cycles = 14
    mem.poke_vic(0x11, 0x1B)

    mem.per_cycle_capture_vic_sample()

    assert mem.per_cycle_vic_flat is None


def test_per_cycle_capture_writes_visible_slots() -> None:
    mem = MemoryMap()
    mem.video_standard = "pal"
    mem.per_cycle_render_enabled = True
    mem.ensure_per_cycle_buffers()
    geom = per_cycle_geometry("pal")

    mem.poke_vic(0x11, 0x1B)
    mem.poke_vic(0x18, 0x32)
    mem.cia2_pra = 0xAA

    for cy in range(geom.content_first_cycle, geom.content_first_cycle + geom.content_cycles):
        mem.raster_line = geom.content_first_raster
        mem.raster_cycles = cy
        mem.per_cycle_capture_vic_sample()

    flat = mem.per_cycle_vic_flat
    cia = mem.per_cycle_cia2_flat
    assert flat is not None and cia is not None
    for cy in range(geom.content_cycles):
        o = cy * 64
        snap = flat[o : o + 64]
        assert snap[0x11] == 0x1B
        assert snap[0x18] == 0x32
        assert cia[cy] == 0xAA


def test_per_cycle_capture_mid_line_d011_diff() -> None:
    """Two cycles on the same raster line see different $D011 shadow bytes."""
    mem = MemoryMap()
    mem.video_standard = "pal"
    mem.per_cycle_render_enabled = True
    mem.ensure_per_cycle_buffers()

    line = 120
    mem.raster_line = line
    mem.raster_cycles = 14
    mem.poke_vic(0x11, 0x1B)
    mem.per_cycle_capture_vic_sample()

    mem.raster_line = line
    mem.raster_cycles = 15
    mem.poke_vic(0x11, 0x5B)
    mem.per_cycle_capture_vic_sample()

    geom = per_cycle_geometry("pal")
    i0 = geom.sample_index(line, 14)
    i1 = geom.sample_index(line, 15)
    assert i0 is not None and i1 is not None
    flat = mem.per_cycle_vic_flat
    assert flat is not None
    s0 = flat[i0 * 64 : (i0 + 1) * 64]
    s1 = flat[i1 * 64 : (i1 + 1) * 64]
    assert s0[0x11] == 0x1B
    assert s1[0x11] == 0x5B
    assert s0 != s1
