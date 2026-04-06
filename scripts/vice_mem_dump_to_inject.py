#!/usr/bin/env python3
"""
Turn VICE text-monitor memory listings into c64py --debug-inject-file lines.

Parses blocks like:
  >C:0100  fd fc fb fa  ...
  >C:0110  ...

Also accepts whole capture logs / JSONL: use --jsonl to read "response" fields from
mem_dump events (optionally --match-command substring).

Output: one "addr=byte" per line (hex, lowercase), suitable for --debug-inject-file.

Example:
  python3 scripts/vice_mem_dump_to_inject.py --low 0100 --high 01ff capture.log > stack.inject
  python3 C64.py ... --debug-inject-at-cycle 12794852 --debug-inject-file stack.inject \\
      --debug-inject-map a=d6,x=da,...
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Start of a VICE memory line; capture base address and following hex bytes.
MEM_LINE_RE = re.compile(
    r"^>C:([0-9a-fA-F]{4})\s+((?:[0-9a-fA-F]{2}\s+)+)",
    re.MULTILINE,
)


def parse_vice_mem_block(text: str) -> dict[int, int]:
    """Map address -> byte from all >C: lines in text."""
    mem: dict[int, int] = {}
    for m in MEM_LINE_RE.finditer(text):
        base = int(m.group(1), 16)
        hexpart = m.group(2).split()
        for i, hx in enumerate(hexpart):
            mem[(base + i) & 0xFFFF] = int(hx, 16)
    return mem


def extract_jsonl_responses(path: Path, *, cmd_substr: str | None) -> str:
    chunks: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "mem_dump":
                continue
            cmd = obj.get("command", "")
            if cmd_substr and cmd_substr.lower() not in cmd.lower():
                continue
            chunks.append(str(obj.get("response", "")))
    return "\n".join(chunks)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "input",
        nargs="?",
        type=Path,
        help="Text file (VICE log); default stdin",
    )
    ap.add_argument("--low", default="0100", help="First address (hex) to emit")
    ap.add_argument("--high", default="01ff", help="Last address (hex) to emit")
    ap.add_argument(
        "--jsonl",
        action="store_true",
        help="Input is JSONL from vice_monitor_client.py; concatenate mem_dump responses",
    )
    ap.add_argument(
        "--match-command",
        default="",
        help="With --jsonl: only mem_dump whose command contains this substring (e.g. '0100 01ff')",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="Exit 2 if any address in --low..--high is missing from the parsed dump.",
    )
    args = ap.parse_args()

    lo = int(args.low, 16) & 0xFFFF
    hi = int(args.high, 16) & 0xFFFF
    if hi < lo:
        print("ERROR: high < low", file=sys.stderr)
        return 2

    if args.input is None or str(args.input) == "-":
        text = sys.stdin.read()
    else:
        if not args.input.is_file():
            print(f"ERROR: not a file: {args.input}", file=sys.stderr)
            return 2
        if args.jsonl:
            text = extract_jsonl_responses(
                args.input,
                cmd_substr=args.match_command or None,
            )
        else:
            text = args.input.read_text(encoding="utf-8", errors="replace")

    mem = parse_vice_mem_block(text)
    if not mem:
        print("WARNING: no >C:xxxx memory lines parsed", file=sys.stderr)

    missing: list[int] = []
    for addr in range(lo, hi + 1):
        if addr in mem:
            print(f"{addr:04x}={mem[addr]:02x}")
        else:
            missing.append(addr)

    if missing:
        print(
            f"# {len(missing)} missing bytes in range {lo:04x}-{hi:04x} (not emitted)",
            file=sys.stderr,
        )
    if args.strict and missing:
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
