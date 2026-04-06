#!/usr/bin/env python3
"""Convert cpu.py _execute_opcode arms to ``rust/c64py-core/src/execute_opcode_match.rs``.

Writes a full ``match opcode { ... }`` included from ``c64_cpu.rs``.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CPU = ROOT / "cpu.py"
OUT = ROOT / "rust" / "c64py-core" / "src" / "execute_opcode_match.rs"


def xlat_line(line: str) -> str:
    s = line.strip()
    if not s or s.startswith('"""') or s.startswith("#"):
        return ""
    if s.startswith("else:"):
        return ""
    if "#" in s:
        s = s.split("#")[0].rstrip()
    if "halt_msg" in s or "self.interface" in s or s.startswith("print("):
        return ""
    if re.match(r"if 0x[0-9A-Fa-f]+ <=", s) or re.match(r"elif 0x", s):
        return ""
    if s.startswith("elif opcode == ") or s.startswith("if opcode == "):
        return ""
    if s.startswith("elif opcode in "):
        return ""
    if s.startswith("# "):
        return "            // " + s[2:]
    s = re.sub(
        r"self\._read_word\(self\.state\.pc \+ (\d+)\)",
        r"read_word_at(mem, cpu.pc.wrapping_add(\1))",
        s,
    )
    s = re.sub(
        r"self\._read_word\(\(self\.state\.pc \+ 1\) & 0xFFFF\)",
        r"read_word_at(mem, cpu.pc.wrapping_add(1))",
        s,
    )
    s = re.sub(r"self\._read_word\(([a-zA-Z_][a-zA-Z0-9_]*)\)", r"read_word_at(mem, \1)", s)
    s = re.sub(
        r"self\._mr\(\(self\.state\.pc \+ 1\) & 0xFFFF\)",
        r"mr(mem, cpu, cpu.pc.wrapping_add(1))",
        s,
    )
    s = re.sub(r"read_word\(mem, cpu, cpu\.pc \+ (\d+)\)", r"read_word_at(mem, cpu.pc.wrapping_add(\1))", s)
    s = re.sub(r"mr\(mem, cpu, cpu\.pc \+ (\d+)\)", r"mr(mem, cpu, cpu.pc.wrapping_add(\1))", s)
    s = re.sub(r"mr\(mem, cpu, \(cpu\.pc \+ 1\) & 0xFFFF\)", r"mr(mem, cpu, cpu.pc.wrapping_add(1))", s)
    s = re.sub(r"0x100 \+ cpu\.sp", "0x0100u16.wrapping_add(cpu.sp as u16)", s)
    s = re.sub(r"0x100\+cpu\.sp", "0x0100u16.wrapping_add(cpu.sp as u16)", s)
    s = re.sub(r"\bself\.state\.pc\b", "cpu.pc", s)
    s = re.sub(r"\bself\.state\.a\b", "cpu.a", s)
    s = re.sub(r"\bself\.state\.x\b", "cpu.x", s)
    s = re.sub(r"\bself\.state\.y\b", "cpu.y", s)
    s = re.sub(r"\bself\.state\.sp\b", "cpu.sp", s)
    s = re.sub(r"\bself\.state\.p\b", "cpu.p", s)
    s = re.sub(r"\bself\.state\.stopped\b", "cpu.stopped", s)
    s = re.sub(r"self\._mr\(", "mr(mem, cpu, ", s)
    s = re.sub(r"self\._mw\(", "mw(mem, cpu, ", s)
    s = re.sub(r"self\._read_word\(", "read_word_at(mem, ", s)
    s = re.sub(r"self\._update_flags\(", "update_nz(cpu, ", s)
    s = re.sub(r"self\._get_flag\(0x01\)", "(cpu.p & 0x01) != 0", s)
    s = re.sub(r"self\._get_flag\(0x02\)", "(cpu.p & 0x02) != 0", s)
    s = re.sub(r"self\._get_flag\(0x40\)", "(cpu.p & 0x40) != 0", s)
    s = re.sub(r"self\._get_flag\(0x80\)", "(cpu.p & 0x80) != 0", s)
    s = re.sub(r"self\._set_flag\(0x01,\s*", "set_flag(cpu, 0x01, ", s)
    s = re.sub(r"self\._set_flag\(0x02,\s*", "set_flag(cpu, 0x02, ", s)
    s = re.sub(r"self\._set_flag\(0x04,\s*", "set_flag(cpu, 0x04, ", s)
    s = re.sub(r"self\._set_flag\(0x08,\s*", "set_flag(cpu, 0x08, ", s)
    s = re.sub(r"self\._set_flag\(0x40,\s*", "set_flag(cpu, 0x40, ", s)
    s = re.sub(r"self\._set_flag\(0x80,\s*", "set_flag(cpu, 0x80, ", s)
    s = re.sub(r"self\._page_crossed\(", "page_crossed(", s)
    s = re.sub(r"self\._adc_finish\(", "adc_finish(cpu, ", s)
    s = re.sub(r"self\._rmw_dummy_write_6510\(", "rmw_dummy_6510(mem, cpu, ", s)
    s = re.sub(r"return self\._([a-z0-9_]+)\(\)", r"return \1(cpu, mem);", s)
    s = s.replace("True", "true").replace("False", "false")
    s = re.sub(r"\s+and\s+", " && ", s)
    s = re.sub(r"\(base \+ cpu\.x\) & 0xFFFF", "base.wrapping_add(cpu.x as u16)", s)
    s = re.sub(r"\(base \+ cpu\.y\) & 0xFFFF", "base.wrapping_add(cpu.y as u16)", s)
    s = re.sub(r"\bcpu\.pc\s*\+\s*(\d+)\b", r"cpu.pc.wrapping_add(\1)", s)
    # return N if cond else M
    m = re.match(r"return (\d+) if (.+) else (\d+)", s)
    if m:
        a, cond, b = m.group(1), m.group(2), m.group(3)
        s = f"return if {cond} {{ {a} }} else {{ {b} }};"
    elif s.startswith("return ") and not s.endswith(";"):
        s = s + ";"
    # assignment -> let (first use in arm — use let mut for zp_addr, base, etc.)
    if re.match(r"^[a-z_][a-z0-9_]*\s*=", s) and not re.match(r"^return\s", s):
        name = s.split("=", 1)[0].strip()
        rhs = s.split("=", 1)[1].strip()
        if rhs.endswith(";"):
            rhs = rhs[:-1].strip()
        s = f"let {name} = {rhs};"
    s = re.sub(r"\(cpu\.pc \+ (\d+)\) & 0xFFFF", r"cpu.pc.wrapping_add(\1)", s)
    if s.startswith("cpu.") and "=" in s and not s.endswith(";"):
        s = s + ";"
    if s.startswith("let ") and not s.endswith(";"):
        s = s + ";"
    mtern_let = re.match(
        r"^let ([a-z_][a-z0-9_]*)\s*=\s*(.+?)\s+if\s+(.+?)\s+else\s+(.+);$",
        s,
    )
    if mtern_let:
        name, a, cond, b = (
            mtern_let.group(1),
            mtern_let.group(2).strip(),
            mtern_let.group(3).strip(),
            mtern_let.group(4).strip(),
        )
        s = f"let {name}: u8 = if {cond} {{ {a} }} else {{ {b} }};"
    s = re.sub(r"mr\(mem, cpu, zp_addr\)", "mr(mem, cpu, u16::from(zp_addr))", s)
    s = re.sub(r"mr\(mem, cpu, zp_ptr\)", "mr(mem, cpu, u16::from(zp_ptr))", s)
    s = re.sub(
        r"let result = cpu\.a - value - \(1 - carry\);",
        "let result: i32 = i32::from(cpu.a) - i32::from(value) - (1 - i32::from(carry));",
        s,
    )
    s = re.sub(
        r"set_flag\(cpu, 0x40, \(\(cpu\.a \^ value\) & 0x80\) != 0 && \(\(cpu\.a \^ result\) & 0x80\) != 0\);",
        "set_flag(cpu, 0x40, ((i32::from(cpu.a) ^ i32::from(value)) & 0x80) != 0 && ((i32::from(cpu.a) ^ result) & 0x80) != 0);",
        s,
    )
    if s and not s.endswith(";") and not s.startswith("//"):
        if (
            s.startswith("mw(")
            or s.startswith("mr(")
            or s.startswith("set_flag(")
            or s.startswith("adc_finish(")
            or s.startswith("update_nz(")
            or s.startswith("rmw_dummy_6510(")
        ):
            s = s + ";"
    s = re.sub(
        r"mr\(mem, cpu, 0x100 \+ cpu\.sp\)",
        "mr(mem, cpu, 0x0100u16.wrapping_add(cpu.sp as u16))",
        s,
    )
    return "            " + s if s else ""


