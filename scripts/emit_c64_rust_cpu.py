#!/usr/bin/env python3
"""Emit rust/c64py-core/src/c64_cpu_generated.rs from cpu.py (CPU6502 helpers + execute).

Run: python3 scripts/emit_c64_rust_cpu.py
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CPU = ROOT / "cpu.py"
OUT = ROOT / "rust" / "c64py-core" / "src" / "c64_cpu_generated.rs"


class EmitError(Exception):
    pass


def to_rust_expr(node: ast.AST) -> str:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, int):
            return hex(node.value) if node.value > 9 else str(node.value)
        if isinstance(node.value, bool):
            return "true" if node.value else "false"
        raise EmitError(f"unsupported constant {node.value!r}")
    if isinstance(node, ast.Name):
        if node.id == "True":
            return "true"
        if node.id == "False":
            return "false"
        raise EmitError(f"unknown name {node.id}")
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            if node.attr == "state":
                raise EmitError("bare self.state")
            # self.state.pc -> cpu.pc
            if node.attr in ("pc", "a", "x", "y", "sp", "p", "stopped"):
                return f"cpu.{node.attr}"
        raise EmitError(f"attribute {ast.dump(node)}")
    if isinstance(node, ast.BinOp):
        l, r = to_rust_expr(node.left), to_rust_expr(node.right)
        if isinstance(node.op, ast.Add):
            return f"({l}).wrapping_add({r})"
        if isinstance(node.op, ast.Sub):
            return f"({l}).wrapping_sub({r})"
        if isinstance(node.op, ast.Mult):
            return f"({l}).wrapping_mul({r})"
        if isinstance(node.op, ast.BitOr):
            return f"({l}) | ({r})"
        if isinstance(node.op, ast.BitAnd):
            return f"({l}) & ({r})"
        if isinstance(node.op, ast.BitXor):
            return f"({l}) ^ ({r})"
        if isinstance(node.op, ast.LShift):
            return f"({l}) << ({r})"
        if isinstance(node.op, ast.RShift):
            return f"(({l}) as i32 >> ({r})) as u8" if r != "8" else f"({l}) >> 8"
        raise EmitError(f"binop {type(node.op)}")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Invert):
        return f"!({to_rust_expr(node.operand)})"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return f"(-({to_rust_expr(node.operand)}))"
    if isinstance(node, ast.Compare):
        left = to_rust_expr(node.left)
        parts = [left]
        for op, comp in zip(node.ops, node.comparators):
            rhs = to_rust_expr(comp)
            if isinstance(op, ast.Eq):
                parts.append(f"== {rhs}")
            elif isinstance(op, ast.NotEq):
                parts.append(f"!= {rhs}")
            elif isinstance(op, ast.Lt):
                parts.append(f"< {rhs}")
            elif isinstance(op, ast.LtE):
                parts.append(f"<= {rhs}")
            elif isinstance(op, ast.Gt):
                parts.append(f"> {rhs}")
            elif isinstance(op, ast.GtE):
                parts.append(f">= {rhs}")
            else:
                raise EmitError(f"compare {type(op)}")
        return " ".join(parts)
    if isinstance(node, ast.BoolOp):
        op = " && " if isinstance(node.op, ast.And) else " || "
        return "(" + op.join(to_rust_expr(v) for v in node.values) + ")"
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute):
            fn = node.func
            if isinstance(fn.value, ast.Name) and fn.value.id == "self":
                m = fn.attr
                args = [to_rust_expr(a) for a in node.args]
                if m == "_mr":
                    return f"mr(mem, cpu, {args[0]})"
                if m == "_mw":
                    return f"{{ mw(mem, cpu, {args[0]}, {args[1]}); }}"
                if m == "_read_word":
                    return f"read_word(mem, cpu, {args[0]})"
                if m == "_update_flags":
                    return f"{{ update_nz(cpu, {args[0]}); }}"
                if m == "_get_flag":
                    fl = node.args[0]
                    assert isinstance(fl, ast.Constant)
                    mask = fl.value
                    return f"(cpu.p & {hex(mask)}) != 0"
                if m == "_set_flag":
                    fl = node.args[0]
                    assert isinstance(fl, ast.Constant)
                    mask = fl.value
                    val = to_rust_expr(node.args[1])
                    return f"{{ set_flag(cpu, {hex(mask)}, {val}); }}"
                if m == "_page_crossed":
                    return f"page_crossed({args[0]}, {args[1]})"
                if m == "_adc_finish":
                    return f"{{ adc_finish(cpu, {args[0]}, {args[1]}, {args[2]}); }}"
                if m == "_rmw_dummy_write_6510":
                    return f"{{ rmw_dummy_6510(mem, cpu, {args[0]}, {args[1]}); }}"
                if m == "_branch":
                    return f"branch(cpu, mem, {args[0]})"
                if m.startswith("_") and m[1:].isalnum():
                    rust_fn = m[1:]
                    return f"{{ {rust_fn}(cpu, mem) }}"
        raise EmitError(f"call {ast.dump(node)[:120]}")
    raise EmitError(f"expr {type(node)} {ast.dump(node)[:80]}")


def to_rust_stmt(node: ast.AST, declared: set[str]) -> list[str]:
    out: list[str] = []
    if isinstance(node, ast.Assign):
        assert len(node.targets) == 1
        t = node.targets[0]
        if not isinstance(t, ast.Name):
            raise EmitError(f"assign target {ast.dump(t)}")
        name = t.id
        val = to_rust_expr(node.value)
        if name not in declared:
            out.append(f"let {name} = {val};")
            declared.add(name)
        else:
            out.append(f"{name} = {val};")
        return out
    if isinstance(node, ast.AugAssign):
        raise EmitError("augassign")
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        c = node.value
        s = to_rust_expr(c)
        if s.startswith("{") and s.endswith("}"):
            out.append(s[1:-1].strip() + ";")
        else:
            out.append(f"{s};")
        return out
    if isinstance(node, ast.Return):
        if node.value is None:
            out.append("return 0;")
            return out
        v = node.value
        if isinstance(v, ast.If):
            # return a if cond else b
            test = to_rust_expr(v.test)
            tb = v.body[0]
            eb = v.orelse[0]
            assert isinstance(tb, ast.Return) and isinstance(eb, ast.Return)
            ta = to_rust_expr(tb.value)
            fa = to_rust_expr(eb.value)
            out.append(f"return if {test} {{ {ta} }} else {{ {fa} }};")
            return out
        out.append(f"return {to_rust_expr(v)};")
        return out
    raise EmitError(f"stmt {type(node)} {ast.dump(node)[:100]}")


def transpile_method(src: str, name: str) -> str:
    m = re.search(rf"    def {re.escape(name)}\(self\) -> int:\s*\n(?P<body>(?:        .*\n)+)", src)
    if not m:
        raise EmitError(f"missing method {name}")
    body_text = m.group("body")
    lines = []
    for line in body_text.splitlines():
        if line.strip().startswith('"""'):
            continue
        if line.strip().startswith("#"):
            continue
        lines.append(line[8:] if line.startswith("        ") else line)
    inner = "\n".join(lines)
    tree = ast.parse("def _f():\n" + textwrap_indent(inner, "    "))
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    declared: set[str] = set()
    rust_lines = []
    for st in fn.body:
        rust_lines.extend(to_rust_stmt(st, declared))
    rust_name = name[1:]
    return f"pub fn {rust_name}(cpu: &mut CpuState, mem: &mut C64MemoryMap<'_>) -> u32 {{\n" + "\n".join(
        "    " + x for x in rust_lines
    ) + "\n}\n"


