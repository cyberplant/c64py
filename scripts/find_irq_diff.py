#!/usr/bin/env python3
"""Find where Python and Rust diverge near the IRQ handler."""
import sys

py_file = sys.argv[1] if len(sys.argv) > 1 else '/tmp/py_2079k.trace'
rs_file = sys.argv[2] if len(sys.argv) > 2 else '/tmp/rs_2079k.trace'

py_lines = [l for l in open(py_file) if l.startswith('.C:')]
rs_lines = [l for l in open(rs_file) if l.startswith('.C:')]


def normalize(line):
    return line.split(';')[0].rstrip()


# Find first divergence in PC (ignoring cycle count)
def get_pc(line):
    return line[3:7]


def get_cyc(line):
    try:
        return int(normalize(line).split()[-1])
    except ValueError:
        return -1


gap = 0
prev_gap = 0
for i, (py, rs) in enumerate(zip(py_lines, rs_lines)):
    py_cyc = get_cyc(py)
    rs_cyc = get_cyc(rs)
    new_gap = py_cyc - rs_cyc

    if get_pc(py) != get_pc(rs):
        print(f"PC divergence at instruction {i+1}:")
        for j in range(max(0, i-5), min(len(py_lines), i+8)):
            py2 = py_lines[j].rstrip() if j < len(py_lines) else ''
            rs2 = rs_lines[j].rstrip() if j < len(rs_lines) else ''
            prefix = '>>>' if j == i else '   '
            print(f"  {prefix} PY[{j+1}]: {py2}")
            print(f"  {prefix} RS[{j+1}]: {rs2}")
        break

    if new_gap != prev_gap and abs(new_gap - prev_gap) > 5:
        print(f"Large gap change at instruction {i+1}: {prev_gap} -> {new_gap} (+{new_gap-prev_gap})")
        print(f"  PY: {py.rstrip()}")
        print(f"  RS: {rs.rstrip()}")
        prev_gap = new_gap

print(f"\nTotal instructions compared: {i+1}")
print(f"Final cycle gap: {new_gap}")
