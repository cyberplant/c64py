#!/usr/bin/env python3
"""
Count JSR $0103 and JSR $00FA executed from PC $087E–$0884 in a VICE CPU trace,
inside the Bruce-loader window: after the usual STA ($2D),Y @ $00FA anchor
(A:CC X:4F Y:00) until the cycle of the Nth INC $2F @ $010D (default N=38911).

This matches the c64py window from C64PY_LOADER_JSR_COUNT=1 (same milestones as
C64PY_LOADER_PTR_SRC_COUNT). Use the same N as the VICE “38911 vs 38913” experiment
if you want comparable bounds.

Usage:
  python3 scripts/vice_trace_loader_jsr_counts.py /path/to/vice_full_trace.log \\
      --nth-inc 38911 --end-at-next-sta00fa-after-nth-inc
  python3 scripts/vice_trace_loader_jsr_counts.py trace.log --nth-inc 38913 \\
      --end-at-next-sta00fa-after-nth-inc
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ANCHOR = re.compile(
    r"^\.C:00fa\s+91 2D\s+STA \(\$2D\),Y\s+- A:CC X:4F Y:00.*\s+(-?\d+)\s*$"
)
INC_010D = re.compile(r"^\.C:010d\s+E6 2F\s+INC \$2F")
# PC $087E–$0884, JSR abs little-endian ($0103 → 03 01, $00FA → FA 00)
JSR_BAND = re.compile(
    r"^\.C:([0-9a-fA-F]{4})\s+20 (03 01|FA 00)\s+JSR",
    re.IGNORECASE,
)


def pc_in_jsr_band(pc_s: str) -> bool:
    try:
        pc = int(pc_s, 16)
    except ValueError:
        return False
    return 0x087E <= pc <= 0x0884


def target_from_operand(op: str) -> str:
    if op.upper() == "03 01":
        return "$0103"
    if op.upper() == "FA 00":
        return "$00FA"
    return op


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("trace", type=Path, help="VICE CPU trace path")
    p.add_argument(
        "--nth-inc",
        type=int,
        default=38911,
        help="End window at cycle of this INC $2F @ $010D after anchor (default 38911)",
    )
    p.add_argument(
        "--start-regex",
        default=None,
        help="Override anchor regex (must have group 1 = cycle); default: STA @ $00FA A:CC X:4F",
    )
    p.add_argument(
        "--end-at-next-sta00fa-after-nth-inc",
        action="store_true",
        help="End window at first STA ($2D),Y @ $00FA after the Nth INC (exclusive), "
        "matching c64py milestone first_e5f0 style bounds more closely than --nth-inc alone",
    )
    return p.parse_args()


STA_00FA = re.compile(r"^\.C:00fa\s+91 2D\s+STA \(\$2D\),Y")


def find_anchor_and_end_cycles(
    path: Path,
    start_re: re.Pattern[str],
    nth: int,
    end_at_next_sta: bool,
) -> tuple[int, int]:
    """Return (anchor_cyc, end_cyc). JSR counts use anchor_cyc < cyc < end_cyc (strict)."""
    anchor_cyc: int | None = None
    inc_after = 0
    inc_nth_cyc: int | None = None
    with path.open("r", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if anchor_cyc is None:
                m = start_re.match(line)
                if m:
                    anchor_cyc = int(m.group(1))
                continue
            if not INC_010D.match(line):
                continue
            parts = line.split()
            try:
                cyc = int(parts[-1])
            except (ValueError, IndexError):
                continue
            if cyc <= anchor_cyc:
                continue
            inc_after += 1
            if inc_after == nth:
                inc_nth_cyc = cyc
                break
    if anchor_cyc is None:
        raise ValueError("no anchor line matched")
    if inc_nth_cyc is None:
        raise ValueError(f"did not reach {nth}th INC $2F @ $010D after anchor")

    if not end_at_next_sta:
        return anchor_cyc, inc_nth_cyc

    end_cyc: int | None = None
    with path.open("r", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not STA_00FA.match(line):
                continue
            parts = line.split()
            try:
                cyc = int(parts[-1])
            except (ValueError, IndexError):
                continue
            if cyc > inc_nth_cyc:
                end_cyc = cyc
                break
    if end_cyc is None:
        raise ValueError("no STA ($2D),Y @ $00FA after Nth INC")
    return anchor_cyc, end_cyc


def count_jsr_in_window(
    path: Path, anchor_cyc: int, end_cyc: int, end_exclusive: bool
) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    with path.open("r", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = JSR_BAND.match(line)
            if not m:
                continue
            pc_s, op = m.groups()
            if not pc_in_jsr_band(pc_s):
                continue
            parts = line.split()
            try:
                cyc = int(parts[-1])
            except (ValueError, IndexError):
                continue
            if cyc <= anchor_cyc:
                continue
            if end_exclusive:
                if cyc >= end_cyc:
                    continue
            elif cyc > end_cyc:
                continue
            tgt = target_from_operand(op)
            key = (pc_s.lower(), tgt)
            counts[key] = counts.get(key, 0) + 1
    return counts


def main() -> int:
    args = parse_args()
    path: Path = args.trace
    if not path.is_file():
        print(f"not a file: {path}", file=sys.stderr)
        return 1
    start_re = re.compile(args.start_regex) if args.start_regex else ANCHOR
    try:
        anchor_cyc, end_cyc = find_anchor_and_end_cycles(
            path,
            start_re,
            args.nth_inc,
            args.end_at_next_sta00fa_after_nth_inc,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"anchor_cycle={anchor_cyc}")
    if args.end_at_next_sta00fa_after_nth_inc:
        print(
            f"end_cycle={end_cyc} (first STA @ $00FA after {args.nth_inc}th INC; "
            f"JSR counted with cyc < this)"
        )
        excl = True
    else:
        print(f"end_cycle={end_cyc} (at {args.nth_inc}th INC $2F @ $010D; JSR with cyc <= end)")
        excl = False
    counts = count_jsr_in_window(path, anchor_cyc, end_cyc, excl)
    if not counts:
        print("no JSR $0103 / $00FA from $087E–$0884 in window")
        return 0
    total = 0
    for (pc_s, tgt), n in sorted(counts.items()):
        print(f"  .C:{pc_s} -> {tgt}  n={n}")
        total += n
    print(f"total_jsr_in_band={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
