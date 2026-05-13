"""Tests for :class:`c64py.presenter.RgbFrameBuffer` hot paths."""

from c64py.presenter import RgbFrameBuffer


def test_fill_rect_1x1_writes_packed_rgb() -> None:
    fb = RgbFrameBuffer(4, 3)
    fb.fill_rect(1, 2, 1, 1, (10, 20, 30))
    i = (2 * 4 + 1) * 3
    assert fb.buf[i : i + 3] == bytearray((10, 20, 30))