def main() -> None:
    lines = CPU.read_text().splitlines()
    start = next(i for i, L in enumerate(lines) if L.strip().startswith("if opcode == 0xA9:"))
    end = next(i for i, L in enumerate(lines) if i > start and L.startswith("    def _brk(self)"))
    chunk = lines[start:end]
    out: list[str] = []
    seen: set[int] = set()
    i = 0
    while i < len(chunk):
        line = chunk[i]
        m = re.match(r"\s*if opcode == (0x[0-9A-Fa-f]+):", line)
        m2 = re.match(r"\s*elif opcode == (0x[0-9A-Fa-f]+):", line)
        m3 = re.match(r"\s*elif opcode in (\[[^\]]+\]):", line)
        if m or m2 or m3:
            if m:
                opc = int(m.group(1), 16)
            elif m2:
                opc = int(m2.group(1), 16)
            else:
                lst = m3.group(1)
                opcs = [int(x, 16) for x in re.findall(r"0x[0-9A-Fa-f]+", lst)]
                i += 1
                body_lines = []
                while i < len(chunk) and not re.match(r"\s*(elif|else)\s", chunk[i]):
                    xl = xlat_line(chunk[i])
                    if xl.strip():
                        body_lines.append(xl)
                    i += 1
                body = "\n".join(body_lines)
                for opc in opcs:
                    if opc in seen:
                        continue
                    seen.add(opc)
                    out.append(f"            {opc:#04x} => {{\n{body}\n            }},\n")
                continue
            i += 1
            body_lines = []
            while i < len(chunk) and not re.match(r"\s*(elif|else)\s", chunk[i]):
                xl = xlat_line(chunk[i])
                if xl.strip():
                    body_lines.append(xl)
                i += 1
            body = "\n".join(body_lines)
            if opc in seen:
                continue
            seen.add(opc)
            out.append(f"            {opc:#04x} => {{\n{body}\n            }},\n")
            continue
        if line.strip().startswith("else:"):
            i += 1
            body_lines = []
            while i < len(chunk):
                xl = xlat_line(chunk[i])
                if xl.strip():
                    body_lines.append(xl)
                i += 1
            body = "\n".join(body_lines)
            out.append(f"            _ => {{\n{body}\n            }},\n")
            break
        i += 1
    body = "".join(out)
    body = re.sub(
        r"if \(base & 0xFF00\) != \(addr & 0xFF00\):\s*\n\s*return (\d+);\s*\n\s*return (\d+);",
        r"return if (base & 0xFF00) != (addr & 0xFF00) { \1 } else { \2 };",
        body,
    )
    body = body.replace("mw(mem, cpu, zp_addr,", "mw(mem, cpu, zp_addr as u16,")
    body = body.replace(
        "mr(mem, cpu, (zp_addr + 1) & 0xFF)",
        "mr(mem, cpu, zp_addr.wrapping_add(1) as u16)",
    )
    body = body.replace(
        "mr(mem, cpu, (zp_ptr + 1) & 0xFF)",
        "mr(mem, cpu, zp_ptr.wrapping_add(1) as u16)",
    )
    body = re.sub(
        r"let addr = mr\(mem, cpu, u16::from\(zp_addr\)\) \| \(mr\(mem, cpu, zp_addr\.wrapping_add\(1\) as u16\) << 8\);",
        "let addr: u16 = u16::from(mr(mem, cpu, u16::from(zp_addr))) | (u16::from(mr(mem, cpu, zp_addr.wrapping_add(1) as u16)) << 8);",
        body,
    )
    body = re.sub(
        r"set_flag\(cpu, 0x40, \(\(cpu\.a \^ value\) & 0x80\) != 0 && \(\(cpu\.a \^ result\) & 0x80\) != 0\);",
        "set_flag(cpu, 0x40, ((i32::from(cpu.a) ^ i32::from(value)) & 0x80) != 0 && ((i32::from(cpu.a) ^ result) & 0x80) != 0);",
        body,
    )
    body = re.sub(r"cpu\.a = result & 0xFF;", "cpu.a = (result & 0xFF) as u8;", body)
    body = body.replace(
        "let addr = addr_low | (addr_high << 8);",
        "let addr: u16 = u16::from(addr_low) | (u16::from(addr_high) << 8);",
    )
    body = body.replace(
        "let base = addr_low | (addr_high << 8);",
        "let base: u16 = u16::from(addr_low) | (u16::from(addr_high) << 8);",
    )
    body = body.replace(
        "let base = mr(mem, cpu, u16::from(zp_addr)) | (mr(mem, cpu, zp_addr.wrapping_add(1) as u16) << 8);",
        "let base: u16 = u16::from(mr(mem, cpu, u16::from(zp_addr))) | (u16::from(mr(mem, cpu, zp_addr.wrapping_add(1) as u16)) << 8);",
    )
    OUT.write_text("match opcode {\n" + body + "}\n")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
