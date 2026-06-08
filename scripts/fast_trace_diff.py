#!/usr/bin/env python3
"""Fast line-by-line trace diff for rust vs VICE, starting at given byte offsets.

Strips cycle columns and VICE `#1 (Trace exec ...)` context lines, then compares
normalized (PC, bytes, mnemonic, A, X, Y, SP, flags) on each `.C:` line.

Usage:
    scripts/fast_trace_diff.py OURS_PATH OURS_OFFSET VICE_PATH VICE_OFFSET [--max-lines N]
"""
from __future__ import annotations
import argparse
import re
import sys


RUST_CYCLE_RE = re.compile(r"\s+\d+\s*;\s*rust\s*$")
VICE_CYCLE_RE = re.compile(r"\s+\d+\s*$")

# `.C:xxxx  BB BB BB    mnemonic args  - A:xx X:xx Y:xx SP:xx flags  cycle`
# Keep: PC (4 hex), opcode bytes (up to 3), and everything from `- A:` onwards (minus cycles)
LINE_RE = re.compile(
    r"^(\.C:[0-9a-fA-F]{4})\s+"
    r"([0-9A-Fa-f]{2}(?:\s+[0-9A-Fa-f]{2}){0,2})\s+"
    r"\S.*?(\s-\s.*)$"
)


def canonical(raw: str, is_rust: bool) -> str:
    """Extract PC + opcode-bytes + register state; drop mnemonic and cycles."""
    line = raw.rstrip("\n")
    if is_rust:
        line = RUST_CYCLE_RE.sub("", line)
    else:
        line = VICE_CYCLE_RE.sub("", line)
    m = LINE_RE.match(line)
    if not m:
        return line.strip()
    pc, bytes_, regs = m.group(1), m.group(2), m.group(3)
    # Normalize hex case and whitespace
    bytes_norm = " ".join(tok.upper() for tok in bytes_.split())
    return f"{pc.lower()} {bytes_norm} {regs.strip()}"


def norm_rust(line: str) -> str:
    return canonical(line, is_rust=True)


def norm_vice(line: str) -> str:
    return canonical(line, is_rust=False)


def rust_lines(path: str, offset: int):
    f = open(path, "rb")
    f.seek(offset)
    # Align to start of current line
    while True:
        data = f.read(16_384)
        if not data:
            return
        for raw in data.splitlines(keepends=True):
            yield raw.decode("latin-1", errors="replace")
            # Yield stays a generator; done inside generator below
        # simpler: re-read line by line after seek
        break

    # unreached


def iter_lines(path: str, offset: int):
    f = open(path, "rb")
    f.seek(offset)
    buf = b""
    while True:
        chunk = f.read(1 << 20)  # 1 MiB
        if not chunk:
            if buf:
                yield buf.decode("latin-1", errors="replace")
            return
        buf += chunk
        parts = buf.split(b"\n")
        buf = parts[-1]
        for p in parts[:-1]:
            yield p.decode("latin-1", errors="replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ours_path")
    ap.add_argument("ours_offset", type=int)
    ap.add_argument("vice_path")
    ap.add_argument("vice_offset", type=int)
    ap.add_argument("--max-lines", type=int, default=50_000_000)
    ap.add_argument("--context", type=int, default=20)
    ap.add_argument("--max-reg-diverge", type=int, default=10,
                    help="Print at most N register-only divergences inline")
    ap.add_argument("--max-pc-diverge", type=int, default=5,
                    help="Print at most N PC/opcode divergences inline")
    ap.add_argument("--stop-after-pc-diverge", type=int, default=3,
                    help="Stop comparison after N PC/opcode divergences (control flow split)")
    args = ap.parse_args()

    ours_it = iter_lines(args.ours_path, args.ours_offset)
    vice_it = iter_lines(args.vice_path, args.vice_offset)

    # Skip VICE non-.C: lines
    def vice_exec_only():
        for ln in vice_it:
            if ln.startswith(".C:"):
                yield ln

    def ours_exec_only():
        for ln in ours_it:
            if ln.startswith(".C:"):
                yield ln

    v = vice_exec_only()
    o = ours_exec_only()
    prev_o = []
    prev_v = []
    n = 0
    divergences = []  # (line_index, ours, vice)
    pc_diverge_count = 0
    last_same_pc = 0
    while n < args.max_lines:
        try:
            lo = next(o)
        except StopIteration:
            print(f"[ours trace ended at line {n}]")
            break
        try:
            lv = next(v)
        except StopIteration:
            print(f"[vice trace ended at line {n}]")
            break
        no = norm_rust(lo)
        nv = norm_vice(lv)
        n += 1
        if no == nv:
            if len(prev_o) >= args.context:
                prev_o.pop(0); prev_v.pop(0)
            prev_o.append(no); prev_v.append(nv)
            last_same_pc = n
            continue
        # Same PC but different registers? Or different PC entirely?
        opc = no.split()[0]
        vpc = nv.split()[0]
        same_pc = (opc == vpc) and (no.split()[1:3] == nv.split()[1:3])
        if not same_pc:
            pc_diverge_count += 1
            if pc_diverge_count <= args.max_pc_diverge:
                print(f"\n### PC/opcode DIVERGENCE #{pc_diverge_count} at line {n} ###")
                print(f"OURS: {no}")
                print(f"VICE: {nv}")
                if pc_diverge_count == 1:
                    print("--- Context before ---")
                    for a in prev_o[-args.context:]:
                        print(f"  = {a}")
            if pc_diverge_count >= args.stop_after_pc_diverge:
                print(f"\n[stopping after {pc_diverge_count} PC divergences]")
                break
        else:
            divergences.append((n, no, nv))
            if len(divergences) <= args.max_reg_diverge:
                print(f"\n--- Register-only divergence #{len(divergences)} at line {n} ---")
                print(f"OURS: {no}")
                print(f"VICE: {nv}")
    print("\n=== SUMMARY ===")
    print(f"Lines compared:              {n}")
    print(f"Register-only divergences:   {len(divergences)}")
    print(f"PC/opcode divergences:       {pc_diverge_count}")
    print(f"Last fully-matching line:    {last_same_pc}")
    if divergences:
        # Bucket register divergences by PC
        from collections import Counter
        pcs = Counter(d[1].split()[0] for d in divergences)
        print("Top PCs with register divergences:")
        for pc, c in pcs.most_common(20):
            print(f"   {pc}: {c}")


if __name__ == "__main__":
    main()
