"""
Beam-oriented video helpers (per-raster-line sampling).

PAL first visible text/bitmap row is roughly raster 51; content is 200 lines tall.
These constants map host framebuffer rows to VIC raster lines for per-line snapshots.
"""

from __future__ import annotations

# Approximate PAL visible window inside the full 312-line frame.
PAL_CONTENT_FIRST_RASTER = 51
PAL_CONTENT_HEIGHT = 200

# NTSC reference (263 lines total).
NTSC_CONTENT_FIRST_RASTER = 51
NTSC_CONTENT_HEIGHT = 200


def content_row_to_raster_line(row: int, video_standard: str) -> int:
    """Map a row index 0..199 inside the 320x200 content area to a raster line index."""
    if video_standard == "ntsc":
        base = NTSC_CONTENT_FIRST_RASTER
        h = NTSC_CONTENT_HEIGHT
    else:
        base = PAL_CONTENT_FIRST_RASTER
        h = PAL_CONTENT_HEIGHT
    row = max(0, min(int(row), h - 1))
    return base + row
