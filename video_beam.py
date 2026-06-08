"""
Beam-oriented video helpers (per-raster-line sampling).

PAL first visible text/bitmap row is roughly raster 51; content is 200 lines tall.
These constants map host framebuffer rows to VIC raster lines for per-line snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Approximate PAL visible window inside the full 312-line frame.
PAL_RASTER_LINES = 312
PAL_CYCLES_PER_LINE = 63
PAL_CONTENT_FIRST_RASTER = 51
PAL_CONTENT_HEIGHT = 200

# NTSC reference (263 lines total).
NTSC_RASTER_LINES = 263
NTSC_CYCLES_PER_LINE = 65
NTSC_CONTENT_FIRST_RASTER = 51
NTSC_CONTENT_HEIGHT = 200

# The VIC-II cycle engine uses 0-based raster cycles. Chip cycles 15..54 are
# the 40 visible character fetch/display cycles for the 320-pixel content area.
CONTENT_FIRST_RASTER_CYCLE = 14
CONTENT_CYCLES = 40


@dataclass(frozen=True)
class VicPerCycleGeometry:
    """Per-cycle sampler dimensions for one video standard."""

    raster_lines: int
    cycles_per_line: int
    content_first_raster: int
    content_height: int
    content_first_cycle: int
    content_cycles: int

    @property
    def frame_cycles(self) -> int:
        return self.raster_lines * self.cycles_per_line

    @property
    def visible_sample_count(self) -> int:
        return self.content_height * self.content_cycles

    def sample_index(self, raster_line: int, raster_cycle: int) -> Optional[int]:
        """Return visible-window sample index for a raster/cycle, or ``None`` outside it."""
        y = int(raster_line) - self.content_first_raster
        x = int(raster_cycle) - self.content_first_cycle
        if 0 <= y < self.content_height and 0 <= x < self.content_cycles:
            return y * self.content_cycles + x
        return None


PAL_PER_CYCLE_GEOMETRY = VicPerCycleGeometry(
    raster_lines=PAL_RASTER_LINES,
    cycles_per_line=PAL_CYCLES_PER_LINE,
    content_first_raster=PAL_CONTENT_FIRST_RASTER,
    content_height=PAL_CONTENT_HEIGHT,
    content_first_cycle=CONTENT_FIRST_RASTER_CYCLE,
    content_cycles=CONTENT_CYCLES,
)
NTSC_PER_CYCLE_GEOMETRY = VicPerCycleGeometry(
    raster_lines=NTSC_RASTER_LINES,
    cycles_per_line=NTSC_CYCLES_PER_LINE,
    content_first_raster=NTSC_CONTENT_FIRST_RASTER,
    content_height=NTSC_CONTENT_HEIGHT,
    content_first_cycle=CONTENT_FIRST_RASTER_CYCLE,
    content_cycles=CONTENT_CYCLES,
)


def _normalize_video_standard(video_standard: str) -> str:
    v = (video_standard or "pal").strip().lower()
    return "ntsc" if v == "ntsc" else "pal"


def per_cycle_geometry(video_standard: str) -> VicPerCycleGeometry:
    """Return per-cycle sampler dimensions for ``video_standard``."""
    return (
        NTSC_PER_CYCLE_GEOMETRY
        if _normalize_video_standard(video_standard) == "ntsc"
        else PAL_PER_CYCLE_GEOMETRY
    )


def content_row_to_raster_line(row: int, video_standard: str) -> int:
    """Map a row index 0..199 inside the 320x200 content area to a raster line index."""
    if _normalize_video_standard(video_standard) == "ntsc":
        base = NTSC_CONTENT_FIRST_RASTER
        h = NTSC_CONTENT_HEIGHT
    else:
        base = PAL_CONTENT_FIRST_RASTER
        h = PAL_CONTENT_HEIGHT
    row = max(0, min(int(row), h - 1))
    return base + row
