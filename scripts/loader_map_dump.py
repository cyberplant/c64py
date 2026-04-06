#!/usr/bin/env python3
"""
Static analysis of Bruce Lee (and similar) PRG: disassemble driver + on-disk helper,
scan for CIA/VIC/raster operands, locate ZP pointer traffic.

Usage:
  python3 scripts/loader_map_dump.py [path/to/game.prg]

Default PRG: programs/BruceLee.prg relative to repo root.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Allow `python3 scripts/loader_map_dump.py` (no package)
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from disasm6502 import disasm_range  # noqa: E402


def prg_payload(path: Path) -> tuple[int, bytes]:
    data = path.read_bytes()
    if len(data) < 3:
        raise ValueError("PRG too small")
    load = data[0] | (data[1] << 8)
    return load, data[2:]


def addr_in_disasm_line(line: str) -> set[int]:
    """Extract $XXXX addresses from a disasm line."""
    return {int(m.group(1), 16) for m in re.finditer(r"\$([0-9A-Fa-f]{4})\b", line)}


def scan_timing_operands(lines: list[str]) -> list[str]:
    hits: list[str] = []
    watch = {
        0xDC0D: "CIA1 ICR / IRQ ack",
        0xDC04: "CIA1 TBLO",
        0xDC05: "CIA1 TBHI",
        0xDC06: "CIA1 TA LO",
        0xDC07: "CIA1 TA HI",
        0xD012: "VIC raster (read)",
        0xD011: "VIC control / raster high bit",
        0xDD0D: "CIA2 ICR",
    }
    for line in lines:
        for a in addr_in_disasm_line(line):
            if a in watch:
                hits.append(f"  {line.strip()}  ; {watch[a]}")
    return hits


def main() -> None:
    ap = argparse.ArgumentParser(description="Static loader map from C64 PRG")
    ap.add_argument(
        "prg",
        nargs="?",
        default=str(Path(__file__).resolve().parents[1] / "programs" / "BruceLee.prg"),
        help="Path to .prg",
    )
    args = ap.parse_args()
    prg_path = Path(args.prg)
    load, raw = prg_payload(prg_path)

    def dump_region(addr_lo: int, addr_hi: int, label: str) -> list[str]:
        so = addr_lo - load
        eo = addr_hi - load + 1
        if so < 0 or eo > len(raw) or so >= eo:
            print(f"\n## {label}\n(not in PRG file; load=${load:04X}, file covers ${load:04X}-${load + len(raw) - 1:04X})\n")
            return []
        print(f"\n## {label}  (${addr_lo:04X}-${addr_hi:04X} in file)\n")
        lines = disasm_range(raw, load, so, eo)
        for ln in lines:
            print(ln)
        return lines

    print(f"# Loader static map: {prg_path.name}  load=${load:04X}  len={len(raw)}")

    all_lines: list[str] = []
    all_lines.extend(dump_region(0x0840, 0x08AF, "Driver outer loop / table step ($0840-$08AF)"))
    # Align to in-file STA ($2D),Y ($91 $2D) at $0918 (same bytes as RAM $00FA after relocation).
    all_lines.extend(dump_region(0x0918, 0x0980, "On-disk helper aligned at STA ($2D),Y ($0918-$0980)"))

    print("\n## ZP stores to $2D-$30 in listed regions (operand match)\n")
    for line in all_lines:
        if re.search(r"\$2[DEF][\s,)]|\$30\b", line) and ("STA" in line or "STX" in line or "STY" in line or "INC" in line or "DEC" in line):
            print(line)

    print("\n## Timing / IRQ-related operands in listed regions\n")
    th = scan_timing_operands(all_lines)
    if th:
        for h in th:
            print(h)
    else:
        print("(none in these windows — does not rule out KERNAL or code outside the dump)")

    print("\n## Raw byte search (full PRG): LDA $DC0D, LDA $D012, BIT $DC0D, …\n")
    patterns = [
        (bytes([0xAD, 0x0D, 0xDC]), "LDA $DC0D"),
        (bytes([0x2C, 0x0D, 0xDC]), "BIT $DC0D"),
        (bytes([0xAD, 0x12, 0xD0]), "LDA $D012"),
        (bytes([0xAD, 0x11, 0xD0]), "LDA $D011"),
        (bytes([0xAD, 0x0D, 0xDD]), "LDA $DD0D"),
    ]
    for pat, name in patterns:
        idx = 0
        found = False
        while True:
            j = raw.find(pat, idx)
            if j < 0:
                break
            found = True
            print(f"  {name} at file offset +{j:05d}  -> PRG addr ${load + j:04X}")
            idx = j + 1
        if not found:
            print(f"  {name}: not found")


if __name__ == "__main__":
    main()