def textwrap_indent(s: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else "" for line in s.splitlines()) + ("\n" if s.endswith("\n") else "")


def extract_execute_function(src: str) -> ast.FunctionDef:
    m = re.search(r"    def _execute_opcode\(self, opcode: int\) -> int:\s*\n", src)
    if not m:
        raise EmitError("no _execute_opcode")
    start = m.end()
    rest = src[start:]
    m2 = re.search(r"\n    def _brk\(self\)", rest)
    body = rest[: m2.start()]
    tree = ast.parse("def _execute_opcode(self, opcode):\n" + body)
    return tree.body[0]  # type: ignore


def emit_execute_match(fn: ast.FunctionDef) -> str:
    arms: list[str] = []

    def handle_body(stmts: list[ast.stmt]) -> str:
        declared: set[str] = set()
        lines: list[str] = []
        for st in stmts:
            lines.extend(to_rust_stmt(st, declared))
        return "\n".join("            " + x for x in lines)

    for st in fn.body:
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
            continue  # docstring
        if not isinstance(st, ast.If):
            continue
        chain = st
        while chain:
            test = chain.test
            if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "opcode":
                # opcode == 0xNN
                op = None
                for opa, comp in zip(test.ops, test.comparators):
                    if isinstance(opa, ast.Eq) and isinstance(comp, ast.Constant):
                        op = comp.value
                if op is not None:
                    body_rust = handle_body(chain.body)
                    arms.append(f"            {op:#04x} => {{\n{body_rust}\n            }}")
            elif isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "opcode":
                pass
            elif isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute):
                pass
            # elif opcode in [...]
            if isinstance(test, ast.Compare):
                if isinstance(test.left, ast.Name) and test.left.id == "opcode":
                    if any(isinstance(o, ast.In) for o in test.ops):
                        # opcode in LIST
                        elts = test.comparators[0]
                        assert isinstance(elts, ast.List | ast.Tuple)
                        vals = [x.value for x in elts.elts if isinstance(x, ast.Constant)]
                        body_rust = handle_body(chain.body)
                        for op in vals:
                            arms.append(f"            {op:#04x} => {{\n{body_rust}\n            }}")
            if chain.orelse and isinstance(chain.orelse[0], ast.If):
                chain = chain.orelse[0]
            else:
                if chain.orelse:
                    # else: unknown opcode
                    body_rust = handle_body(chain.orelse)
                    arms.append(f"            _ => {{\n{body_rust}\n            }}")
                break
    return "match opcode {\n" + ",\n".join(arms) + "\n        }"


def main() -> None:
    src = CPU.read_text()
    # For now emit stub: user must hand-maintain execute if AST chain is complex
    OUT.write_text(
        "// Run emit_c64_rust_cpu.py — partial emitter; use c64_cpu.rs hand-written match.\n"
    )
    print("stub", OUT)


if __name__ == "__main__":
    main()
