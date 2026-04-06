#!/usr/bin/env python3
"""
Analyze a VICE CPU trace (log + trace exec) for the Bruce Lee–style loader pattern.

Finds the first STA ($2D),Y at $00FA matching a start regex, then counts INC $2F at $010D
and reports the cycle after the Nth INC and the next STA ($2D),Y @ $00FA.

Example (same logic as the inline analysis on a multi-GB file — streaming only):

  python3 scripts/vice_trace_loader_counts.py /path/to/vice_full_trace.log

  python3 scripts/vice_trace_loader_counts.py trace.log --inc-counts 38911,38913

Why 38911 vs 38913:
  0x97FF = 38911 = ($E753 - $4F54) — source bytes advanced in the reference path.
  0x9801 = 38913 = ($E755 - $4F54) — two extra INC $2F in c64py for the same dest leg.
  This is pointer arithmetic, not “KERNAL free memory” (those live around $281–$2E, etc.).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("trace", type=Path, help="VICE trace log path")
    p.add_argument(
        "--start-regex",
        default=r"^\.C:00fa\s+91 2D\s+STA \(\$2D\),Y\s+- A:CC X:4F Y:00.*\s+(-?\d+)\s*$",
        help="Regex for first anchor line; group 1 = cycle (default: first loader-style STA @ $00FA)",
    )
    p.add_argument(
        "--inc-counts",
        default="38911,38913",
        help="Comma-separated N values for Nth INC $2F @ $010D after anchor (default: 38911,38913)",
    )
    p.add_argument(
        "--no-next-sta",
        action="store_true",
        help="Do not scan for next STA ($2D),Y @ $00FA after each N",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    path: Path = args.trace
    if not path.is_file():
        print(f"not a file: {path}", file=sys.stderr)
        return 1

    start_re = re.compile(args.start_regex)
    inc_line = re.compile(r"^\.C:010d\s+E6 2F\s+INC \$2F")
    sta00fa = re.compile(r"^\.C:00fa\s+91 2D\s+STA \(\$2D\),Y")

    cyc_start: int | None = None
    with path.open("r", errors="replace") as f:
        for line in f:
            m = start_re.match(line.rstrip())
            if m:
                cyc_start = int(m.group(1))
                break

    if cyc_start is None:
        print("anchor: no line matched --start-regex", file=sys.stderr)
        return 2

    counts = [int(x.strip()) for x in args.inc_counts.split(",") if x.strip()]
    if not counts:
        print("no --inc-counts", file=sys.stderr)
        return 2
    counts_sorted = sorted(set(counts))

    print(f"anchor_cycle={cyc_start} (from --start-regex)")
    print(f"nth_INC_values={counts_sorted}")
    for n in counts_sorted:
        print(f"  0x{n:X} = {n} decimal")

    results: dict[int, tuple[int, str]] = {}

    need_max = max(counts_sorted)
    cur = 0
    with path.open("r", errors="replace") as f:
        for line in f:
            if not inc_line.match(line):
                continue
            parts = line.rstrip().split()
            try:
                cyc = int(parts[-1])
            except ValueError:
                continue
            if cyc <= cyc_start:
                continue
            cur += 1
            if cur in counts_sorted:
                results[cur] = (cyc, line.rstrip())
            if cur >= need_max:
                break

    pending = {n: results[n][0] for n in counts_sorted if n in results}
    next_sta: dict[int, tuple[int, str] | None] = {n: None for n in pending}

    if pending and not args.no_next_sta:
        thresholds = sorted(pending.items(), key=lambda kv: kv[1])
        ti = 0
        with path.open("r", errors="replace") as f:
            for line in f:
                if not sta00fa.match(line):
                    continue
                parts = line.rstrip().split()
                try:
                    c = int(parts[-1])
                except ValueError:
                    continue
                while ti < len(thresholds) and c > thresholds[ti][1]:
                    n, _icyc = thresholds[ti]
                    if next_sta[n] is None:
                        next_sta[n] = (c, line.rstrip())
                    ti += 1
                if ti >= len(thresholds):
                    break

    for n in counts_sorted:
        if n not in results:
            print(f"nth={n}: not reached (file ended early?)", file=sys.stderr)
            continue
        cyc, ln = results[n]
        print(f"\n--- after {n}th INC $2F @ $010D (cyc > {cyc_start}) ---")
        print(f"last_INC_cycle={cyc}")
        print(f"line: {ln[:120]}")

        if args.no_next_sta:
            continue
        ns = next_sta.get(n)
        if not ns:
            print("next_STA: not found", file=sys.stderr)
            continue
        c_sta, l_sta = ns
        print(f"next_STA_($2D),Y_@_00fa cycle={c_sta}")
        print(f"line: {l_sta[:120]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
