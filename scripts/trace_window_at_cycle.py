#!/usr/bin/env python3
"""Stream a VICE-format CPU trace and print context around an anchor point.

Two ways to choose the anchor:

1) **By cumulative trace cycle** (``--at-cycle N``) — both VICE and c64py can use the same N
   only if their trace clocks are comparable (usually false when c64py skips real disk I/O).

2) **By first hit of a PC** (``--first-at-pc HEX``) — e.g. game-entry PC after the
   loader's ``JMP`` into game code. Use optional
   ``--min-cycle`` to skip earlier spurious hits of the same PC.

Examples:
  python3 scripts/trace_window_at_cycle.py logs/vice_full_trace.log \\
      --first-at-pc C200 --vice-monitor-dedupe --before 20 --after 60 --analyze 5000 --print-sync-hint

  python3 scripts/trace_window_at_cycle.py logs/vice_full_trace.log --at-cycle 50000000 \\
      --vice-monitor-dedupe --analyze 3000
"""

from __future__ import annotations

import argparse
import collections
import os
import sys
from collections import deque
from typing import Deque, Iterator, List, Optional, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from compare_traces import parse_trace_line  # noqa: E402


def iter_trace_rows(
    path: str,
    dedupe_same_pc_opcode: bool,
) -> Iterator[Tuple[int, object]]:
    """Yield (byte_offset_of_line_start, TraceLine)."""
    last_key: Optional[Tuple[int, int]] = None
    with open(path, "rb") as f:
        while True:
            line_start = f.tell()
            raw = f.readline()
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace")
            tl = parse_trace_line(text, 0)
            if tl is None:
                continue
            if dedupe_same_pc_opcode:
                key = (tl.pc, tl.opcode)
                if key == last_key:
                    continue
                last_key = key
            yield line_start, tl


def analyze_pc_runs(rows: List[object]) -> None:
    """Print streak and histogram stats for a list of TraceLine-like rows."""
    if not rows:
        print("(no rows)")
        return
    pcs = [r.pc for r in rows]
    best_pc, best_len, cur_pc, cur_len = -1, 0, pcs[0], 1
    for p in pcs[1:]:
        if p == cur_pc:
            cur_len += 1
        else:
            if cur_len > best_len:
                best_pc, best_len = cur_pc, cur_len
            cur_pc, cur_len = p, 1
    if cur_len > best_len:
        best_pc, best_len = cur_pc, cur_len
    print(f"  Longest single-PC run: ${best_pc:04X} × {best_len} instructions")

    ctr = collections.Counter(pcs)
    print("  Top PCs by count in window:")
    for pc, n in ctr.most_common(15):
        print(f"    ${pc:04X}  {n:6d}  ({100.0 * n / len(pcs):.1f}%)")


def _parse_pc(s: str) -> int:
    s = s.strip().lower().replace("0x", "")
    return int(s, 16)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("trace", help="Path to .C: format trace (VICE or c64py)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--at-cycle", type=int, metavar="N", help="Anchor at first row with cumulative cycle >= N")
    mode.add_argument(
        "--first-at-pc",
        type=_parse_pc,
        metavar="HEX",
        help="Anchor at first row with this PC (e.g. C200 for game entry after a loader JMP)",
    )
    ap.add_argument(
        "--min-cycle",
        type=int,
        default=None,
        metavar="N",
        help="With --first-at-pc: only accept anchor when cumulative cycle >= N (skip earlier PC hits)",
    )
    ap.add_argument(
        "--vice-monitor-dedupe",
        action="store_true",
        help="Skip consecutive same (PC, opcode) rows (VICE monitor duplicate lines)",
    )
    ap.add_argument("--before", type=int, default=25, help="Trace rows to print before anchor")
    ap.add_argument("--after", type=int, default=60, help="Trace rows to print after anchor")
    ap.add_argument(
        "--analyze",
        type=int,
        default=0,
        metavar="N",
        help="After anchor, analyze next N rows for tight loops / PC histogram (0=off)",
    )
    ap.add_argument(
        "--print-sync-hint",
        action="store_true",
        help="Print suggested tools/compare_traces.py flags",
    )
    args = ap.parse_args()

    if args.min_cycle is not None and args.at_cycle is not None:
        ap.error("--min-cycle is only valid with --first-at-pc")

    buf: Deque[Tuple[int, object]] = deque(maxlen=max(1, args.before))
    anchor: Optional[Tuple[int, object]] = None
    after_rows: List[object] = []
    scanned = 0

    for line_start, tl in iter_trace_rows(args.trace, args.vice_monitor_dedupe):
        scanned += 1
        if scanned % 2_500_000 == 0:
            print(f"... scanned {scanned:,} trace rows, last cyc={tl.cycles:,}", file=sys.stderr)
        if anchor is None:
            if args.at_cycle is not None:
                hit = tl.cycles >= args.at_cycle
            else:
                hit = tl.pc == args.first_at_pc and (
                    args.min_cycle is None or tl.cycles >= args.min_cycle
                )
            if hit:
                anchor = (line_start, tl)
            else:
                buf.append((line_start, tl))
        else:
            after_rows.append(tl)
            if len(after_rows) >= args.after + max(0, args.analyze):
                break

    if anchor is None:
        if args.at_cycle is not None:
            print(f"No trace row with cycles >= {args.at_cycle:,} (scanned {scanned:,} rows)", file=sys.stderr)
        else:
            extra = f" and cycles>={args.min_cycle:,}" if args.min_cycle is not None else ""
            print(
                f"No trace row with PC=${args.first_at_pc:04X}{extra} (scanned {scanned:,} rows)",
                file=sys.stderr,
            )
        sys.exit(1)

    a_off, a_tl = anchor
    print(f"Anchor: byte={a_off:,}  cyc={a_tl.cycles:,}  PC=${a_tl.pc:04X}  {a_tl.mnemonic}  {a_tl.raw.strip()}")
    print()
    print("--- before ---")
    for _bo, row in buf:
        print(row.raw.strip())
    print(">>> ANCHOR <<<")
    print(a_tl.raw.strip())
    print("--- after ---")
    for row in after_rows[: args.after]:
        print(row.raw.strip())

    if args.analyze > 0:
        chunk = after_rows[: args.analyze]
        print()
        print(f"--- analyze first {len(chunk)} rows after anchor ---")
        analyze_pc_runs(chunk)

    if args.print_sync_hint:
        print()
        print("Suggested tools/compare_traces.py (use --vice-monitor-dedupe for VICE monitor logs):")
        pc = a_tl.pc
        print(f"  --match-cycles-at {pc:04x}")
        if args.at_cycle is not None:
            print(f"  --match-min-cycle {args.at_cycle}   # same clock basis only")
        else:
            print(
                "  # If this PC appears at different absolute cycles (disk load vs fast load), set per side, e.g.:"
            )
            print(
                f"  #   --our-match-min-cycle 21000000 --vice-match-min-cycle 80000000"
            )
            print("  # Or omit both mins if the first hit of this PC is the right one on each trace.")
        print(f"  --vice-skip-bytes {a_off}   # byte offset for *this* file (copy from anchor above)")


if __name__ == "__main__":
    main()
