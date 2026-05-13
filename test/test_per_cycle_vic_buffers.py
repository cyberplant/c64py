from c64py.memory import MemoryMap
from c64py.video_beam import per_cycle_geometry


def test_per_cycle_geometry_pal_visible_window() -> None:
    geom = per_cycle_geometry("pal")
    assert geom.content_first_raster == 51
    assert geom.content_height == 200
    assert geom.content_first_cycle == 14
    assert geom.content_cycles == 40
    assert geom.visible_sample_count == 8_000
    assert geom.sample_index(50, 20) is None
    assert geom.sample_index(51, 13) is None
    assert geom.sample_index(51, 14) == 0
    assert geom.sample_index(51, 53) == 39
    assert geom.sample_index(52, 14) == 40
    assert geom.sample_index(250, 53) == 7999
    assert geom.sample_index(251, 14) is None


def test_per_cycle_geometry_ntsc_frame_budget() -> None:
    geom = per_cycle_geometry("ntsc")
    assert geom.raster_lines == 263
    assert geom.cycles_per_line == 65
    assert geom.visible_sample_count == 8_000


def test_ensure_per_cycle_buffers_allocates_visible_sample_grid() -> None:
    mem = MemoryMap()
    mem.video_standard = "pal"
    mem.ensure_per_cycle_buffers()
    assert mem.per_cycle_vic_flat is not None
    assert mem.per_cycle_cia2_flat is not None
    assert len(mem.per_cycle_vic_flat) == 8_000 * 64
    assert len(mem.per_cycle_cia2_flat) == 8_000
    assert mem.per_cycle_snapshots_primed is False

    old_vic_flat = mem.per_cycle_vic_flat
    mem.ensure_per_cycle_buffers()
    assert mem.per_cycle_vic_flat is old_vic_flat

    mem.video_standard = "ntsc"
    mem.ensure_per_cycle_buffers()
    assert len(mem.per_cycle_vic_flat) == 8_000 * 64
    assert per_cycle_geometry("ntsc").raster_lines == 263


def test_prime_per_cycle_snapshots_from_current_vic() -> None:
    mem = MemoryMap()
    mem.video_standard = "pal"
    mem.per_cycle_render_enabled = True
    mem.cia2_pra = 0x7F
    mem.prime_per_cycle_snapshots_from_current_vic()

    assert mem.per_cycle_vic_flat is not None
    assert mem.per_cycle_cia2_flat is not None
    assert mem.per_cycle_snapshots_primed is True

    flat = mem.per_cycle_vic_flat
    first = flat[0:64]
    last = flat[-64:]
    assert first == last
    assert mem.per_cycle_cia2_flat[0] == 0x7F
    assert mem.per_cycle_cia2_flat[-1] == 0x7F


def test_prime_per_cycle_snapshots_is_gated() -> None:
    mem = MemoryMap()
    mem.video_standard = "pal"
    mem.per_cycle_render_enabled = False
    mem.prime_per_cycle_snapshots_from_current_vic()

    assert mem.per_cycle_vic_flat is None
    assert mem.per_cycle_snapshots_primed is False


def test_vic_fg_opaque_helpers_match_compositor_rules() -> None:
    from c64py.graphics import PygameInterface

    assert PygameInterface._vic_fg_opaque_hires_row(0xAA) == (
        True,
        False,
        True,
        False,
        True,
        False,
        True,
        False,
    )
    t = PygameInterface._vic_fg_opaque_mcm_row(0xC0)
    assert t[0] and t[1]
    assert not t[2] and not t[3]
