#!/usr/bin/env python3
"""Profile CPU6502.step / _vic_tick_one / ViciiCycleEngine.tick (cProfile).

Run (package layout: parent of ``c64py`` on PYTHONPATH):
  PYTHONPATH=/path/to/parent python3 /path/to/c64py/scripts/profile_hotpath.py [steps]
"""
from __future__ import annotations

import cProfile
import os
import pstats
import sys
from io import StringIO


def main() -> None:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

    from c64py.memory import MemoryMap
    from c64py.cpu import CPU6502

    m = MemoryMap()
    c = CPU6502(m)
    c.state.pc = 0x0800
    m._vic_regs[0x11] = 0x1B
    m._vic_regs[0x12] = 0x00
    m._vic_regs[0x15] = 0x00

    n = 50_000
    if len(sys.argv) > 1:
        n = int(sys.argv[1])

    pr = cProfile.Profile()
    pr.enable()
    for _ in range(n):
        c.step()
    pr.disable()

    s = StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumtime").print_stats(40)
    print(s.getvalue())


if __name__ == "__main__":
    main()
