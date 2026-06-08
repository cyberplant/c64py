"""Helpers for optional external VICE test assets."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


VICE_DIR = Path(__file__).resolve().parent / "vice"
FETCH_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fetch_vice_tests.sh"


def _has_prg_files() -> bool:
    return any(VICE_DIR.rglob("*.prg"))


def ensure_vice_assets() -> bool:
    """Ensure VICE PRG corpus exists; fetch it unless disabled."""
    if _has_prg_files():
        return True

    if os.environ.get("C64PY_NO_AUTO_FETCH_VICE") == "1":
        return False

    try:
        subprocess.run([str(FETCH_SCRIPT)], check=True, cwd=Path(__file__).resolve().parent.parent)
    except Exception:
        return False

    return _has_prg_files()
