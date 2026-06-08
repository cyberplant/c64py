#!/usr/bin/env python3
"""Capture PNGs from user-provided PRG/D64 files via manifest.local.json.

Media and outputs stay under docs/screenshots/local/ (gitignored binaries).

Usage:
    cp docs/screenshots/local/manifest.example.json \\
       docs/screenshots/local/manifest.local.json
    # add mydemo.prg + edit manifest
    C64PY_ROM_DIR=./roms python scripts/capture_local_screenshots.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from readme_screenshot_common import (
    bootstrap,
    print_size_reports,
    repo_root,
    resolve_rom_dir,
    run_manifest_entries,
    set_display_options,
    validate_image_budget,
)

bootstrap()


def _load_manifest(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--local-dir",
        type=Path,
        default=repo_root() / "docs" / "screenshots" / "local",
        help="Directory containing manifest.local.json and media files",
    )
    ap.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Manifest path (default: <local-dir>/manifest.local.json)",
    )
    ap.add_argument("--rom-dir", type=Path, default=None)
    ap.add_argument(
        "--id",
        dest="ids",
        action="append",
        default=None,
        metavar="ID[,ID...]",
        help="Only run manifest entries with these id(s). Comma-separated and/or "
        "repeat the flag. Default: run all entries.",
    )
    ap.add_argument(
        "--show",
        action="store_true",
        help="Open a live pygame window and mirror captured frames into it while "
        "running (handy for local debugging; omit for headless/CI).",
    )
    ap.add_argument(
        "--show-scale",
        type=int,
        default=2,
        metavar="N",
        help="Integer scale for the --show window (default: 2).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned captures without running the emulator",
    )
    args = ap.parse_args()

    set_display_options(show=args.show, scale=args.show_scale)

    only_ids = None
    if args.ids:
        only_ids = {
            piece.strip()
            for chunk in args.ids
            for piece in chunk.split(",")
            if piece.strip()
        }

    local_dir = args.local_dir.resolve()
    manifest_path = (args.manifest or local_dir / "manifest.local.json").resolve()
    if not manifest_path.is_file():
        example = local_dir / "manifest.example.json"
        print(
            f"ERROR: {manifest_path} not found.\n"
            f"Copy {example} to manifest.local.json and add your media.",
            file=sys.stderr,
        )
        return 1

    rom_dir = resolve_rom_dir(str(args.rom_dir) if args.rom_dir else None)
    manifest = _load_manifest(manifest_path)
    print(f"Manifest: {manifest_path}")
    print(f"Local dir: {local_dir}")
    print(f"ROM dir: {rom_dir}")
    print()

    if only_ids:
        print(f"Filtering to id(s): {', '.join(sorted(only_ids))}")
    outputs = run_manifest_entries(
        manifest,
        local_dir=local_dir,
        rom_dir=rom_dir,
        dry_run=args.dry_run,
        only_ids=only_ids,
    )
    if args.dry_run:
        return 0
    if not outputs:
        print("No captures produced (missing media or empty manifest).")
        return 0

    print()
    pngs = [p for p in outputs if p.suffix.lower() == ".png"]
    videos = [p for p in outputs if p.suffix.lower() != ".png"]
    for vid in videos:
        size = vid.stat().st_size if vid.is_file() else 0
        print(f"[VIDEO] {vid} ({size:,} bytes)")
    reports = [validate_image_budget(p) for p in pngs]
    return print_size_reports(reports)


if __name__ == "__main__":
    sys.exit(main())
