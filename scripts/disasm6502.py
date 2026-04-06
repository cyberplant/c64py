"""Minimal MOS 6502 disassembler (legal opcodes; same-line operands as hex)."""

from __future__ import annotations

# fmt: off
OPCODES: dict[int, tuple[str, int]] = {
    0x00: ("BRK", 1), 0x01: ("ORA (zp,X)", 2), 0x05: ("ORA zp", 2), 0x06: ("ASL zp", 2),
    0x08: ("PHP", 1), 0x09: ("ORA #", 2), 0x0A: ("ASL A", 1), 0x0D: ("ORA abs", 3),
    0x0E: ("ASL abs", 3), 0x10: ("BPL rel", 2), 0x11: ("ORA (zp),Y", 2), 0x15: ("ORA zp,X", 2),
    0x16: ("ASL zp,X", 2), 0x18: ("CLC", 1), 0x19: ("ORA abs,Y", 3), 0x1D: ("ORA abs,X", 3),
    0x1E: ("ASL abs,X", 3), 0x20: ("JSR abs", 3), 0x21: ("AND (zp,X)", 2), 0x24: ("BIT zp", 2),
    0x25: ("AND zp", 2), 0x26: ("ROL zp", 2), 0x28: ("PLP", 1), 0x29: ("AND #", 2), 0x2A: ("ROL A", 1),
    0x2C: ("BIT abs", 3), 0x2D: ("AND abs", 3), 0x2E: ("ROL abs", 3), 0x30: ("BMI rel", 2),
    0x31: ("AND (zp),Y", 2), 0x35: ("AND zp,X", 2), 0x36: ("ROL zp,X", 2), 0x38: ("SEC", 1),
    0x39: ("AND abs,Y", 3), 0x3D: ("AND abs,X", 3), 0x3E: ("ROL abs,X", 3), 0x40: ("RTI", 1),
    0x41: ("EOR (zp,X)", 2), 0x45: ("EOR zp", 2), 0x46: ("LSR zp", 2), 0x48: ("PHA", 1),
    0x49: ("EOR #", 2), 0x4A: ("LSR A", 1), 0x4C: ("JMP abs", 3), 0x4D: ("EOR abs", 3),
    0x4E: ("LSR abs", 3), 0x50: ("BVC rel", 2), 0x51: ("EOR (zp),Y", 2), 0x55: ("EOR zp,X", 2),
    0x56: ("LSR zp,X", 2), 0x58: ("CLI", 1), 0x59: ("EOR abs,Y", 3), 0x5D: ("EOR abs,X", 3),
    0x5E: ("LSR abs,X", 3), 0x60: ("RTS", 1), 0x61: ("ADC (zp,X)", 2), 0x65: ("ADC zp", 2),
    0x66: ("ROR zp", 2), 0x68: ("PLA", 1), 0x69: ("ADC #", 2), 0x6A: ("ROR A", 1),
    0x6C: ("JMP (abs)", 3), 0x6D: ("ADC abs", 3), 0x6E: ("ROR abs", 3), 0x70: ("BVS rel", 2),
    0x71: ("ADC (zp),Y", 2), 0x75: ("ADC zp,X", 2), 0x76: ("ROR zp,X", 2), 0x78: ("SEI", 1),
    0x79: ("ADC abs,Y", 3), 0x7D: ("ADC abs,X", 3), 0x7E: ("ROR abs,X", 3), 0x81: ("STA (zp,X)", 2),
    0x84: ("STY zp", 2), 0x85: ("STA zp", 2), 0x86: ("STX zp", 2), 0x88: ("DEY", 1),
    0x8A: ("TXA", 1), 0x8C: ("STY abs", 3), 0x8D: ("STA abs", 3), 0x8E: ("STX abs", 3),
    0x90: ("BCC rel", 2), 0x91: ("STA (zp),Y", 2), 0x94: ("STY zp,X", 2), 0x95: ("STA zp,X", 2),
    0x96: ("STX zp,Y", 2), 0x98: ("TYA", 1), 0x99: ("STA abs,Y", 3), 0x9A: ("TXS", 1),
    0x9D: ("STA abs,X", 3), 0xA0: ("LDY #", 2), 0xA1: ("LDA (zp,X)", 2), 0xA2: ("LDX #", 2),
    0xA4: ("LDY zp", 2), 0xA5: ("LDA zp", 2), 0xA6: ("LDX zp", 2), 0xA8: ("TAY", 1),
    0xA9: ("LDA #", 2), 0xAA: ("TAX", 1), 0xAC: ("LDY abs", 3), 0xAD: ("LDA abs", 3),
    0xAE: ("LDX abs", 3), 0xB0: ("BCS rel", 2), 0xB1: ("LDA (zp),Y", 2), 0xB4: ("LDY zp,X", 2),
    0xB5: ("LDA zp,X", 2), 0xB6: ("LDX zp,Y", 2), 0xB8: ("CLV", 1), 0xB9: ("LDA abs,Y", 3),
    0xBC: ("LDY abs,X", 3), 0xBD: ("LDA abs,X", 3), 0xBE: ("LDX abs,Y", 3), 0xC0: ("CPY #", 2),
    0xC1: ("CMP (zp,X)", 2), 0xC4: ("CPY zp", 2), 0xC5: ("CMP zp", 2), 0xC6: ("DEC zp", 2),
    0xC8: ("INY", 1), 0xC9: ("CMP #", 2), 0xCA: ("DEX", 1), 0xCC: ("CPY abs", 3),
    0xCD: ("CMP abs", 3), 0xCE: ("DEC abs", 3), 0xD0: ("BNE rel", 2), 0xD1: ("CMP (zp),Y", 2),
    0xD4: ("*NOP zp,X", 2), 0xD5: ("CMP zp,X", 2), 0xD6: ("DEC zp,X", 2), 0xD8: ("CLD", 1),
    0xD9: ("CMP abs,Y", 3), 0xDD: ("CMP abs,X", 3), 0xDE: ("DEC abs,X", 3), 0xE0: ("CPX #", 2),
    0xE1: ("SBC (zp,X)", 2), 0xE4: ("CPX zp", 2), 0xE5: ("SBC zp", 2), 0xE6: ("INC zp", 2),
    0xE8: ("INX", 1), 0xE9: ("SBC #", 2), 0xEA: ("NOP", 1), 0xEC: ("CPX abs", 3),
    0xED: ("SBC abs", 3), 0xEE: ("INC abs", 3), 0xF0: ("BEQ rel", 2), 0xF1: ("SBC (zp),Y", 2),
    0xF5: ("SBC zp,X", 2), 0xF6: ("INC zp,X", 2), 0xF8: ("SED", 1), 0xF9: ("SBC abs,Y", 3),
    0xFD: ("SBC abs,X", 3), 0xFE: ("INC abs,X", 3),
}
# fmt: on


