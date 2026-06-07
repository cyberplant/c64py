#!/usr/bin/env python3
"""Hint script for comparing c64py and VICE screenshots (PNG).

Does not require PIL: prints suggested commands. With Pillow installed, also
reports per-channel mean absolute error for a quick numeric delta.

Usage:
  python3 scripts/vice_frame_compare.py shot_c64py.png shot_vice.png
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: vice_frame_compare.py <image_a.png> <image_b.png>", file=sys.stderr)
        sys.exit(1)
    a, b = Path(sys.argv[1]), Path(sys.argv[2])
    if not a.is_file() or not b.is_file():
        print("ERROR: both paths must exist", file=sys.stderr)
        sys.exit(1)
    print(f"sha256 {a.name}: {_sha256(a)}")
    print(f"sha256 {b.name}: {_sha256(b)}")
    print("Tools: compare (ImageMagick), perceptualdiff, ffmpeg blend, or:")
    try:
        from PIL import Image  # type: ignore[import-not-found]

        im1 = Image.open(a).convert("RGB")
        im2 = Image.open(b).convert("RGB")
        if im1.size != im2.size:
            print(f"size mismatch: {im1.size} vs {im2.size}")
            sys.exit(2)
        px1, px2 = im1.load(), im2.load()
        w, h = im1.size
        acc = [0, 0, 0]
        n = w * h
        for y in range(h):
            for x in range(w):
                p, q = px1[x, y], px2[x, y]
                acc[0] += abs(p[0] - q[0])
                acc[1] += abs(p[1] - q[1])
                acc[2] += abs(p[2] - q[2])
        mae = tuple(c / n for c in acc)
        print(f"mean_abs_error_rgb: {mae[0]:.4f} {mae[1]:.4f} {mae[2]:.4f}")
    except ImportError:
        print("(Install Pillow for RGB mean absolute error.)")


if __name__ == "__main__":
    main()
