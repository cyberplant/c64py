"""
Host-side video presenter (not the VIC-II).

This module is the boundary between **emulation core** (CPU, RAM, VIC registers,
cycle engine) and **host output**. The core maintains faithful state; the
presenter samples that state at a bounded host rate and builds pixels for SDL/pygame.

Design goals:

- **Decoupled evolution:** Today we composite a full frame from RAM + latched VIC
  registers (see ``MemoryMap.snapshot_vic_render_state``). Later, the same API can
  consume a scanline buffer or multi-band compositor fed by the cycle engine without
  rewriting the CPU loop.
- **Bounded host cost:** Prefer packing RGB into a single buffer and one upload to
  a ``pygame.Surface`` instead of per-pixel ``Surface.set_at`` calls from Python.

Raster-accurate split screens (different VIC modes within one host frame) are **not**
implemented yet; they belong in a future presenter backend that reads per-line or
per-region data from the core.
"""

from __future__ import annotations

from typing import Tuple


class RgbFrameBuffer:
    """Packed RGB888 framebuffer, row-major (width * height * 3 bytes)."""

    __slots__ = ("width", "height", "_buf")

    def __init__(self, width: int, height: int) -> None:
        self.width = max(0, int(width))
        self.height = max(0, int(height))
        self._buf = bytearray(self.width * self.height * 3)

    @property
    def buf(self) -> bytearray:
        return self._buf

    def fill(self, rgb: Tuple[int, int, int]) -> None:
        """Fill the entire buffer with *rgb*."""
        w, h = self.width, self.height
        if w <= 0 or h <= 0:
            return
        r, g, b = rgb
        line = bytes((r, g, b) * w)
        buf = self._buf
        stride = w * 3
        for y in range(h):
            buf[y * stride : (y + 1) * stride] = line

    def fill_rect(self, x: int, y: int, rw: int, rh: int, rgb: Tuple[int, int, int]) -> None:
        """Fill axis-aligned rectangle; clipped to the framebuffer."""
        if rw <= 0 or rh <= 0:
            return
        x0 = max(0, x)
        y0 = max(0, y)
        x1 = min(self.width, x + rw)
        y1 = min(self.height, y + rh)
        if x0 >= x1 or y0 >= y1:
            return
        r, g, b = rgb
        buf = self._buf
        w = self.width
        # Hot path: double-wide pixels (multicolor bitmap / MCM text / MC sprites).
        # Avoid per-call bytes() allocation (tens of thousands/frame in swinth-like demos).
        if rw == 2 and rh == 1 and x1 - x0 == 2 and y1 - y0 == 1:
            i = (y0 * w + x0) * 3
            buf[i] = r
            buf[i + 1] = g
            buf[i + 2] = b
            buf[i + 3] = r
            buf[i + 4] = g
            buf[i + 5] = b
            return
        row_len = (x1 - x0) * 3
        line = bytes((r, g, b) * (x1 - x0))
        for yy in range(y0, y1):
            offset = (yy * w + x0) * 3
            buf[offset : offset + row_len] = line

    def put_pixel(self, x: int, y: int, rgb: Tuple[int, int, int]) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        r, g, b = rgb
        i = (y * self.width + x) * 3
        buf = self._buf
        buf[i] = r
        buf[i + 1] = g
        buf[i + 2] = b

    def plot_hires_glyph(
        self,
        x: int,
        y: int,
        rows: bytes,
        fg: Tuple[int, int, int],
    ) -> None:
        """Draw 8×8 mono glyph; bits 1 use *fg*, bits 0 leave existing pixels."""
        fr, fg_g, fb = fg
        buf = self._buf
        w = self.width
        h = self.height
        for yy in range(8):
            row_b = rows[yy]
            py = y + yy
            if not (0 <= py < h):
                continue
            base_row = py * w * 3
            for xx in range(8):
                if not (row_b & (1 << (7 - xx))):
                    continue
                px = x + xx
                if not (0 <= px < w):
                    continue
                i = base_row + px * 3
                buf[i] = fr
                buf[i + 1] = fg_g
                buf[i + 2] = fb
