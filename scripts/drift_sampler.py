#!/usr/bin/env python3
"""Sample CPU cycle drift between two traces at equal instruction counts.

Extracts the cumulative cycle value at every Nth instruction (counted
post-sync) from each trace and prints a side-by-side table with the drift
(ours − vice) so we can tell whether the drift accumulates linearly or in
discrete jumps.

Usage:
    drift_sampler.py OURS_TRACE OURS_OFFSET VICE_TRACE VICE_OFFSET [--step 50000] [--max-lines 900000]

OURS_OFFSET / VICE_OFFSET are byte offsets of the sync point (typically the
$C200 line in each file), obtained e.g. via::

    rg -b -m 1 '^\\.C:c200 ' FILE

The script assumes both traces are aligned at that offset (same instruction
stream starting there; cycles may differ).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterator, Optional, Tuple

# ``.C:xxxx  BB …  mnem …  - A:xx … flags  CYCLES [; rust]``
CYCLE_RE_RUST = re.compile(r"\s+(\d+)\s*;\s*rust\s*$")
CYCLE_RE_VICE = re.compile(r"\s+(\d+)\s*$")
CODE_LINE_RE = re.compile(r"^\.C:[0-9a-fA-F]{4}\s+[0-9A-Fa-f]{2}")


def iter_code_lines(path: Path, start: int) -> Iterator[str]:
    """Yield only .C: instruction lines starting at byte offset *start*."""
    with open(path, "rb") as f:
        f.seek(start)
        for raw in f:
            try:
                s = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            if not s.startswith(".C:"):
                continue
            if not CODE_LINE_RE.match(s):
                continue
            yield s


def cycle_from_line(line: str, *, rust: bool) -> Optional[int]:
    pat = CYCLE_RE_RUST if rust else CYCLE_RE_VICE
    m = pat.search(line)
    return int(m.group(1)) if m else None


def sample(
    path: Path, start: int, *, rust: bool, step: int, max_lines: int
) -> Tuple[list[int], list[int]]:
    """Return (line_numbers, cycles) sampled every *step* code lines.

    Always includes the first line (at offset ``start``) so the callers can
    subtract the baseline cycle to get a delta-from-sync view.
    """
    xs: list[int] = []
    ys: list[int] = []
    it = iter_code_lines(path, start)
    for i, line in enumerate(it):
        if i >= max_lines:
            break
        if i == 0 or (i % step) == 0 or i == max_lines - 1:
            c = cycle_from_line(line, rust=rust)
            if c is None:
                continue
            xs.append(i)
            ys.append(c)
    return xs, ys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ours")
    ap.add_argument("ours_offset", type=int)
    ap.add_argument("vice")
    ap.add_argument("vice_offset", type=int)
    ap.add_argument("--step", type=int, default=50_000)
    ap.add_argument("--max-lines", type=int, default=900_000)
    args = ap.parse_args(argv)

    ox, oy = sample(
        Path(args.ours), args.ours_offset,
        rust=True, step=args.step, max_lines=args.max_lines,
    )
    vx, vy = sample(
        Path(args.vice), args.vice_offset,
        rust=False, step=args.step, max_lines=args.max_lines,
    )

    if not ox or not vx:
        print("ERROR: no samples extracted; check offsets / trace format", file=sys.stderr)
        return 1

    o_base = oy[0]
    v_base = vy[0]

    print(
        f"{'instr_after_sync':>18}  {'ours_Δcyc':>14}  {'vice_Δcyc':>14}  {'drift':>10}  {'per_1k':>10}"
    )
    n = min(len(ox), len(vx))
    last_drift = 0
    for i in range(n):
        if ox[i] != vx[i]:
            continue
        do = oy[i] - o_base
        dv = vy[i] - v_base
        drift = do - dv
        per_1k = ((drift - last_drift) / max(1, ox[i] - (ox[i - 1] if i > 0 else 0))) * 1000
        print(
            f"{ox[i]:>18}  {do:>14}  {dv:>14}  {drift:>+10}  {per_1k:>+10.2f}"
        )
        last_drift = drift
    return 0


if __name__ == "__main__":
    sys.exit(main())
