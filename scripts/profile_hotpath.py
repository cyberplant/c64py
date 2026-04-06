#!/usr/bin/env python3
"""Profile CPU6502.step / _vic_tick_one / ViciiCycleEngine.tick (cProfile).

Run (parent of the ``c64py`` package dir on PYTHONPATH, same as editable install):
  PYTHONPATH=/path/to/parent python3 scripts/profile_hotpath.py [--accurate-vic] [steps]

Example from repo root named ``c64py`` under ``dev``:
  PYTHONPATH=/path/to/dev python3 c64py/scripts/profile_hotpath.py --accurate-vic 20000
"""
from __future__ import annotations

import argparse
import cProfile
import os
import pstats
import sys
from io import StringIO


def main() -> None:
    ap = argparse.ArgumentParser(description="cProfile CPU6502.step hot path")
    ap.add_argument(
        "steps",
        nargs="?",
        type=int,
        default=50_000,
        help="Number of step() calls (default 50000)",
    )
    ap.add_argument(
        "--accurate-vic",
        action="store_true",
        help="Enable cycle-accurate VIC (matches --accurate-vic in C64.py)",
    )
    ap.add_argument("--top", type=int, default=40, help="Lines to print (default 40)")
    args = ap.parse_args()

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

    from c64py.memory import MemoryMap
    from c64py.cpu import CPU6502

    m = MemoryMap()
    c = CPU6502(m, accurate_vic=args.accurate_vic)
    c.state.pc = 0x0800
    m._vic_regs[0x11] = 0x1B
    m._vic_regs[0x12] = 0x00
    m._vic_regs[0x15] = 0x00

    pr = cProfile.Profile()
    pr.enable()
    for _ in range(args.steps):
        c.step()
    pr.disable()

    s = StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumtime").print_stats(args.top)
    print(s.getvalue())


if __name__ == "__main__":
    main()
