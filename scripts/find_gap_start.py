#!/usr/bin/env python3
"""Find where the cycle gap first appears between Python and Rust traces."""

py_lines = [l for l in open('/tmp/py_2079k.trace') if l.startswith('.C:')]
rs_lines = [l for l in open('/tmp/rs_2079k.trace') if l.startswith('.C:')]


def normalize(line):
    return line.split(';')[0].rstrip()


gap = 0
changes = 0
for i, (py, rs) in enumerate(zip(py_lines[:570000], rs_lines[:570000])):
    try:
        py_cyc = int(normalize(py).split()[-1])
        rs_cyc = int(normalize(rs).split()[-1])
        new_gap = py_cyc - rs_cyc
        if new_gap != gap:
            changes += 1
            if changes <= 3:
                print(f"Gap change #{changes} at instruction {i+1}: {gap} -> {new_gap} (+{new_gap-gap})")
                for j in range(max(0, i-5), min(len(py_lines), i+2)):
                    py2 = py_lines[j].rstrip() if j < len(py_lines) else ''
                    rs2 = rs_lines[j].rstrip() if j < len(rs_lines) else ''
                    prefix = '>>>' if j == i else '   '
                    print(f"  {prefix} PY[{j+1}]: {py2}")
                    print(f"  {prefix} RS[{j+1}]: {rs2}")
                print()
            gap = new_gap
    except (ValueError, IndexError):
        pass

print(f"Total gap changes: {changes}")
print(f"Final gap: {gap} cycles")
