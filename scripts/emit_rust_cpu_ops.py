#!/usr/bin/env python3
"""Emit rust/c64py-core/src/cpu_ops_generated.rs from cpu.py helper methods.

Run from repo root:
  python3 scripts/emit_rust_cpu_ops.py

Translates CPU6502 methods _lda_imm .. _bit_abs and _execute_opcode dispatch
using line-oriented regex (best-effort; intended to match c64py's Python core).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CPU_PY = ROOT / "cpu.py"
OUT = ROOT / "rust" / "c64py-core" / "src" / "cpu_ops_generated.rs"


def extract_method(src: str, name: str) -> str:
    pat = rf"    def {re.escape(name)}\(self\) -> int:\s*\n"
    m = re.search(pat, src)
    if not m:
        raise SystemExit(f"missing {name}")
    start = m.end()
    rest = src[start:]
    if rest.lstrip().startswith('"""'):
        dq = rest.index('"""') + 3
        dq2 = rest.index('"""', dq)
        rest = rest[dq2 + 3 :].lstrip("\n")
    # next method at same indent
    m2 = re.search(r"\n    def _[a-z]", rest)
    if not m2:
        body = rest
    else:
        body = rest[: m2.start()]
    lines = []
    for line in body.splitlines():
        if not line.strip():
            continue
        if line.strip().startswith('"""'):
            continue
        lines.append(line)
    return "\n".join(lines)