def _fmt_operand(mnem: str, addr: int, b1: int, b2: int) -> str:
    if "#" in mnem:
        return f"{mnem.replace('#', f'#$%02X' % b1)}"
    if "rel" in mnem:
        dest = (addr + 2 + (b1 if b1 < 128 else b1 - 256)) & 0xFFFF
        return mnem.replace("rel", f"${dest:04X}")
    if "zp,X" in mnem and "(zp,X)" not in mnem:
        return mnem.replace("zp,X", f"${b1:02X},X")
    if "zp,Y" in mnem and "(zp),Y" not in mnem:
        return mnem.replace("zp,Y", f"${b1:02X},Y")
    if "(zp,X)" in mnem:
        return mnem.replace("(zp,X)", f"(${b1:02X},X)")
    if "(zp),Y" in mnem:
        return mnem.replace("(zp),Y", f"(${b1:02X}),Y")
    if "zp" in mnem and "abs" not in mnem and "zp,X" not in mnem and "zp,Y" not in mnem:
        return mnem.replace("zp", f"${b1:02X}")
    if "abs" in mnem or "JMP" in mnem:
        w = b1 | (b2 << 8)
        if "(abs)" in mnem:
            return mnem.replace("(abs)", f"(${w:04X})")
        if "abs,Y" in mnem:
            return mnem.replace("abs,Y", f"${w:04X},Y")
        if "abs,X" in mnem:
            return mnem.replace("abs,X", f"${w:04X},X")
        return mnem.replace("abs", f"${w:04X}")
    return mnem


def disasm_one(ram: bytes, org: int, offset: int) -> tuple[str, int]:
    """Disassemble one instruction at ram[offset]; return (line, length)."""
    if offset >= len(ram):
        return ("(EOF)", 0)
    op = ram[offset]
    if op not in OPCODES:
        return (f"*${op:02X}", 1)
    mnem, ln = OPCODES[op]
    if ln == 1:
        return (mnem, 1)
    if offset + ln - 1 >= len(ram):
        return (f"{mnem} <trunc>", 1)
    b1 = ram[offset + 1]
    b2 = ram[offset + 2] if ln == 3 else 0
    a = org + offset
    text = _fmt_operand(mnem, a, b1, b2)
    return (text, ln)


def disasm_range(ram: bytes, org: int, start_off: int, end_off: int) -> list[str]:
    lines: list[str] = []
    o = start_off
    while o < end_off and o < len(ram):
        text, ln = disasm_one(ram, org, o)
        if ln == 0:
            break
        chunk = ram[o : o + ln]
        hexp = " ".join(f"{b:02X}" for b in chunk)
        lines.append(f"{org + o:04X}  {hexp:12s}  {text}")
        o += ln
    return lines
