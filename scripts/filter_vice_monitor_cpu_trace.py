#!/usr/bin/env python3
"""Stream-filter VICE monitor CPU traces for tools/compare_traces.py.

VICE often logs a breakpoint/stop line and then a duplicate ``.C:addr`` row for the
same instruction (e.g. two ``.C:fce2`` lines before ``.C:fce4``). c64py emits one row
per instruction, so drop consecutive duplicate (PC, opcode) rows.

Usage:
  python3 scripts/filter_vice_monitor_cpu_trace.py logs/vice_full_trace.log > vice_clean.log
  python3 scripts/filter_vice_monitor_cpu_trace.py < vice.log > vice_clean.log

Only lines starting with ``.C:`` are passed through (other monitor noise is skipped).
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import BinaryIO, Optional, TextIO

ROW = re.compile(
    r"^\.C:([0-9A-Fa-f]{4})\s+([0-9A-Fa-f]{2})\b",
)


def filter_stream(inp: TextIO, out: TextIO, max_rows: Optional[int] = None) -> None:
    last_key: Optional[tuple[str, str]] = None
    n = 0
    try:
        for line in inp:
            if not line.startswith(".C:"):
                continue
            m = ROW.match(line)
            if not m:
                out.write(line)
                n += 1
                if max_rows is not None and n >= max_rows:
                    break
                continue
            key = (m.group(1).lower(), m.group(2).lower())
            if key == last_key:
                continue
            last_key = key
            out.write(line)
            n += 1
            if max_rows is not None and n >= max_rows:
                break
    except BrokenPipeError:
        return


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "path",
        nargs="?",
        help="Trace file (default: stdin)",
    )
    ap.add_argument(
        "--max-rows",
        type=int,
        default=None,
        metavar="N",
        help="Stop after writing N trace rows (faster smoke tests on huge logs)",
    )
    args = ap.parse_args()
    if args.path:
        with open(args.path, "r", encoding="utf-8", errors="replace") as f:
            filter_stream(f, sys.stdout, max_rows=args.max_rows)
    else:
        filter_stream(sys.stdin, sys.stdout, max_rows=args.max_rows)


if __name__ == "__main__":
    main()
