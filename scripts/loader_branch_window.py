#!/usr/bin/env python3
"""
Print a window of aligned (pc, take, z) branch events from a Bruce log vs VICE trace.

Uses the same anchors and filtering as compare_loader_branches.py.  Useful to see
*local* divergence (e.g. missing second $010F after JSR $0103 on one side).

Example:
  python3 scripts/loader_branch_window.py \\
    --c64py-log bruce_clean.log --vice-trace vice_full_trace.log \\
    --center-idx 105706 --radius 8
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Import from sibling script (no package)
import importlib.util

_scripts = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "compare_loader_branches", _scripts / "compare_loader_branches.py"
)
_clb = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_clb)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--c64py-log", type=Path, required=True)
    ap.add_argument("--vice-trace", type=Path, required=True)
    ap.add_argument("--center-idx", type=int, required=True, help="0-based index in branch stream")
    ap.add_argument("--radius", type=int, default=6, help="lines before/after center")
    args = ap.parse_args()

    if not args.vice_trace.is_file():
        print(f"missing {args.vice_trace}", file=sys.stderr)
        return 2

    c_anchor, c_list = _clb.parse_c64py_branches(args.c64py_log)
    v_anchor = _clb.find_vice_anchor(args.vice_trace)
    lo = max(0, args.center_idx - args.radius)
    hi = min(len(c_list), args.center_idx + args.radius + 1)

    it = _clb.iter_vice_branches(args.vice_trace, v_anchor)
    for _ in range(lo):
        next(it)

    print(f"c64py anchor_cyc={c_anchor}  vice anchor_cyc={v_anchor}")
    print(f"idx  match  c64  pc/cyc/a      vice pc/cyc/a")
    for idx in range(lo, hi):
        c = c_list[idx]
        v = next(it)
        ok = (c[0], c[2], c[3]) == (v[0], v[2], v[3])
        m = "OK " if ok else "BAD"
        mark = " <--" if idx == args.center_idx else ""
        print(
            f"{idx:6d} {m}  {c[0]} {c[1]:9d} a={c[4]}  |  {v[0]} {v[1]:9d} a={v[4]}{mark}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
