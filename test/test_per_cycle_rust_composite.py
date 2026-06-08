"""Smoke test for optional Rust per-cycle compositor (``composite_per_cycle_frame``)."""

from __future__ import annotations

import pytest

from c64py import _core
from c64py.memory import MemoryMap

pytestmark = pytest.mark.skipif(
    not _core.is_available,
    reason="c64py_rust_core not built / importable",
)


def test_composite_per_cycle_frame_smoke() -> None:
    mem = MemoryMap(video_standard="pal")
    mem.per_cycle_render_enabled = True
    mem.ensure_per_cycle_buffers()
    mem.prime_per_cycle_snapshots_from_current_vic()
    pal48 = bytes(48)
    nw, nh = 40, 30
    out = bytearray(nw * nh * 3)
    _core.composite_per_cycle_frame(
        mem,
        pal48,
        out,
        video_standard="pal",
        native_width=nw,
        native_height=nh,
        screen_left=0,
        screen_top=0,
        screen_width=nw,
        screen_height=nh,
        border_px=0,
        skip_sprites=True,
    )
    assert len(out) == nw * nh * 3
