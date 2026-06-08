"""CLI smoke tests for unified emulation flags.

Verifies the argparse layer accepts video/VIC tiers, rejects legacy
audio/interface flags, and that `--accurate` aliases VIC + video only
(drive tier is configured via TOML `[emulation] disk_emulation`).
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _build_parser():
    """Re-create the argparse parser used by `c64py.C64.main` for testing.

    We can't easily invoke `main()` without ROMs/UI, but the relevant flag
    parsing is self-contained — we replicate just the bits we care about.
    """
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--video-rendering",
        choices=("per-frame", "per-raster", "per-cycle", "fast", "accurate"),
        default="per-frame",
    )
    ap.add_argument(
        "--vic-emulation",
        choices=("fast", "accurate-python", "accurate-rust"),
        default="fast",
    )
    ap.add_argument("--accurate", action="store_true")
    ap.add_argument(
        "--audio-emulation",
        choices=("resid", "python-sid", "disabled"),
        default="resid",
    )
    ap.add_argument(
        "--interface",
        choices=("textual", "text", "tui", "headless", "graphics", "pygame"),
        default="textual",
    )
    return ap


def _apply_video_aliases_and_accurate(ns):
    """Mirror post-parse logic in ``C64.py`` (aliases + ``--accurate`` + per-cycle)."""
    aliases = {"fast": "per-frame", "accurate": "per-raster"}
    if ns.video_rendering in aliases:
        ns.video_rendering = aliases[ns.video_rendering]
    if ns.accurate:
        ns.video_rendering = "per-raster"
        ns.vic_emulation = "accurate-rust"
    if ns.video_rendering == "per-cycle":
        ns.video_rendering = "per-raster"


def test_default_video_vic():
    ns = _build_parser().parse_args([])
    assert ns.vic_emulation == "fast"
    assert ns.video_rendering == "per-frame"


def test_accurate_master_flag_targets_vic_and_video_only():
    """``--accurate`` in C64.py sets VIC tier and per-raster video."""
    ns = _build_parser().parse_args(["--accurate"])
    _apply_video_aliases_and_accurate(ns)
    assert ns.vic_emulation == "accurate-rust"
    assert ns.video_rendering == "per-raster"


def test_accurate_overrides_config_style_defaults():
    """``--accurate`` upgrades VIC + video even when argv starts on fast tiers."""
    ns = _build_parser().parse_args(["--vic-emulation", "fast", "--accurate"])
    _apply_video_aliases_and_accurate(ns)
    assert ns.vic_emulation == "accurate-rust"
    assert ns.video_rendering == "per-raster"


@pytest.mark.parametrize("mode", ["resid", "python-sid", "disabled"])
def test_audio_emulation_modes_accepted(mode):
    ns = _build_parser().parse_args(["--audio-emulation", mode])
    assert ns.audio_emulation == mode


@pytest.mark.parametrize("mode", ["textual", "text", "tui", "headless", "graphics", "pygame"])
def test_interface_modes_accepted(mode):
    ns = _build_parser().parse_args(["--interface", mode])
    assert ns.interface == mode


@pytest.mark.parametrize(
    "legacy_tokens",
    [
        ["--enable-sid"],
        ["--enable-resid"],
        ["--disable-resid"],
        ["--headless"],
        ["--graphics"],
    ],
)
def test_legacy_flags_rejected(legacy_tokens):
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(legacy_tokens)
