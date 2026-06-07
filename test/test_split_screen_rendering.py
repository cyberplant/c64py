"""Per-raster renderer regression: split-screen is composed row-by-row.

Synthesizes a small beam capture where the top half and bottom half of the
screen have different ``$D018`` (charset address) bytes and checks that
:meth:`PygameInterface._render_frame_beam` reaches both charsets via the
per-row dispatch. This is the classic bug pattern of mid-frame ``$D018``
swaps used to source charsets from different addresses for the top/bottom
halves of the screen.

We stub the pixel-plot helpers to just record the charset address used for
each row; the point of the test is the dispatch, not the pixel math.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

# SDL's macOS Cocoa backend is AppKit-unsafe when pygame is initialized inside
# pytest; force the offscreen "dummy" driver before pygame is imported anywhere.
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pytest

from c64py.emulator import C64
from c64py.graphics import PygameInterface
from c64py.video_beam import content_row_to_raster_line


def _vic_regs_with_d018(d018: int) -> bytes:
    """64-byte VIC shadow with most bytes 0 and ``$D018`` set.

    ``$D011`` is left at 0x1B (DEN on, 25 rows, YSCROLL=3) so the row maps to
    raster lines cleanly. ``$D020``/``$D021`` default to non-zero to keep the
    renderer's "all-zero" sentinel from kicking in.
    """
    regs = bytearray(64)
    regs[0x11] = 0x1B
    regs[0x16] = 0x08
    regs[0x18] = d018 & 0xFF
    regs[0x20] = 0x06
    regs[0x21] = 0x06
    return bytes(regs)


def _make_ui() -> PygameInterface:
    emu = C64(vic_emulation="accurate-rust")
    ui = PygameInterface(emu, scale=1, fps=30, border_size=0)
    import pygame

    pygame.init()
    pygame.display.set_mode((320, 200))
    ui._pygame = pygame
    ui._native_size = (320, 200)
    ui._display_size = (320, 200)
    ui._display_surface = pygame.display.get_surface()
    ui._frame_surface = pygame.Surface((320, 200)).convert()
    from c64py.graphics import RgbFrameBuffer

    ui._rgb_frame = RgbFrameBuffer(320, 200)
    ui._screen_rect = pygame.Rect(0, 0, 320, 200)
    return ui


def test_beam_renderer_dispatches_per_row_charset() -> None:
    """Two-band frame: rows 0-12 use ``$D018=$32``; rows 13-24 use ``$D018=$14``.

    We capture the sequence of ``char_base`` values the per-row helper sees;
    the assertion is that both values show up in the right rows. With the
    legacy renderer the whole frame used the last-sampled ``$D018`` and this
    invariant failed for the bottom half.
    """
    ui = _make_ui()
    mem = ui.emulator.memory
    mem.beam_render_enabled = True
    mem.ensure_beam_buffers()

    # Build per-raster samples for the full 312-line PAL frame.
    vs = mem.video_standard
    split_content_row = 12  # rows 0..12 use first config; rows 13..24 use second
    split_raster = content_row_to_raster_line(split_content_row * 8 + 8, vs)
    regs_top = _vic_regs_with_d018(0x32)  # char_base at $0800
    regs_bot = _vic_regs_with_d018(0x14)  # char_base at $1000
    n = len(mem.beam_vic_lines or [])
    assert n > 0
    for rl in range(n):
        sample = regs_bot if rl >= split_raster else regs_top
        mem.beam_vic_lines[rl] = sample
        o = rl * 64
        mem.beam_vic_flat[o : o + 64] = sample
        mem.beam_cia2_lines[rl] = 0x00
        mem.beam_cia2_flat[rl] = 0x00
    mem.beam_snapshots_primed = True

    seen: list[int] = []

    def _record_row(
        row, y, vic_bank, screen_base, char_base, mode_info, bg_colors, bg_fill_color=None
    ):
        seen.append(char_base)

    ui._render_row_text = _record_row  # type: ignore[assignment]
    ui._render_row_bitmap = MagicMock()
    ui._render_sprites = MagicMock()

    ui._render_frame_beam()

    assert len(seen) == 25, f"expected 25 rows dispatched, got {len(seen)}"
    # Rows 0..12 (13 rows) should have seen char_base = 0x0800 (from $D018=$32).
    # Rows 13..24 (12 rows) should have seen char_base = 0x1000 (from $D018=$14).
    # Allow one-row slack around the boundary since content_row_to_raster_line
    # rounds differently than the band boundary (first-raster-of-row vs
    # last-raster-of-row).
    assert seen[0] == 0x0800
    assert seen[-1] == 0x1000
    assert 0x0800 in seen and 0x1000 in seen, f"both charsets must appear in {seen}"
    # The switch should happen near ``split_content_row``.
    switch_idx = next(
        i for i, v in enumerate(seen) if v != seen[0]
    )
    assert abs(switch_idx - split_content_row) <= 2


def test_beam_renderer_falls_back_to_live_regs_on_all_zero_sample() -> None:
    """When beam sample is all zeros (e.g. pre-prime frame) use live VIC regs."""
    ui = _make_ui()
    mem = ui.emulator.memory
    mem.beam_render_enabled = True
    mem.ensure_beam_buffers()

    # Leave all samples zero (unprimed beam buffer).
    mem.beam_snapshots_primed = True
    # Set live VIC regs to a realistic config.
    mem._vic_regs[0x11] = 0x1B
    mem._vic_regs[0x18] = 0x32
    mem._vic_regs[0x20] = 0x0E
    mem._vic_regs[0x21] = 0x06

    seen: list[int] = []

    def _record_row(
        row, y, vic_bank, screen_base, char_base, mode_info, bg_colors, bg_fill_color=None
    ):
        seen.append(char_base)

    ui._render_row_text = _record_row  # type: ignore[assignment]
    ui._render_row_bitmap = MagicMock()
    ui._render_sprites = MagicMock()

    ui._render_frame_beam()

    assert all(v == 0x0800 for v in seen)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
