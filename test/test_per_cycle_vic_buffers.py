"""Per-cycle VIC buffer geometry and allocation foundation."""

from c64py.memory import MemoryMap
from c64py.video_beam import per_cycle_geometry


def test_per_cycle_geometry_pal_visible_window() -> None:
    geom = per_cycle_geometry("pal")

    assert geom.raster_lines == 312
    assert geom.cycles_per_line == 63
    assert geom.frame_cycles == 312 * 63
    assert geom.content_first_raster == 51
    assert geom.content_height == 200
    assert geom.content_first_cycle == 14
    assert geom.content_cycles == 40
    assert geom.visible_sample_count == 8_000
    assert geom.sample_index(51, 14) == 0
    assert geom.sample_index(51, 53) == 39
    assert geom.sample_index(250, 53) == 7_999
    assert geom.sample_index(50, 14) is None
    assert geom.sample_index(51, 13) is None
    assert geom.sample_index(251, 14) is None
    assert geom.sample_index(51, 54) is None


def test_per_cycle_geometry_ntsc_frame_budget() -> None:
    geom = per_cycle_geometry("ntsc")

    assert geom.raster_lines == 263
    assert geom.cycles_per_line == 65
    assert geom.frame_cycles == 263 * 65
    assert geom.visible_sample_count == 8_000


def test_ensure_per_cycle_buffers_allocates_visible_sample_grid() -> None:
    mem = MemoryMap()
    mem.video_standard = "pal"

    mem.ensure_per_cycle_buffers()

    assert mem.per_cycle_vic_samples is not None
    assert mem.per_cycle_cia2_samples is not None
    assert mem.per_cycle_vic_flat is not None
    assert mem.per_cycle_cia2_flat is not None
    assert len(mem.per_cycle_vic_samples) == 8_000
    assert len(mem.per_cycle_cia2_samples) == 8_000
    assert len(mem.per_cycle_vic_flat) == 8_000 * 64
    assert len(mem.per_cycle_cia2_flat) == 8_000
    assert mem.per_cycle_snapshots_primed is False

    old_vic_flat = mem.per_cycle_vic_flat
    mem.ensure_per_cycle_buffers()
    assert mem.per_cycle_vic_flat is old_vic_flat

    mem.video_standard = "ntsc"
    mem.ensure_per_cycle_buffers()
    assert mem.per_cycle_vic_flat is old_vic_flat


def test_prime_per_cycle_snapshots_from_current_vic() -> None:
    mem = MemoryMap()
    mem.per_cycle_render_enabled = True
    mem.poke_vic(0x11, 0x1B)
    mem.poke_vic(0x18, 0x32)
    mem.cia2_pra = 0x7F

    mem.prime_per_cycle_snapshots_from_current_vic()

    assert mem.per_cycle_vic_samples is not None
    assert mem.per_cycle_cia2_samples is not None
    assert mem.per_cycle_vic_flat is not None
    assert mem.per_cycle_cia2_flat is not None
    assert mem.per_cycle_snapshots_primed is True

    first = mem.per_cycle_vic_samples[0]
    last = mem.per_cycle_vic_samples[-1]
    assert first[0x11] == 0x1B
    assert first[0x18] == 0x32
    assert last == first
    assert mem.per_cycle_cia2_samples[0] == 0x7F
    assert mem.per_cycle_cia2_samples[-1] == 0x7F
    assert mem.per_cycle_vic_flat[:64] == first
    assert mem.per_cycle_vic_flat[-64:] == first
    assert mem.per_cycle_cia2_flat[0] == 0x7F
    assert mem.per_cycle_cia2_flat[-1] == 0x7F


def test_prime_per_cycle_snapshots_is_gated() -> None:
    mem = MemoryMap()

    mem.prime_per_cycle_snapshots_from_current_vic()

    assert mem.per_cycle_vic_samples is None
    assert mem.per_cycle_vic_flat is None
    assert mem.per_cycle_snapshots_primed is False