def xlat_block(body: str) -> str:
    out = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("#"):
            out.append(f"    // {s[1:].strip()}")
            continue
        if "#" in s and not s.strip().startswith("#"):
            s = s.split("#")[0].rstrip()
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
        s = re.sub(r"self\._read_word\(0xFFFE\)", r"read_word_at(mem, 0xFFFE)", s)
        s = re.sub(
            r"self\._mr\(\(self\.state\.pc \+ 1\) & 0xFFFF\)",
            r"mr(mem, cpu, cpu.pc.wrapping_add(1))",
            s,
        )
        s = re.sub(r"\(self\.state\.pc \+ (\d+)\) & 0xFFFF", r"cpu.pc.wrapping_add(\1)", s)
        s = re.sub(r"self\.state\.pc \+ (\d+)", r"cpu.pc.wrapping_add(\1)", s)
        s = re.sub(r"self\.state\.pc", "cpu.pc", s)
        s = re.sub(r"self\.state\.a", "cpu.a", s)
        s = re.sub(r"self\.state\.x", "cpu.x", s)
        s = re.sub(r"self\.state\.y", "cpu.y", s)
        s = re.sub(r"self\.state\.sp", "cpu.sp", s)
        s = re.sub(r"self\.state\.p", "cpu.p", s)
        s = re.sub(r"self\.state\.stopped", "cpu.stopped", s)
        s = re.sub(r"0x100 \+ cpu\.sp", "0x0100u16.wrapping_add(cpu.sp as u16)", s)
        s = re.sub(r"0x100\+cpu\.sp", "0x0100u16.wrapping_add(cpu.sp as u16)", s)
        s = re.sub(r"self\._mr\(", "mr(mem, cpu, ", s)
        s = re.sub(r"self\._mw\(", "mw(mem, cpu, ", s)
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
        s = re.sub(r"return self\._branch\(not self\._get_flag\(0x01\)\)", "return branch(cpu, mem, (cpu.p & 0x01) == 0);", s)
        s = re.sub(r"return self\._branch\(self\._get_flag\(0x01\)\)", "return branch(cpu, mem, (cpu.p & 0x01) != 0);", s)
        s = re.sub(r"return self\._branch\(not self\._get_flag\(0x02\)\)", "return branch(cpu, mem, (cpu.p & 0x02) == 0);", s)
        s = re.sub(r"return self\._branch\(self\._get_flag\(0x02\)\)", "return branch(cpu, mem, (cpu.p & 0x02) != 0);", s)
        s = re.sub(r"return self\._branch\(not self\._get_flag\(0x40\)\)", "return branch(cpu, mem, (cpu.p & 0x40) == 0);", s)
        s = re.sub(r"return self\._branch\(self\._get_flag\(0x40\)\)", "return branch(cpu, mem, (cpu.p & 0x40) != 0);", s)
        s = re.sub(r"return self\._branch\(not self\._get_flag\(0x80\)\)", "return branch(cpu, mem, (cpu.p & 0x80) == 0);", s)
        s = re.sub(r"return self\._branch\(self\._get_flag\(0x80\)\)", "return branch(cpu, mem, (cpu.p & 0x80) != 0);", s)
        s = re.sub(r"self\._branch\(", "branch(cpu, mem, ", s)
        s = re.sub(r"return branch\(cpu, mem, not \(cpu\.p & 0x01\) != 0\)", "return branch(cpu, mem, (cpu.p & 0x01) == 0)", s)
        s = re.sub(r"return branch\(cpu, mem, not \(cpu\.p & 0x02\) != 0\)", "return branch(cpu, mem, (cpu.p & 0x02) == 0)", s)
        s = re.sub(r"return branch\(cpu, mem, not \(cpu\.p & 0x40\) != 0\)", "return branch(cpu, mem, (cpu.p & 0x40) == 0)", s)
        s = re.sub(r"return branch\(cpu, mem, not \(cpu\.p & 0x80\) != 0\)", "return branch(cpu, mem, (cpu.p & 0x80) == 0)", s)
        s = re.sub(r"return self\._([a-z0-9_]+)\(\)", r"return \1(cpu, mem);", s)
        s = s.replace("True", "true").replace("False", "false")
        s = re.sub(r"\s\band\b\s", " && ", s)
        mtern = re.match(
            r"^([a-z_][a-z0-9_]*)\s*=\s*(.+?)\s+if\s+(.+?)\s+else\s+(.+)$",
            s,
        )
        if mtern:
            name, a, cond, b = (
                mtern.group(1),
                mtern.group(2).strip(),
                mtern.group(3).strip(),
                mtern.group(4).strip(),
            )
            s = f"let {name}: u8 = if {cond} {{ {a} }} else {{ {b} }};"
        s = re.sub(r"\(base \+ cpu\.x\) & 0xFFFF", "base.wrapping_add(cpu.x as u16)", s)
        s = re.sub(r"\(base \+ cpu\.y\) & 0xFFFF", "base.wrapping_add(cpu.y as u16)", s)
        s = re.sub(r"mr\(mem, cpu, zp_addr\)", "mr(mem, cpu, u16::from(zp_addr))", s)
        m = re.match(r"return (\d+) if (.+) else (\d+)", s)
        if m:
            a, cond, b = m.group(1), m.group(2), m.group(3)
            s = f"return if {cond} {{ {a} }} else {{ {b} }};"
        elif s.startswith("return ") and not s.endswith(";"):
            s = s + ";"
        if re.match(r"^[a-z_][a-z0-9_]*\s*=", s) and not re.match(r"^return\s", s):
            name = s.split("=", 1)[0].strip()
            rhs = s.split("=", 1)[1].strip().rstrip(";")
            typ = ""
            if name == "return_addr":
                typ = ": u16 "
            elif name in ("pc_high", "pc_low"):
                typ = ": u8 "
            elif name in ("addr", "base"):
                typ = ": u16 "
            elif name == "ret":
                typ = ": u16 "
            if name in ("pc_high", "pc_low"):
                s = f"let {name}{typ}= ({rhs}) as u8;"
            else:
                s = f"let {name}{typ}= {rhs};"
        elif s.startswith("cpu.") and "=" in s and not s.startswith("return") and not s.startswith("let "):
            if not s.endswith(";"):
                s = s + ";"
        elif (
            s.startswith("mw(")
            or s.startswith("set_flag(")
            or s.startswith("adc_finish(")
            or s.startswith("update_nz(")
            or s.startswith("rmw_dummy_6510(")
        ) and not s.endswith(";"):
            s = s + ";"
        s = re.sub(
            r"let result=\s*old_a \+ value \+ carry",
            "let result: u32 = u32::from(old_a) + u32::from(value) + u32::from(carry)",
            s,
        )
        s = re.sub(
            r"let result=\s*cpu\.a - value - \(1 - carry\)",
            "let result: i32 = i32::from(cpu.a) - i32::from(value) - (1 - i32::from(carry))",
            s,
        )
        s = re.sub(
            r"\(\(cpu\.a \^ value\) & 0x80\) != 0 && \(\(cpu\.a \^ result\) & 0x80\) != 0",
            "((i32::from(cpu.a) ^ i32::from(value)) & 0x80) != 0 && ((i32::from(cpu.a) ^ result) & 0x80) != 0",
            s,
        )
        if s.startswith("if ") and s.endswith(":"):
            cond = s[3:-1].strip()
            s = f"if {cond} {{"
        elif s.strip() == "else:":
            s = "} else {"
        out.append("    " + s)
    return "\n".join(out)


