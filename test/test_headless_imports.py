"""Headless import guards (PyPy Docker smoke test has no textual/pygame)."""

from __future__ import annotations

import builtins
import importlib


def _block_textual_import(name, *args, **kwargs):
    if name == "textual" or name.startswith("textual."):
        raise AssertionError(f"unexpected textual import: {name!r}")
    return _REAL_IMPORT(name, *args, **kwargs)


_REAL_IMPORT = builtins.__import__


def test_c64py_config_import_without_textual(monkeypatch) -> None:
    monkeypatch.setattr(builtins, "__import__", _block_textual_import)
    importlib.import_module("c64py.config")


def test_emulator_headless_factory_without_textual(monkeypatch) -> None:
    monkeypatch.setattr(builtins, "__import__", _block_textual_import)
    from c64py.emulator import C64

    C64(interface_factory=lambda _emu: None, vic_emulation="fast")
