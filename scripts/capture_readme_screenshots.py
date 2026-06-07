#!/usr/bin/env python3
"""Regenerate README showcase screenshots with size-budget validation.

Outputs (under docs/images/readme/ by default):
  boot_ready_graphics.png  — pygame frame at BASIC READY
  textual_ui.png           — Textual-style chrome + colored C64 panel

Usage:
    pip install -r requirements-docs.txt
    C64PY_ROM_DIR=./roms python scripts/capture_readme_screenshots.py

Fixtures live in test/fixtures/readme_screenshots/ (snapshots, BAS listings).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from readme_screenshot_common import (
    FAIL_MAX_BYTES,
    TARGET_MAX_BYTES,
    bootstrap,
    load_roms,
    load_snap,
    make_emu,
    print_size_reports,
    render_pygame_frame,
    render_textual_ui_png,
    repo_root,
    resolve_rom_dir,
    run_to_cycles,
    save_snap,
    validate_image_budget,
)

_PKG = bootstrap()


def capture_boot_ready(out_dir: Path, rom_dir: Path, fixture_dir: Path) -> Path:
    snap = fixture_dir / "boot_ready.snap"
    out = out_dir / "boot_ready_graphics.png"
    if not snap.is_file():
        emu = make_emu(vic_emulation="fast")
        load_roms(emu, rom_dir)
        emu.running = True
        run_to_cycles(emu, 5_000_000)
        save_snap(emu, snap, note="readme boot_ready")
        print(f"created fixture snapshot {snap}")
    emu = make_emu(vic_emulation="fast")
    load_roms(emu, rom_dir, require_char=True)
    load_snap(emu, snap)
    w, h = render_pygame_frame(emu, out, cycles=0, mode="latched")
    print(f"wrote {out} ({w}x{h})")
    return out


def capture_textual_ui(out_dir: Path, rom_dir: Path, fixture_dir: Path) -> Path:
    snap = fixture_dir / "boot_ready.snap"
    out = out_dir / "textual_ui.png"
    emu = make_emu(vic_emulation="fast")
    load_roms(emu, rom_dir)
    load_snap(emu, snap)
    w, h = render_textual_ui_png(emu, out)
    print(f"wrote {out} ({w}x{h})")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=repo_root() / "docs" / "images" / "readme",
        help="Directory for PNG outputs (default: docs/images/readme/)",
    )
    ap.add_argument("--rom-dir", type=Path, default=None, help="ROM directory")
    ap.add_argument(
        "--include-local",
        action="store_true",
        help="Also run docs/screenshots/local/manifest.local.json if present",
    )
    args = ap.parse_args()

    if not shutil_which("petcat"):
        print("ERROR: petcat not found (install VICE tools)", file=sys.stderr)
        return 1

    rom_dir = resolve_rom_dir(str(args.rom_dir) if args.rom_dir else None)
    fixture_dir = repo_root() / "test" / "fixtures" / "readme_screenshots"
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"ROM dir: {rom_dir}")
    print(f"Output dir: {out_dir}")
    print(f"Budget: target <= {TARGET_MAX_BYTES} B, fail > {FAIL_MAX_BYTES} B")
    print()

    outputs: list[Path] = []
    outputs.append(capture_boot_ready(out_dir, rom_dir, fixture_dir))
    outputs.append(capture_textual_ui(out_dir, rom_dir, fixture_dir))

    if args.include_local:
        from readme_screenshot_common import run_manifest_entries
        import json

        local_dir = repo_root() / "docs" / "screenshots" / "local"
        manifest_path = local_dir / "manifest.local.json"
        if manifest_path.is_file():
            print()
            print("=== local manifest ===")
            with manifest_path.open(encoding="utf-8") as fh:
                manifest = json.load(fh)
            outputs.extend(
                run_manifest_entries(manifest, local_dir=local_dir, rom_dir=rom_dir)
            )
        else:
            print(f"SKIP: no {manifest_path}")

    print()
    reports = [validate_image_budget(p) for p in outputs]
    return print_size_reports(reports)


def shutil_which(cmd: str) -> str | None:
    from shutil import which

    return which(cmd)


if __name__ == "__main__":
    sys.exit(main())
