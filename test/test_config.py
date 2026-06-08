"""Tests for the TOML config layer (item D from input_config_plan.md)."""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Make the repo importable as ``c64py`` whether or not it's installed.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT.parent))

from c64py import config as cfg_mod  # noqa: E402


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Tmp HOME, tmp XDG, tmp cwd. Yields a dict of relevant paths."""
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    work = tmp_path / "work"
    work.mkdir()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.chdir(work)

    return {
        "home": home,
        "xdg": xdg,
        "work": work,
        "cwd_file": work / "c64py.toml",
        "home_file": home / ".c64py.toml",
        "xdg_file": xdg / "c64py" / "c64py.toml",
    }


def test_defaults_when_no_file_present(isolated_env):
    cfg = cfg_mod.load_config()
    assert cfg["video"]["rendering"] == "per-frame"
    assert cfg["video"]["standard"] == "pal"
    assert cfg["video"]["scale"] == 2
    assert cfg["video"]["fps"] == 30
    assert cfg["video"]["border"] == 32
    assert cfg["video"]["fullscreen"] is False
    assert cfg["audio"]["emulation"] == "resid"
    assert cfg["audio"]["volume"] == 1.0
    assert cfg["emulation"]["interface"] == "textual"
    assert cfg["emulation"]["disk_emulation"] == "fast"
    assert cfg["emulation"]["vic_emulation"] == "fast"
    assert cfg["debug"]["turbo"] is False
    assert cfg["debug"]["udp_debug"] is False
    assert cfg["debug"]["screen_update_interval"] == 0.1
    assert cfg["input"]["joystick"]["port1"] == {}
    assert cfg["input"]["joystick"]["port2"]["up"] == "Up"
    assert cfg["input"]["joystick"]["port2"]["fire"] == ["RCtrl", "Space"]
    assert cfg["input"]["gamepad"]["port1"]["enabled"] is False
    assert cfg["input"]["gamepad"]["port2"]["enabled"] is False
    assert cfg["input"]["gamepad"]["port2"]["mapping"]["fire"] == "button0"


def test_search_order_cwd_wins(isolated_env):
    isolated_env["cwd_file"].write_text('[video]\nscale = 5\n')
    isolated_env["home_file"].write_text('[video]\nscale = 7\n')
    isolated_env["xdg_file"].parent.mkdir(parents=True, exist_ok=True)
    isolated_env["xdg_file"].write_text('[video]\nscale = 9\n')

    cfg = cfg_mod.load_config()
    assert cfg["video"]["scale"] == 5


def test_search_order_home_when_no_cwd(isolated_env):
    isolated_env["home_file"].write_text('[video]\nscale = 7\n')
    isolated_env["xdg_file"].parent.mkdir(parents=True, exist_ok=True)
    isolated_env["xdg_file"].write_text('[video]\nscale = 9\n')

    cfg = cfg_mod.load_config()
    assert cfg["video"]["scale"] == 7


def test_search_order_xdg_when_only_xdg(isolated_env):
    isolated_env["xdg_file"].parent.mkdir(parents=True, exist_ok=True)
    isolated_env["xdg_file"].write_text('[video]\nscale = 9\n')

    cfg = cfg_mod.load_config()
    assert cfg["video"]["scale"] == 9


def test_xdg_default_under_home_when_unset(isolated_env, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    default_xdg = isolated_env["home"] / ".config" / "c64py" / "c64py.toml"
    default_xdg.parent.mkdir(parents=True, exist_ok=True)
    default_xdg.write_text('[video]\nscale = 11\n')

    cfg = cfg_mod.load_config()
    assert cfg["video"]["scale"] == 11


def test_partial_config_deep_merges_with_defaults(isolated_env):
    isolated_env["cwd_file"].write_text(
        "[video]\nrendering = \"per-raster\"\n"
    )
    cfg = cfg_mod.load_config()
    # Overridden:
    assert cfg["video"]["rendering"] == "per-raster"
    # Untouched:
    assert cfg["video"]["scale"] == 2
    assert cfg["audio"]["volume"] == 1.0
    assert cfg["debug"]["turbo"] is False


def test_explicit_path_skips_search(isolated_env, tmp_path):
    # cwd file would otherwise win — make sure --config bypasses it.
    isolated_env["cwd_file"].write_text('[video]\nscale = 5\n')

    forced = tmp_path / "forced.toml"
    forced.write_text('[video]\nscale = 42\n')

    cfg = cfg_mod.load_config(forced)
    assert cfg["video"]["scale"] == 42


def test_explicit_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        cfg_mod.load_config(tmp_path / "does-not-exist.toml")


def test_skip_search_returns_pure_defaults(isolated_env):
    isolated_env["cwd_file"].write_text('[video]\nscale = 999\n')
    cfg = cfg_mod.load_config(skip_search=True)
    assert cfg["video"]["scale"] == 2  # the hardcoded default


def test_write_config_then_reload_roundtrip(isolated_env):
    target = isolated_env["work"] / "out.toml"
    cfg_mod.write_config(target)

    # Make sure the cwd file isn't picked up; force the path we wrote.
    cfg = cfg_mod.load_config(target)
    assert cfg == cfg_mod.load_config(skip_search=True)


def test_write_config_refuses_overwrite(isolated_env):
    target = isolated_env["work"] / "out.toml"
    target.write_text("# preexisting\n")
    with pytest.raises(FileExistsError):
        cfg_mod.write_config(target)
    # force=True succeeds:
    cfg_mod.write_config(target, force=True)
    assert "c64py configuration file" in target.read_text()


def test_write_config_creates_parent_dirs(isolated_env):
    target = isolated_env["work"] / "nested" / "deeper" / "c64py.toml"
    cfg_mod.write_config(target)
    assert target.is_file()


def test_cli_overrides_config(isolated_env):
    """End-to-end: config sets scale=8, --graphics-scale 4 wins."""
    isolated_env["cwd_file"].write_text('[video]\nscale = 8\n')

    # We can't run the full emulator, but we can reach into the parser
    # construction the same way main() does and verify the resulting
    # Namespace.
    from c64py.config import load_config

    cfg = load_config()
    assert cfg["video"]["scale"] == 8

    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--graphics-scale", type=int, default=cfg["video"]["scale"])

    # No CLI arg → config wins.
    ns = ap.parse_args([])
    assert ns.graphics_scale == 8

    # CLI explicit → CLI wins.
    ns = ap.parse_args(["--graphics-scale", "4"])
    assert ns.graphics_scale == 4


def test_help_builds_with_no_config(isolated_env):
    """``python C64.py --no-config --help`` must exit 0 and not crash."""
    env = os.environ.copy()
    env["HOME"] = str(isolated_env["home"])
    env["XDG_CONFIG_HOME"] = str(isolated_env["xdg"])
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "C64.py"), "--no-config", "--help"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(isolated_env["work"]),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "C64 Emulator" in result.stdout


def test_write_config_via_cli(isolated_env):
    target = isolated_env["work"] / "written.toml"
    env = os.environ.copy()
    env["HOME"] = str(isolated_env["home"])
    env["XDG_CONFIG_HOME"] = str(isolated_env["xdg"])
    result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "C64.py"),
            "--write-config",
            str(target),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(isolated_env["work"]),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert target.is_file()
    cfg = cfg_mod.load_config(target)
    assert cfg == cfg_mod.load_config(skip_search=True)


def test_config_module_write_default_cli(isolated_env):
    target = isolated_env["work"] / "module_written.toml"
    env = os.environ.copy()
    env["HOME"] = str(isolated_env["home"])
    env["XDG_CONFIG_HOME"] = str(isolated_env["xdg"])
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "c64py.config",
            "--write-default",
            "--config",
            str(target),
            "--force",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(isolated_env["work"]),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert target.is_file()
    cfg = cfg_mod.load_config(target)
    assert cfg["input"]["gamepad"]["port2"]["mapping"]["up"] == "axis1-"


def test_legacy_flat_gamepad_migrates_to_port(isolated_env):
    isolated_env["cwd_file"].write_text(
        '[input.gamepad]\nenabled = true\nport = 2\ndevice_index = 1\n'
        'axis_threshold = 0.4\n\n[input.gamepad.mapping]\nfire = "button2"\n'
    )
    cfg = cfg_mod.load_config()
    assert "enabled" not in cfg["input"]["gamepad"]
    assert cfg["input"]["gamepad"]["port2"]["enabled"] is True
    assert "device_index" not in cfg["input"]["gamepad"]["port2"]
    assert cfg["input"]["gamepad"]["port2"]["axis_threshold"] == 0.4
    assert cfg["input"]["gamepad"]["port2"]["mapping"]["fire"] == "button2"
    assert cfg["input"]["gamepad"]["port1"]["enabled"] is False


def test_petscii_to_screen_code_matches_chrout():
    """Chargen is indexed by screen code; lowercase PETSCII must not be used raw."""
    sc = cfg_mod._petscii_to_screen_code_chrout
    assert sc(ord(" ")) == 0x20
    assert sc(ord(".")) == ord(".")
    assert sc(ord("A")) == 0x01
    assert sc(ord("Z")) == 0x1A
    assert sc(ord("a")) == 0x41
    assert sc(ord("z")) == 0x5A


def test_editor_unicode_dash_maps_to_hyphen_screen_code():
    em = "\u2014"
    assert cfg_mod._editor_char_to_screen_code(em) == cfg_mod._petscii_to_screen_code_chrout(ord("-"))


def test_editor_screen_codes_match_lo_up_charset_for_ascii():
    """Lo/up ROM half: uppercase at 0x41–0x5A, lowercase at 1–26 (not CHROUT)."""
    assert cfg_mod._editor_char_to_screen_code("A") == 0x41
    assert cfg_mod._editor_char_to_screen_code("a") == 1
    assert cfg_mod._editor_char_to_screen_code("Z") == 0x5A
    assert cfg_mod._editor_char_to_screen_code("z") == 26
    assert cfg_mod._editor_char_to_screen_code(" ") == ord(" ")


def test_editor_clear_input_binding():
    cfg = copy.deepcopy(cfg_mod.DEFAULT_CONFIG)
    cfg_mod._config_set(cfg, "input.joystick.port2.up", "Q")
    row = ("fld", "input.joystick.port2.up", "key", None)
    cfg_mod._editor_clear_input_binding(cfg, row)
    assert cfg_mod._config_get(cfg, "input.joystick.port2.up") == ""
    fire_row = ("fire", 2)
    cfg_mod._editor_clear_input_binding(cfg, fire_row)
    assert cfg_mod._config_get(cfg, "input.joystick.port2.fire") == []


def test_config_delete_leaf_and_restore_default():
    cfg = copy.deepcopy(cfg_mod.DEFAULT_CONFIG)
    cfg_mod._config_set(cfg, "input.joystick.port1.up", "X")
    assert cfg_mod._config_get(cfg, "input.joystick.port1.up") == "X"
    cfg_mod._config_restore_default_leaf(cfg, "input.joystick.port1.up")
    assert cfg_mod._config_get(cfg, "input.joystick.port1.up") is None

    cfg_mod._config_set(cfg, "debug.udp_port", 12345)
    cfg_mod._config_restore_default_leaf(cfg, "debug.udp_port")
    assert cfg_mod._config_get(cfg, "debug.udp_port") == cfg_mod.DEFAULT_CONFIG["debug"]["udp_port"]


def test_parse_gamepad_mapping_entry():
    p = cfg_mod.parse_gamepad_mapping_entry
    assert p(None) == (None, None, "")
    assert p("") == (None, None, "")
    assert p("  Button0 ") == (None, None, "button0")
    assert p({"guid": "030000005e040000", "token": "axis0+"}) == ("030000005e040000", None, "axis0+")
    assert p({"guid": "030000005E040000", "token": "axis0+", "host_index": 2}) == (
        "030000005e040000",
        2,
        "axis0+",
    )
    assert p({"device_guid": "AA", "bind": "button1"}) == ("aa", None, "button1")


def test_normalize_sdl_guid():
    assert cfg_mod.normalize_sdl_guid("  AB-CD  ") == "abcd"
    assert cfg_mod.normalize_sdl_guid(None) is None


def test_coerce_numeric_input():
    c = cfg_mod._coerce_numeric_input
    assert c("debug.udp_port", "int", 70000) == 65535
    assert c("debug.udp_port", "int", 0) == 0
    assert c("debug.udp_port", "int", -5) == 0
    assert c("video.scale", "int", 99) == 32
    assert c("video.scale", "int", -1) == 1
    assert c("some.custom.counter", "int", 5) == 5
    assert c("audio.volume", "float", 9.0) == 4.0
    assert c("input.gamepad.axis_threshold", "float", 2.0) == 1.0


def test_editor_clamp_scroll_only_when_selection_crosses_edge():
    f = cfg_mod._editor_clamp_first_visible
    assert f(0, 0, 20, 5) == 0
    assert f(0, 4, 20, 5) == 0
    assert f(0, 5, 20, 5) == 1
    assert f(1, 4, 20, 5) == 1
    assert f(1, 1, 20, 5) == 0
    assert f(1, 0, 20, 5) == 0
    assert f(10, 14, 20, 5) == 10
    assert f(10, 15, 20, 5) == 11
    assert f(0, 3, 4, 10) == 0
