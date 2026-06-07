#!/usr/bin/env python3
"""Normalize cycle counts in VICE trace logs by subtracting an offset."""
import sys
import re

# Offset to subtract (VICE start cycle - c64py start cycle)
# Usage: cat file.log | python normalize_cycles.py [offset]
OFFSET = int(sys.argv[1]) if len(sys.argv) > 1 else (225234196 - 2112847)

pattern = re.compile(r'^(.+\s+)(\d+)$')

for line in sys.stdin:
    line = line.rstrip('\n')
    m = pattern.match(line)
    if m:
        prefix = m.group(1)
        cycles = int(m.group(2))
        new_cycles = cycles - OFFSET
        print(f"{prefix}{new_cycles}")
    else:
        print(line)
