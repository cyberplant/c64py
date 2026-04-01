#!/usr/bin/env python3
"""
Compare Bruce Lee–style loader branch outcomes between:
  - c64py Bruce log (BRANCH_TRACE lines from C64PY_BRUCELEE_DEBUG=1)
  - VICE CPU trace (.C:addr ... - A:.. X:.. Y:.. SP:.. NV-BDIZC  cycles)

Anchor: first STA ($2D),Y @ $00FA with A:CC X:4F Y:00 (same as first eff=$4CF5 phase).
Only events with PC in WATCH_PCS and cycle > anchor_cycle are compared.

Usage:
  python3 scripts/compare_loader_branches.py \\
    --c64py-log /tmp/bruce.log \\
    --vice-trace /path/to/vice_full_trace.log \\
    [--max-diff 20] [--inject-hint]

If --vice-trace is omitted, only prints c64py branch count and sample lines.

With --inject-hint: also emits c64py_cyc_last_010f_before_mismatch and a suggested
--debug-inject-map from the Bruce log (regs + ZP on that $010F line).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterator


# PCs where we log BRANCH_TRACE in cpu.py (lowercase hex without $)
WATCH_PCS = frozenset({"00fe", "010f", "088a", "0120", "0125", "012c", "0130", "0134"})

C64PY_STA_ANCHOR = re.compile(
    r"STA_INDY_TRACE pc=\$00FA cyc=(\d+) .* eff=\$4CF5 "
)
C64PY_BRANCH = re.compile(
    r"^BRANCH_TRACE pc=\$([0-9A-Fa-f]{4}) cyc=(\d+) op=\$([0-9A-Fa-f]{2}) .*\b"
    r"take=(\d) z=(\d) a=\$([0-9A-Fa-f]{2}) x=\$([0-9A-Fa-f]{2}) y=\$([0-9A-Fa-f]{2})"
)
# Full $010F line includes ZP (logged in cpu.py for loader debugging).
C64PY_BRANCH_010F = re.compile(
    r"^BRANCH_TRACE pc=\$010[Ff] cyc=(\d+) op=\$([0-9A-Fa-f]{2}) "
    r"rel=\$([0-9A-Fa-f]{2}) target=\$([0-9A-Fa-f]{4}) take=(\d) z=(\d) "
    r"a=\$([0-9A-Fa-f]{2}) x=\$([0-9A-Fa-f]{2}) y=\$([0-9A-Fa-f]{2}) p=\$([0-9A-Fa-f]{2}) "
    r"zp2d=\$([0-9A-Fa-f]{2}) zp2e=\$([0-9A-Fa-f]{2}) zp2f=\$([0-9A-Fa-f]{2}) zp30=\$([0-9A-Fa-f]{2})"
)

VICE_STA_ANCHOR = re.compile(
    r"^\.C:00fa\s+91 2D\s+STA \(\$2D\),Y\s+- A:CC X:4F Y:00.*\s+(\d+)\s*$"
)
# PC, op_hi, A,X,Y, 8-char flags, cycles
VICE_LINE = re.compile(
    r"^\.C:([0-9a-fA-F]{4})\s+"
    r"([0-9A-Fa-f]{2}) "
    r"([0-9A-Fa-f]{2})\s+"
    r".*-\s+A:([0-9A-Fa-f]{2}) X:([0-9A-Fa-f]{2}) Y:([0-9A-Fa-f]{2}) "
    r"SP:[0-9a-fA-F]{2}\s+"
    r"(\S{8})\s+"
    r"(-?\d+)\s*$"
)


def z_from_vice_flags(flags: str) -> int:
    if len(flags) != 8:
        return 0
    return 1 if flags[6] == "Z" else 0


def vice_take(op: int, z: int) -> int:
    if op == 0xD0:
        return 1 if z == 0 else 0
    if op == 0xF0:
        return 1 if z == 1 else 0
    return -1


def parse_c64py_branches(path: Path) -> tuple[int, list[tuple[str, int, int, int, str, str, str]]]:
    """Return (anchor_cyc, [(pc, cyc, take, z, a, x, y), ...]). Streams large logs."""
    anchor: int | None = None
    out: list[tuple[str, int, int, int, str, str, str]] = []
    with path.open("r", errors="replace") as f:
        for line in f:
            if anchor is None:
                m = C64PY_STA_ANCHOR.search(line)
                if m:
                    anchor = int(m.group(1))
            m = C64PY_BRANCH.match(line.strip())
            if not m or anchor is None:
                continue
            pc, cyc, _op, take, z, a, x, y = m.groups()
            cyc_i = int(cyc)
            if cyc_i <= anchor:
                continue
            pl = pc.lower()
            if pl not in WATCH_PCS:
                continue
            out.append((pl, cyc_i, int(take), int(z), a.lower(), x.lower(), y.lower()))
    if anchor is None:
        raise SystemExit(f"no STA_INDY anchor (eff=$4CF5) in {path}")
    return anchor, out


def iter_vice_branches(path: Path, anchor_cyc: int) -> Iterator[tuple[str, int, int, int, str, str, str]]:
    """Stream VICE trace: same tuple shape after anchor (uses VICE absolute cycles)."""
    with path.open("r", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = VICE_LINE.match(line)
            if not m:
                continue
            pc, b0, _b1, a, x, y, flags, cyc_s = m.groups()
            pc = pc.lower()
            cyc = int(cyc_s)
            if cyc <= anchor_cyc:
                continue
            if pc not in WATCH_PCS:
                continue
            op = int(b0, 16)
            if op not in (0xD0, 0xF0):
                continue
            z = z_from_vice_flags(flags)
            take = vice_take(op, z)
            if take < 0:
                continue
            yield (pc, cyc, take, z, a.lower(), x.lower(), y.lower())


def find_vice_anchor(path: Path) -> int:
    with path.open("r", errors="replace") as f:
        for line in f:
            m = VICE_STA_ANCHOR.match(line.rstrip())
            if m:
                return int(m.group(1))
    raise SystemExit(f"no VICE STA anchor (00fa A:CC X:4F) in {path}")


def last_010f_snapshot_before_cyc(path: Path, max_cyc: int) -> tuple[int, str] | None:
    """
    Last BRANCH_TRACE at $010F with cyc < max_cyc (c64py cycle before first mismatch).

    Returns (cyc, --debug-inject-map fragment) or None if no $010F seen before max_cyc.
    """
    best: tuple[int, str] | None = None
    with path.open("r", errors="replace") as f:
        for line in f:
            m = C64PY_BRANCH_010F.match(line.strip())
            if not m:
                continue
            cyc = int(m.group(1))
            if cyc >= max_cyc:
                continue
            _op, _rel, _tgt, _take, _z, a, x, y, p, d0, d1, d2, d3 = m.groups()[1:]
            frag = (
                f"a={a.lower()},x={x.lower()},y={y.lower()},p={p.lower()},"
                f"2d={d0.lower()},2e={d1.lower()},2f={d2.lower()},30={d3.lower()}"
            )
            if best is None or cyc > best[0]:
                best = (cyc, frag)
    return best


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--c64py-log", type=Path, required=True)
    ap.add_argument("--vice-trace", type=Path, default=None)
    ap.add_argument("--max-diff", type=int, default=30, help="max mismatch reports")
    ap.add_argument(
        "--inject-hint",
        action="store_true",
        help=(
            "On first pc/take/z mismatch, print c64py/VICE cycles, last $010F inject cycle "
            "before mismatch, and vice_trace_to_inject.py stub"
        ),
    )
    ap.add_argument(
        "--prefix-pc-counts",
        action="store_true",
        help="On first pc/take/z mismatch, print per-PC event counts for the matched prefix (c64py vs VICE)",
    )
    args = ap.parse_args()

    c_anchor, c_list = parse_c64py_branches(args.c64py_log)
    print(f"c64py anchor_cyc={c_anchor} branch_events_after_anchor={len(c_list)}")
    if c_list[:3]:
        print("  first 3:", c_list[:3])

    if args.vice_trace is None:
        print("(--vice-trace not set; skipping VICE comparison)")
        return 0

    if not args.vice_trace.is_file():
        print(f"missing {args.vice_trace}", file=sys.stderr)
        return 2

    v_anchor = find_vice_anchor(args.vice_trace)
    print(f"vice anchor_cyc={v_anchor} (first 00fa STA A:CC X:4F)")

    it = iter_vice_branches(args.vice_trace, v_anchor)
    diffs = 0
    v_prefix: list[tuple[str, int, int, int, str, str, str]] = []
    for i, c_ev in enumerate(c_list):
        try:
            v_ev = next(it)
        except StopIteration:
            print(f"END: VICE stream ended at comparison index {i}, c64py has more events")
            print(f"  next c64py: {c_ev}")
            return 1
        if (c_ev[0], c_ev[2], c_ev[3]) != (v_ev[0], v_ev[2], v_ev[3]):
            print(f"MISMATCH idx={i}  (matched prefix length = {i})")
            print(f"  c64py: pc={c_ev[0]} cyc={c_ev[1]} take={c_ev[2]} z={c_ev[3]} a={c_ev[4]} x={c_ev[5]} y={c_ev[6]}")
            print(f"  vice:  pc={v_ev[0]} cyc={v_ev[1]} take={v_ev[2]} z={v_ev[3]} a={v_ev[4]} x={v_ev[5]} y={v_ev[6]}")
            if (c_ev[2], c_ev[3]) == (v_ev[2], v_ev[3]):
                print(
                    "  note: take/z agree but PC differs (phase slip vs VICE). "
                    "Use --prefix-pc-counts: if per-PC totals match in the prefix, slip is ordering/timing, "
                    "not an extra branch at those PCs."
                )
            if args.prefix_pc_counts and diffs == 0:
                assert len(v_prefix) == i
                c_pc = Counter(e[0] for e in c_list[:i])
                v_pc = Counter(e[0] for e in v_prefix)
                print("--- prefix-pc-counts (matched events only, idx 0..i-1) ---")
                all_pcs = sorted(set(c_pc) | set(v_pc))
                for pc in all_pcs:
                    dc = c_pc.get(pc, 0) - v_pc.get(pc, 0)
                    extra = f"  delta_c64_minus_vice={dc:+d}" if dc else ""
                    print(f"  pc={pc}  c64py={c_pc.get(pc, 0):6d}  vice={v_pc.get(pc, 0):6d}{extra}")
            if args.inject_hint and diffs == 0:
                vc = v_ev[1]
                cc = c_ev[1]
                print("--- inject-hint (first pc/take/z mismatch) ---")
                print(f"FIRST_MISMATCH idx={i} c64py_cyc={cc} vice_cyc={vc}")
                print(
                    "# Mismatch PC is often $088A while VICE shows $010F (phase slip). "
                    "Debug inject runs at the *start* of the step when cycles >= N (see cpu.py)."
                )
                snap = last_010f_snapshot_before_cyc(args.c64py_log, cc)
                if snap is None:
                    print(
                        "c64py_cyc_last_010f_before_mismatch: (none — log missing BRANCH_TRACE @ $010F "
                        "with ZP fields before mismatch cycle; regenerate Bruce log with current cpu.py)"
                    )
                else:
                    c010f, map_frag = snap
                    print(f"c64py_cyc_last_010f_before_mismatch={c010f}")
                    print(
                        "# Suggested: inject at $010F boundary (regs+ZP from that log line); "
                        "add stack from VICE JSONL if needed:"
                    )
                    print(
                        f"python3 C64.py programs/BruceLee.prg --headless --turbo "
                        f"--max-cycles 13200000 --autoquit --rom-dir roms \\\n"
                        f"  --debug-inject-at-cycle {c010f} \\\n"
                        f"  --debug-inject-file /path/to/stack.inject \\\n"
                        f"  --debug-inject-map '{map_frag}'"
                    )
                print("# Alternate: first-mismatch cycle (may land at $088A, not $010F):")
                print(
                    f"python3 scripts/vice_trace_to_inject.py --file {args.vice_trace} "
                    f"--match-vice-cycle {vc} --fast-rg \\\n"
                    f"  --zp-2d-to-30 <2d>,<2e>,<2f>,<30_from_VICE_monitor> \\\n"
                    f"  --inject-cycle {cc} --print-c64py-command programs/BruceLee.prg /path/to/roms"
                )
                print("# Then re-run compare with a new Bruce log after inject (same --max-cycles / VIC mode).")
            diffs += 1
            if diffs >= args.max_diff:
                print(f"(stopped after {args.max_diff} mismatches)")
                return 1
        else:
            v_prefix.append(v_ev)
            if (c_ev[4], c_ev[5], c_ev[6]) != (v_ev[4], v_ev[5], v_ev[6]):
                # Same branch outcome; registers differ (still interesting)
                pass

    rest = sum(1 for _ in it)
    if rest:
        print(f"END: c64py ended at index {len(c_list)}, VICE had ~{rest} more branch events (approx)")
        return 1

    print(f"OK: compared {len(c_list)} branch events; pc/take/z sequences match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