METHODS = [
    n
    for n in [
        "_brk",
        "_jmp_abs",
        "_jsr_abs",
        "_rts",
        "_lda_imm",
        "_lda_zp",
        "_lda_abs",
        "_sta_zp",
        "_sta_abs",
        "_lda_zpx",
        "_lda_absx",
        "_lda_absy",
        "_lda_indx",
        "_lda_indy",
        "_ldx_imm",
        "_ldx_zp",
        "_ldx_abs",
        "_ldy_imm",
        "_ldy_zp",
        "_ldy_abs",
        "_ldy_absx",
        "_ldy_zpx",
        "_sta_zpx",
        "_sta_absx",
        "_sta_absy",
        "_sta_indx",
        "_sta_indy",
        "_stx_zp",
        "_stx_abs",
        "_sty_zp",
        "_sty_abs",
        "_sty_zpx",
        "_adc_imm",
        "_adc_zp",
        "_adc_indx",
        "_adc_indy",
        "_adc_abs",
        "_adc_absx",
        "_adc_absy",
        "_sbc_imm",
        "_sbc_zp",
        "_sbc_abs",
        "_and_imm",
        "_and_zp",
        "_and_abs",
        "_and_absx",
        "_and_absy",
        "_ora_imm",
        "_ora_zp",
        "_ora_abs",
        "_ora_absy",
        "_ora_absx",
        "_eor_imm",
        "_eor_zp",
        "_eor_abs",
        "_eor_absx",
        "_eor_absy",
        "_eor_indy",
        "_cmp_imm",
        "_cmp_zp",
        "_cmp_abs",
        "_cpx_imm",
        "_cpx_zp",
        "_cpx_abs",
        "_cpy_imm",
        "_cpy_zp",
        "_cpy_abs",
        "_inc_zp",
        "_inc_abs",
        "_dec_zp",
        "_dec_abs",
        "_dec_absx",
        "_inx",
        "_iny",
        "_dex",
        "_dey",
        "_asl_acc",
        "_asl_zp",
        "_asl_zpx",
        "_asl_abs",
        "_asl_absx",
        "_lsr_acc",
        "_lsr_zp",
        "_lsr_abs",
        "_lsr_absx",
        "_lsr_zpx",
        "_rol_acc",
        "_rol_zp",
        "_rol_abs",
        "_rol_absx",
        "_ror_acc",
        "_ror_zp",
        "_ror_zpx",
        "_ror_abs",
        "_ror_absx",
        "_bcc",
        "_bcs",
        "_beq",
        "_bne",
        "_bpl",
        "_bmi",
        "_bvc",
        "_bvs",
        "_pha",
        "_pla",
        "_php",
        "_plp",
        "_tax",
        "_tay",
        "_txa",
        "_tya",
        "_tsx",
        "_txs",
        "_rti",
        "_bit_zp",
        "_bit_abs",
    ]
]


def rust_fn_name(py: str) -> str:
    return py[1:]  # drop _


def main() -> None:
    src = CPU_PY.read_text()
    chunks = []
    chunks.append("// AUTO-GENERATED by scripts/emit_rust_cpu_ops.py — included from c64_cpu.rs\n\n")
    for m in METHODS:
        body = extract_method(src, m)
        rb = xlat_block(body)
        name = rust_fn_name(m)
        chunks.append(f"pub fn {name}(cpu: &mut CpuState, mem: &mut C64MemoryMap<'_>) -> u32 {{\n{rb}\n}}\n\n")
    text = "".join(chunks)
    text = text.replace(
        "let addr: u16 = mr(mem, cpu, u16::from(zp_addr)) | (mr(mem, cpu, (zp_addr + 1) & 0xFF) << 8);",
        "let addr: u16 = u16::from(mr(mem, cpu, u16::from(zp_addr))) | (u16::from(mr(mem, cpu, u16::from(zp_addr.wrapping_add(1)))) << 8);",
    )
    text = text.replace(
        "let base: u16 = mr(mem, cpu, u16::from(zp_addr)) | (mr(mem, cpu, (zp_addr + 1) & 0xFF) << 8);",
        "let base: u16 = u16::from(mr(mem, cpu, u16::from(zp_addr))) | (u16::from(mr(mem, cpu, u16::from(zp_addr.wrapping_add(1)))) << 8);",
    )
    text = text.replace(
        "let ret: u16 = ((pc_high << 8) | pc_low);",
        "let ret: u16 = (u16::from(pc_high) << 8) | u16::from(pc_low);",
    )
    text = text.replace("mw(mem, cpu, zp_addr,", "mw(mem, cpu, zp_addr as u16,")
    text = text.replace("rmw_dummy_6510(mem, cpu, zp_addr,", "rmw_dummy_6510(mem, cpu, zp_addr as u16,")
    text = text.replace("cpu.a = result & 0xFF;", "cpu.a = (result & 0xFF) as u8;")
    text = text.replace(
        "let addr_low= mr(mem, cpu, u16::from(zp_addr));\n    let addr_high= mr(mem, cpu, (zp_addr + 1) & 0xFF);\n    let addr: u16 = addr_low | (addr_high << 8);",
        "let addr_low = mr(mem, cpu, u16::from(zp_addr));\n    let addr_high = mr(mem, cpu, u16::from(zp_addr.wrapping_add(1)));\n    let addr: u16 = u16::from(addr_low) | (u16::from(addr_high) << 8);",
    )
    text = text.replace(
        "let addr_low= mr(mem, cpu, zp_ptr);\n    let addr_high= mr(mem, cpu, (zp_ptr + 1) & 0xFF);\n    let base: u16 = addr_low | (addr_high << 8);",
        "let addr_low = mr(mem, cpu, u16::from(zp_ptr));\n    let addr_high = mr(mem, cpu, u16::from(zp_ptr.wrapping_add(1)));\n    let base: u16 = u16::from(addr_low) | (u16::from(addr_high) << 8);",
    )
    text = text.replace(
        "cpu.pc = (pc_low | (pc_high << 8)) & 0xFFFF;",
        "cpu.pc = u16::from(pc_low) | (u16::from(pc_high) << 8);",
    )
    OUT.write_text(text)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
