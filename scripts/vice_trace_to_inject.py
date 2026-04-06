#!/usr/bin/env python3
"""
Build --debug-inject-map (and hints for --debug-inject-at-cycle) from a VICE-style
CPU trace line or from the first matching line in a large trace file.

VICE cycle numbers differ from c64py's; use --inject-cycle with your *c64py* cycle when
calling C64.py. Use --match-vice-cycle only to locate the right line inside a VICE log.

Examples:

  # Paste one line (quotes as needed):
  python3 scripts/vice_trace_to_inject.py --line ".C:010f  D0 02  ...  90487723"

  # Big trace: use ripgrep (much faster than a pure Python scan):
  python3 scripts/vice_trace_to_inject.py --file vice_full_trace.log --match-vice-cycle 90487723 --fast-rg

  # Add ZP $2D-$30 from a monitor dump (four bytes, comma or space separated):
  python3 scripts/vice_trace_to_inject.py --file vice_full_trace.log --match-vice-cycle 90487723 \\
      --zp-2d-to-30 f5,4c,53,e7

  # Full suggested command (replace C64PY_CYCLE with e.g. value from compare_loader_branches):
  python3 scripts/vice_trace_to_inject.py ... --inject-cycle 12794852 --print-c64py-command \\
      programs/BruceLee.prg /path/to/roms

Output: maps VICE NV-BDIZC string to P for c64py (bit 5 = 0x20 set like typical trace display).

For stack page **`$0100`–`$01FF`**, convert a VICE **`m 0100 01ff`** dump to **`addr=value`** lines and pass **`--debug-inject-file`** to C64.py (see [test/fixtures/debug_inject_stack.example.txt](../test/fixtures/debug_inject_stack.example.txt)).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path


TRACE_RE = re.compile(
    r"^\.C:([0-9a-fA-F]{4})\s+.*-\s+A:([0-9A-Fa-f]{2}) X:([0-9A-Fa-f]{2}) Y:([0-9A-Fa-f]{2}) "
    r"SP:([0-9a-fA-F]{2})\s+"
    r"(\S{8})\s+"
    r"(-?\d+)\s*$"
)


def p_from_vice_flag_string(flags: str) -> int:
    """Rebuild 6502 P from VICE 8-char NV-BDIZC display (pos 2 is unused '-')."""
    if len(flags) != 8:
        return 0x20
    p = 0x20  # bit 5 unused on 6502, often shown as set in traces
    if flags[0] == "N":
        p |= 0x80
    if flags[1] == "V":
        p |= 0x40
    if flags[3] == "B":
        p |= 0x10
    if flags[4] == "D":
        p |= 0x08
    if flags[5] == "I":
        p |= 0x04
    if flags[6] == "Z":
        p |= 0x02
    if flags[7] == "C":
        p |= 0x01
    return p & 0xFF


def parse_trace_line(line: str) -> tuple[str, str, str, str, str, str, int]:
    """Return pc, a, x, y, sp, flags, vice_cycles."""
    m = TRACE_RE.match(line.strip())
    if not m:
        raise ValueError(f"not a VICE trace instruction line: {line[:80]!r}...")
    pc, a, x, y, sp, flags, cyc = m.groups()
    return pc.lower(), a.lower(), x.lower(), y.lower(), sp.lower(), flags, int(cyc)


def parse_zp_2d_30(s: str) -> list[tuple[int, int]]:
    """'f5,4c,53,e7' or 'f5 4c 53 e7' -> [(0x2d,f5), (0x2e,4c), (0x2f,53), (0x30,e7)]."""
    parts = re.split(r"[\s,]+", s.strip())
    parts = [p for p in parts if p]
    if len(parts) != 4:
        raise ValueError(f"need 4 hex bytes for 2d..30, got {parts!r}")
    addrs = [0x2D, 0x2E, 0x2F, 0x30]
    return [(a, int(b, 16) & 0xFF) for a, b in zip(addrs, parts)]


def build_inject_map(
    a: str,
    x: str,
    y: str,
    flags: str,
    zp_pairs: list[tuple[int, int]] | None,
) -> str:
    p = p_from_vice_flag_string(flags)
    chunks = [f"a={a}", f"x={x}", f"y={y}", f"p={p:02x}"]
    if zp_pairs:
        for addr, val in zp_pairs:
            chunks.append(f"{addr:02x}={val:02x}")
    return ",".join(chunks)


def iter_trace_lines(path: Path):
    with path.open("r", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith(".C:") and " - A:" in line:
                yield line


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--line", type=str, help="Single VICE trace line")
    g.add_argument("--file", type=Path, help="Trace file (streaming)")
    ap.add_argument(
        "--match-vice-cycle",
        type=int,
        default=None,
        help="With --file: use first instruction line whose last field equals this cycle",
    )
    ap.add_argument(
        "--fast-rg",
        action="store_true",
        help="With --file: use ripgrep (rg) to find the line (much faster on multi-GB traces)",
    )
    ap.add_argument(
        "--zp-2d-to-30",
        type=str,
        default=None,
        metavar="B0,B1,B2,B3",
        help="Four hex bytes for addresses $2D,$2E,$2F,$30 (e.g. f5,4c,53,e7)",
    )
    ap.add_argument(
        "--inject-cycle",
        type=int,
        default=None,
        metavar="N",
        help="c64py CPU cycle for --debug-inject-at-cycle (for --print-c64py-command)",
    )
    ap.add_argument(
        "--print-c64py-command",
        action="store_true",
        help="Print a sample python3 C64.py ... line (needs --inject-cycle)",
    )
    ap.add_argument(
        "remainder",
        nargs="*",
        help="With --print-c64py-command: prg path and optional --rom-dir path",
    )
    args = ap.parse_args()

    zp_pairs: list[tuple[int, int]] | None = None
    if args.zp_2d_to_30:
        zp_pairs = parse_zp_2d_30(args.zp_2d_to_30)

    line: str | None = None
    vice_cyc: int | None = None
    if args.line:
        line = args.line
    else:
        if args.match_vice_cycle is None:
            print("ERROR: --file requires --match-vice-cycle", file=sys.stderr)
            return 2
        cyc = args.match_vice_cycle
        if args.fast_rg:
            if not shutil.which("rg"):
                print("ERROR: --fast-rg needs ripgrep (rg) on PATH", file=sys.stderr)
                return 2
            pat = rf"^\.C:.*\s{cyc}\s*$"
            r = subprocess.run(
                ["rg", "-m", "1", pat, str(args.file)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if r.returncode != 0 or not r.stdout.strip():
                print(f"ERROR: rg found no .C: line ending with cycle {cyc}", file=sys.stderr)
                return 2
            line = r.stdout.strip().splitlines()[0]
            vice_cyc = cyc
        else:
            for ln in iter_trace_lines(args.file):
                try:
                    _pc, _a, _x, _y, _sp, _fl, cyc2 = parse_trace_line(ln)
                except ValueError:
                    continue
                if cyc2 == cyc:
                    line = ln
                    vice_cyc = cyc2
                    break
            if line is None:
                print(f"ERROR: no trace line with cycle {cyc}", file=sys.stderr)
                return 2

    pc, a, x, y, _sp, flags, cyc = parse_trace_line(line)
    if vice_cyc is None:
        vice_cyc = cyc

    mmap = build_inject_map(a, x, y, flags, zp_pairs)

    print(f"# pc=${pc} vice_cycle={vice_cyc} flags={flags} -> p=${p_from_vice_flag_string(flags):02x}")
    print(f"DEBUG_INJECT_MAP={mmap!r}")

    if args.print_c64py_command:
        if args.inject_cycle is None:
            print("ERROR: --print-c64py-command needs --inject-cycle (c64py side)", file=sys.stderr)
            return 2
        rest = args.remainder
        prg = rest[0] if rest else "programs/BruceLee.prg"
        rom = ""
        if len(rest) >= 2:
            rom = f" --rom-dir {rest[1]}"
        cmd = (
            f"python3 C64.py {prg} --headless --turbo --max-cycles 13150000 --autoquit{rom} "
            f"--debug-inject-at-cycle {args.inject_cycle} "
            f"--debug-inject-map {mmap}"
        )
        print(f"\n# c64py cycle {args.inject_cycle} (not VICE {vice_cyc})")
        print(cmd)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
