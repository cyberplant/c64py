//! Run many instructions on the fast path (GIL held by PyO3 caller).

use crate::c64_cpu::{execute_opcode, irq_should_dispatch, read_word_at, set_flag, CpuState};
use crate::c64_memory::C64MemoryMap;
use crate::c64_timing::{advance_raster, update_cia_timers};
use crate::c64_vicii::ViciiEngine;
use crate::resid_session::ResidSession;
use std::fs::File;
use std::io::Write;
use pad::PadStr;

const IRQ_VECTOR: u16 = 0xFFFE;

/// Bus cycle classification — mirrors Python's `_BUS_READ / _BUS_WRITE / _BUS_INTERNAL`.
/// Only READ cycles can be stalled by BA; WRITE and INTERNAL cycles proceed regardless.
#[derive(Clone, Copy, PartialEq)]
enum BusPhase { Read, Write, Internal }

/// Return per-cycle bus phases for an opcode with `c` cycles.
/// Mirrors the logic in `cpu.py::_bus_cycle_phases`.
/// Defaults to all-READ (conservative) for unlisted opcodes.
fn bus_cycle_phases(opc: u8, c: u32) -> [BusPhase; 8] {
    let mut ph = [BusPhase::Read; 8];
    let c = c as usize;

    // Implied / accumulator 2-cycle ops: second cycle is internal.
    const IMPLIED_INTERNAL_2: &[u8] = &[
        0xCA, 0x88, 0xE8, 0xC8, // DEX DEY INX INY
        0x18, 0x38, 0x58, 0x78, // CLC SEC CLI SEI
        0xB8, 0xD8, 0xF8,       // CLV CLD SED
        0xEA,                   // NOP
        0xAA, 0xA8, 0x8A, 0x98, // TAX TAY TXA TYA
        0xBA, 0x9A,             // TSX TXS
        0x0A, 0x2A, 0x4A, 0x6A, // ASL/ROL/LSR/ROR A
    ];
    if c == 2 && IMPLIED_INTERNAL_2.contains(&opc) {
        ph[1] = BusPhase::Internal;
        return ph;
    }

    // Stores: last cycle is a write.
    const STORE_OPS: &[u8] = &[
        0x85, 0x95, 0x8D, 0x9D, 0x99, 0x81, 0x91, // STA
        0x86, 0x8E, 0x96,                          // STX
        0x84, 0x8C, 0x94,                          // STY
    ];
    if STORE_OPS.contains(&opc) && c >= 1 {
        ph[c - 1] = BusPhase::Write;
        return ph;
    }

    // RMW: last two cycles are writes.
    const RMW_OPS: &[u8] = &[
        0x06, 0x16, 0x0E, 0x1E, // ASL
        0x26, 0x36, 0x2E, 0x3E, // ROL
        0x46, 0x56, 0x4E, 0x5E, // LSR
        0x66, 0x76, 0x6E, 0x7E, // ROR
        0xC6, 0xD6, 0xCE, 0xDE, // DEC
        0xE6, 0xF6, 0xEE, 0xFE, // INC
    ];
    if RMW_OPS.contains(&opc) && c >= 2 {
        ph[c - 1] = BusPhase::Write;
        ph[c - 2] = BusPhase::Write;
        return ph;
    }

    // JSR ($20): cycles 3-4 (0-indexed) are writes (push return address hi/lo).
    if opc == 0x20 && c >= 6 {
        ph[3] = BusPhase::Write;
        ph[4] = BusPhase::Write;
        return ph;
    }

    // RTS ($60): cycle 4 is internal (return address increment).
    if opc == 0x60 && c >= 6 {
        ph[4] = BusPhase::Internal;
        return ph;
    }

    ph
}

fn handle_irq_fast(cpu: &mut CpuState, mem: &mut C64MemoryMap<'_>) {
    mem.pending_irq = false;
    let pc = cpu.pc;
    mw_stack(mem, cpu, (pc >> 8) as u8);
    cpu.sp = cpu.sp.wrapping_sub(1);
    mw_stack(mem, cpu, pc as u8);
    cpu.sp = cpu.sp.wrapping_sub(1);
    mw_stack(mem, cpu, (cpu.p | 0x20) & !0x10);
    cpu.sp = cpu.sp.wrapping_sub(1);
    set_flag(cpu, 0x04, true);
    cpu.pc = read_word_at(mem, IRQ_VECTOR);
}

#[inline]
fn mw_stack(mem: &mut C64MemoryMap<'_>, cpu: &mut CpuState, v: u8) {
    mem.write(0x0100u16.wrapping_add(cpu.sp as u16), v);
}

#[inline]
fn pc_in_stop_set(pc: u16, stop_pcs: &[u16]) -> bool {
    stop_pcs.iter().any(|&s| s == pc)
}

fn service_irq_if_any(cpu: &mut CpuState, mem: &mut C64MemoryMap<'_>) {
    mem.recompute_pending_irq();
    if irq_should_dispatch(cpu, mem.pending_irq) {
        // Coarse-mode: follow historical behavior and service CIA-driven IRQ path only.
        if (mem.cia1_icr & 0x80) != 0 {
            handle_irq_fast(cpu, mem);
        }
    }
}

/// 7-cycle IRQ dispatch for the hybrid VIC path.
/// Mirrors Python's `_handle_irq` which ticks the VIC once per dispatch cycle.
/// Bus phases: 2×READ (dummy), 3×WRITE (push), 2×READ (vector).
fn dispatch_irq_hybrid(
    cpu: &mut CpuState,
    mem: &mut C64MemoryMap<'_>,
    eng: &mut ViciiEngine,
    _resid: Option<&mut ResidSession>,
    total_cyc: &mut u64,
) {
    mem.pending_irq = false;
    let pc = cpu.pc;
    let pch = (pc >> 8) as u8;
    let pcl = pc as u8;
    let status = (cpu.p | 0x20) & !0x10; // B clear, bit 5 set

    // Per-phase tick: advance VIC, stall on READ if ba_blocks_cpu.
    // resid is passed as None here — 7-cycle drift is negligible for reSID sync.
    macro_rules! tick_phase {
        (READ) => {
            loop {
                let (_, ba_blocks_cpu) = tick_one_cycle(eng, mem, cpu, None, total_cyc);
                if !ba_blocks_cpu { break; }
            }
        };
        (WRITE) => {
            tick_one_cycle(eng, mem, cpu, None, total_cyc);
        };
    }

    // Cycles 1-2: dummy opcode fetches (READ, can stall)
    tick_phase!(READ);
    tick_phase!(READ);
    // Cycles 3-5: stack pushes (WRITE — not stalled by BA)
    tick_phase!(WRITE);
    mem.write(0x0100u16.wrapping_add(cpu.sp as u16), pch);
    cpu.sp = cpu.sp.wrapping_sub(1);
    tick_phase!(WRITE);
    mem.write(0x0100u16.wrapping_add(cpu.sp as u16), pcl);
    cpu.sp = cpu.sp.wrapping_sub(1);
    tick_phase!(WRITE);
    mem.write(0x0100u16.wrapping_add(cpu.sp as u16), status);
    cpu.sp = cpu.sp.wrapping_sub(1);
    // Cycles 6-7: vector fetch (READ, can stall)
    tick_phase!(READ);
    let vec_lo = mem.read(IRQ_VECTOR);
    tick_phase!(READ);
    let vec_hi = mem.read(IRQ_VECTOR.wrapping_add(1));

    set_flag(cpu, 0x04, true);
    cpu.pc = (vec_lo as u16) | ((vec_hi as u16) << 8);
    mem.recompute_pending_irq();
}

fn coarse_cycles(
    cpu: &mut CpuState,
    mem: &mut C64MemoryMap<'_>,
    c: u32,
    resid: Option<&mut ResidSession>,
) {
    advance_raster(mem, c);
    cpu.cycles = cpu.cycles.wrapping_add(u64::from(c));
    update_cia_timers(mem, c, false);
    if let Some(r) = resid {
        r.clock_cycles(c as i32);
    }
    service_irq_if_any(cpu, mem);
}

/// Opcode sizes for trace operand formatting
const OPCODE_SIZES: [u8; 256] = [
    1, 2, 0, 0, 0, 2, 2, 0, 1, 2, 1, 0, 0, 3, 3, 0, // 00-0F
    2, 2, 0, 0, 0, 2, 2, 0, 1, 3, 0, 0, 0, 3, 3, 0, // 10-1F
    3, 2, 0, 0, 2, 2, 2, 0, 1, 2, 1, 0, 3, 3, 3, 0, // 20-2F
    2, 2, 0, 0, 0, 2, 2, 0, 1, 3, 0, 0, 0, 3, 3, 0, // 30-3F
    1, 2, 0, 0, 0, 2, 2, 0, 1, 2, 1, 0, 3, 3, 3, 0, // 40-4F
    2, 2, 0, 0, 0, 2, 2, 0, 1, 3, 0, 0, 0, 3, 3, 0, // 50-5F
    1, 2, 0, 0, 0, 2, 2, 0, 1, 2, 1, 0, 3, 3, 3, 0, // 60-6F
    2, 2, 0, 0, 0, 2, 2, 0, 1, 3, 0, 0, 0, 3, 3, 0, // 70-7F
    2, 2, 2, 0, 2, 2, 2, 0, 1, 0, 1, 0, 3, 3, 3, 0, // 80-8F
    2, 2, 0, 0, 2, 2, 2, 0, 1, 3, 1, 0, 0, 3, 0, 0, // 90-9F
    2, 2, 2, 0, 2, 2, 2, 0, 1, 2, 1, 0, 3, 3, 3, 0, // A0-AF
    2, 2, 0, 0, 2, 2, 2, 0, 1, 3, 0, 0, 3, 3, 3, 0, // B0-BF
    2, 2, 2, 0, 2, 2, 2, 0, 1, 2, 1, 0, 3, 3, 3, 0, // C0-CF
    2, 2, 0, 0, 0, 2, 2, 0, 1, 3, 0, 0, 0, 3, 3, 0, // D0-DF
    2, 2, 2, 0, 2, 2, 2, 0, 1, 2, 1, 0, 3, 3, 3, 0, // E0-EF
    2, 2, 0, 0, 0, 2, 2, 0, 1, 3, 0, 0, 0, 3, 3, 0, // F0-FF
];

/// Opcode mnemonics for trace output
const OPCODE_NAMES: [&str; 256] = [
    "BRK", "ORA", "???", "???", "???", "ORA", "ASL", "???", "PHP", "ORA", "ASL", "???", "???", "ORA", "ASL", "???",
    "BPL", "ORA", "???", "???", "???", "ORA", "ASL", "???", "CLC", "ORA", "???", "???", "???", "ORA", "ASL", "???",
    "JSR", "AND", "???", "???", "BIT", "AND", "ROL", "???", "PLP", "AND", "ROL", "???", "BIT", "AND", "ROL", "???",
    "BMI", "AND", "???", "???", "???", "AND", "ROL", "???", "SEC", "AND", "???", "???", "???", "AND", "ROL", "???",
    "RTI", "EOR", "???", "???", "???", "EOR", "LSR", "???", "PHA", "EOR", "LSR", "???", "JMP", "EOR", "LSR", "???",
    "BVC", "EOR", "???", "???", "???", "EOR", "LSR", "???", "CLI", "EOR", "???", "???", "???", "EOR", "LSR", "???",
    "RTS", "ADC", "???", "???", "???", "ADC", "ROR", "???", "PLA", "ADC", "ROR", "???", "JMP", "ADC", "ROR", "???",
    "BVS", "ADC", "???", "???", "???", "ADC", "ROR", "???", "SEI", "ADC", "???", "???", "???", "ADC", "ROR", "???",
    "???", "STA", "???", "???", "STY", "STA", "STX", "???", "DEY", "???", "TXA", "???", "STY", "STA", "STX", "???",
    "BCC", "STA", "???", "???", "STY", "STA", "STX", "???", "TYA", "STA", "TXS", "???", "???", "STA", "???", "???",
    "LDY", "LDA", "LDX", "???", "LDY", "LDA", "LDX", "???", "TAY", "LDA", "TAX", "???", "LDY", "LDA", "LDX", "???",
    "BCS", "LDA", "???", "???", "LDY", "LDA", "LDX", "???", "CLV", "LDA", "TSX", "???", "LDY", "LDA", "LDX", "???",
    "CPY", "CMP", "???", "???", "CPY", "CMP", "DEC", "???", "INY", "CMP", "DEX", "???", "CPY", "CMP", "DEC", "???",
    "BNE", "CMP", "???", "???", "???", "CMP", "DEC", "???", "CLD", "CMP", "???", "???", "???", "CMP", "DEC", "???",
    "CPX", "SBC", "???", "???", "CPX", "SBC", "INC", "???", "INX", "SBC", "NOP", "???", "CPX", "SBC", "INC", "???",
    "BEQ", "SBC", "???", "???", "???", "SBC", "INC", "???", "SED", "SBC", "???", "???", "???", "SBC", "INC", "???",
];

/// Write a VICE-format trace line to file
fn write_trace_entry(
    file: &mut File,
    pc: u16,
    opcode: u8,
    mem: &mut C64MemoryMap<'_>,
    cpu: &CpuState,
) {
    let size = OPCODE_SIZES[opcode as usize] as usize;
    let mut bytes = vec![opcode];
    for i in 1..size {
        bytes.push(mem.read(pc.wrapping_add(i as u16)));
    }
    let bytes_str = bytes.iter().map(|b| format!("{:02X}", b)).collect::<Vec<_>>().join(" ").pad_to_width(11);
    
    let mnemonic = OPCODE_NAMES[opcode as usize];
    let operand_str = format_operand(opcode, &bytes[1..], pc);
    let instr_str = format!("{} {}", mnemonic, operand_str).pad_to_width(14);
    
    let flags = format_flags(cpu.p);
    
    let line = format!(
        ".C:{:04x}  {} {} - A:{:02X} X:{:02X} Y:{:02X} SP:{:02x} {}  {} ; rust",
        pc, bytes_str, instr_str, cpu.a, cpu.x, cpu.y, cpu.sp, flags, cpu.cycles
    );
    
    if writeln!(file, "{}", line).is_ok() {
        let _ = file.flush();
    }
}

fn format_flags(p: u8) -> String {
    format!(
        "{}{}-{}{}{}{}{}",
        if p & 0x80 != 0 { 'N' } else { '.' },
        if p & 0x40 != 0 { 'V' } else { '.' },
        if p & 0x10 != 0 { 'B' } else { '.' },
        if p & 0x08 != 0 { 'D' } else { '.' },
        if p & 0x04 != 0 { 'I' } else { '.' },
        if p & 0x02 != 0 { 'Z' } else { '.' },
        if p & 0x01 != 0 { 'C' } else { '.' },
    )
}

fn format_operand(opcode: u8, operand_bytes: &[u8], pc: u16) -> String {
    use std::collections::HashMap;
    lazy_static::lazy_static! {
        static ref MODES: HashMap<u8, &'static str> = {
            let mut m = HashMap::new();
            m.insert(0x00, "imp"); m.insert(0x01, "izx"); m.insert(0x05, "zp"); m.insert(0x06, "zp");
            m.insert(0x08, "imp"); m.insert(0x09, "imm"); m.insert(0x0A, "acc"); m.insert(0x0D, "abs");
            m.insert(0x0E, "abs"); m.insert(0x10, "rel"); m.insert(0x11, "izy"); m.insert(0x15, "zpx");
            m.insert(0x16, "zpx"); m.insert(0x18, "imp"); m.insert(0x19, "aby"); m.insert(0x1D, "abx");
            m.insert(0x1E, "abx"); m.insert(0x20, "abs"); m.insert(0x21, "izx"); m.insert(0x24, "zp");
            m.insert(0x25, "zp"); m.insert(0x26, "zp"); m.insert(0x28, "imp"); m.insert(0x29, "imm");
            m.insert(0x2A, "acc"); m.insert(0x2C, "abs"); m.insert(0x2D, "abs"); m.insert(0x2E, "abs");
            m.insert(0x30, "rel"); m.insert(0x31, "izy"); m.insert(0x35, "zpx"); m.insert(0x36, "zpx");
            m.insert(0x38, "imp"); m.insert(0x39, "aby"); m.insert(0x3D, "abx"); m.insert(0x3E, "abx");
            m.insert(0x40, "imp"); m.insert(0x41, "izx"); m.insert(0x45, "zp"); m.insert(0x46, "zp");
            m.insert(0x48, "imp"); m.insert(0x49, "imm"); m.insert(0x4A, "acc"); m.insert(0x4C, "abs");
            m.insert(0x4D, "abs"); m.insert(0x4E, "abs"); m.insert(0x50, "rel"); m.insert(0x51, "izy");
            m.insert(0x55, "zpx"); m.insert(0x56, "zpx"); m.insert(0x58, "imp"); m.insert(0x59, "aby");
            m.insert(0x5D, "abx"); m.insert(0x5E, "abx"); m.insert(0x60, "imp"); m.insert(0x61, "izx");
            m.insert(0x65, "zp"); m.insert(0x66, "zp"); m.insert(0x68, "imp"); m.insert(0x69, "imm");
            m.insert(0x6A, "acc"); m.insert(0x6C, "ind"); m.insert(0x6D, "abs"); m.insert(0x6E, "abs");
            m.insert(0x70, "rel"); m.insert(0x71, "izy"); m.insert(0x75, "zpx"); m.insert(0x76, "zpx");
            m.insert(0x78, "imp"); m.insert(0x79, "aby"); m.insert(0x7D, "abx"); m.insert(0x7E, "abx");
            m.insert(0x80, "imm"); m.insert(0x81, "izx"); m.insert(0x84, "zp"); m.insert(0x85, "zp");
            m.insert(0x86, "zp"); m.insert(0x88, "imp"); m.insert(0x8A, "imp"); m.insert(0x8C, "abs");
            m.insert(0x8D, "abs"); m.insert(0x8E, "abs"); m.insert(0x90, "rel"); m.insert(0x91, "izy");
            m.insert(0x94, "zpx"); m.insert(0x95, "zpx"); m.insert(0x96, "zpy"); m.insert(0x98, "imp");
            m.insert(0x99, "aby"); m.insert(0x9A, "imp"); m.insert(0x9D, "abx"); m.insert(0x9E, "abx");
            m.insert(0xA0, "imm"); m.insert(0xA1, "izx"); m.insert(0xA2, "imm"); m.insert(0xA4, "zp");
            m.insert(0xA5, "zp"); m.insert(0xA6, "zp"); m.insert(0xA8, "imp"); m.insert(0xA9, "imm");
            m.insert(0xAA, "imp"); m.insert(0xAC, "abs"); m.insert(0xAD, "abs"); m.insert(0xAE, "abs");
            m.insert(0xB0, "rel"); m.insert(0xB1, "izy"); m.insert(0xB4, "zpx"); m.insert(0xB5, "zpx");
            m.insert(0xB6, "zpy"); m.insert(0xB8, "imp"); m.insert(0xB9, "aby"); m.insert(0xBD, "abx");
            m.insert(0xBE, "aby"); m.insert(0xC0, "imm"); m.insert(0xC1, "izx"); m.insert(0xC2, "imm");
            m.insert(0xC4, "zp"); m.insert(0xC5, "zp"); m.insert(0xC6, "zp"); m.insert(0xC8, "imp");
            m.insert(0xC9, "imm"); m.insert(0xCA, "imp"); m.insert(0xCC, "abs"); m.insert(0xCD, "abs");
            m.insert(0xCE, "abs"); m.insert(0xD0, "rel"); m.insert(0xD1, "izy"); m.insert(0xD5, "zpx");
            m.insert(0xD6, "zpx"); m.insert(0xD8, "imp"); m.insert(0xD9, "aby"); m.insert(0xDD, "abx");
            m.insert(0xDE, "abx"); m.insert(0xE0, "imm"); m.insert(0xE1, "izx"); m.insert(0xE2, "imm");
            m.insert(0xE4, "zp"); m.insert(0xE5, "zp"); m.insert(0xE6, "zp"); m.insert(0xE8, "imp");
            m.insert(0xE9, "imm"); m.insert(0xEA, "imp"); m.insert(0xEC, "abs"); m.insert(0xED, "abs");
            m.insert(0xEE, "abs"); m.insert(0xF0, "rel"); m.insert(0xF1, "izy"); m.insert(0xF5, "zpx");
            m.insert(0xF6, "zpx"); m.insert(0xF8, "imp"); m.insert(0xF9, "aby"); m.insert(0xFD, "abx");
            m.insert(0xFE, "abx");
            m
        };
    }
    
    let mode = MODES.get(&opcode).copied().unwrap_or("imp");
    match mode {
        "imp" => String::new(),
        "acc" => "A".to_string(),
        "imm" => format!("#${:02X}", operand_bytes.get(0).copied().unwrap_or(0)),
        "zp" => format!("${:02X}", operand_bytes.get(0).copied().unwrap_or(0)),
        "zpx" => format!("${:02X},X", operand_bytes.get(0).copied().unwrap_or(0)),
        "zpy" => format!("${:02X},Y", operand_bytes.get(0).copied().unwrap_or(0)),
        "izx" => format!("(${:02X},X)", operand_bytes.get(0).copied().unwrap_or(0)),
        "izy" => format!("(${:02X}),Y", operand_bytes.get(0).copied().unwrap_or(0)),
        "abs" => format!("${:04X}", u16::from_le_bytes([operand_bytes.get(0).copied().unwrap_or(0), operand_bytes.get(1).copied().unwrap_or(0)])),
        "abx" => format!("${:04X},X", u16::from_le_bytes([operand_bytes.get(0).copied().unwrap_or(0), operand_bytes.get(1).copied().unwrap_or(0)])),
        "aby" => format!("${:04X},Y", u16::from_le_bytes([operand_bytes.get(0).copied().unwrap_or(0), operand_bytes.get(1).copied().unwrap_or(0)])),
        "ind" => format!("(${:04X})", u16::from_le_bytes([operand_bytes.get(0).copied().unwrap_or(0), operand_bytes.get(1).copied().unwrap_or(0)])),
        "rel" => {
            let off = operand_bytes.get(0).copied().unwrap_or(0) as i8;
            let target = pc.wrapping_add(2).wrapping_add(off as u16);
            format!("${:04X}", target)
        }
        _ => String::new(),
    }
}

/// Advance VIC + CIA + reSID by one cycle, returning (irq_edge, ba_blocks_cpu).
#[inline]
fn tick_one_cycle(
    eng: &mut ViciiEngine,
    mem: &mut C64MemoryMap<'_>,
    cpu: &mut CpuState,
    resid: Option<&mut ResidSession>,
    total_cyc: &mut u64,
) -> (bool, bool) {
    let (irq_edge, ba_blocks_cpu) = eng.step(mem);
    mem.per_cycle_capture_at_cursor();
    if irq_edge {
        mem.trigger_vic_irq(0x01);
    }
    cpu.cycles = cpu.cycles.wrapping_add(1);
    update_cia_timers(mem, 1, false);
    if let Some(r) = resid {
        r.clock_cycles(1);
    }
    *total_cyc += 1;
    (irq_edge, ba_blocks_cpu)
}

/// Run up to `max_instructions` instructions.
///
/// * `hybrid_vic` — after each opcode, step VIC-II engine once per CPU cycle (PAL or NTSC table).
///   Models BA CPU stalls: when `ba_blocks_cpu` is returned by the VIC (badline, prefetch
///   drained), the CPU is held between instructions until the stall clears.
/// * `resid` — optional reSID session (lockstep); clocks each emulated cycle appropriately.
/// * `trace_path` — optional file path to write VICE-format trace output.
pub fn run_fast_batch(
    cpu: &mut CpuState,
    mem: &mut C64MemoryMap<'_>,
    max_instructions: u64,
    stop_pcs: &[u16],
    hybrid_vic: bool,
    mut vicii: Option<&mut ViciiEngine>,
    mut resid: Option<&mut ResidSession>,
    trace_path: Option<&str>,
) -> (u64, u64) {
    let mut trace_file: Option<File> = trace_path.map(|p| File::options().create(true).append(true).open(p).unwrap());
    let trace_enabled = trace_file.is_some();
    let mut ins = 0u64;
    let mut total_cyc = 0u64;
    for _ in 0..max_instructions {
        if cpu.stopped {
            break;
        }
        if pc_in_stop_set(cpu.pc, stop_pcs) {
            break;
        }

        let pc_before = cpu.pc;
        let opc = mem.read(cpu.pc);

        // Log trace entry before executing (matching Python behavior)
        if trace_enabled {
            if let Some(ref mut f) = trace_file {
                write_trace_entry(f, pc_before, opc, &mut *mem, cpu);
            }
        }

        let mut c = execute_opcode(cpu, mem, opc);
        if c == 0 && !cpu.stopped {
            c = 1;
        }
        let c = c as u32;

        if hybrid_vic {
            if let Some(eng) = vicii.as_deref_mut() {
                // Per-cycle BA stalling: mirrors Python cpu.py _bus_cycle_phases loop.
                // For each instruction cycle, advance VIC; if ba_blocks_cpu and this cycle
                // is a READ cycle, keep ticking (stall) before consuming the CPU cycle.
                let phases = bus_cycle_phases(opc, c);
                for i in 0..c {
                    let bus_phase = phases[i as usize];
                    loop {
                        let (_, ba_blocks_cpu) = tick_one_cycle(eng, mem, cpu, resid.as_deref_mut(), &mut total_cyc);
                        // Only stall on READ cycles — WRITE/INTERNAL proceed regardless.
                        if !(ba_blocks_cpu && bus_phase == BusPhase::Read) {
                            break;
                        }
                    }
                }
                // After full instruction: check IRQ. Use 7-cycle dispatch to account for
                // dispatch cycles in the cycle count, matching Python's _handle_irq.
                mem.recompute_pending_irq();
                if irq_should_dispatch(cpu, mem.pending_irq) {
                    dispatch_irq_hybrid(cpu, mem, eng, resid.as_deref_mut(), &mut total_cyc);
                }
            } else {
                coarse_cycles(cpu, mem, c, resid.as_deref_mut());
                total_cyc += u64::from(c);
            }
        } else {
            coarse_cycles(cpu, mem, c, resid.as_deref_mut());
            total_cyc += u64::from(c);
        }
        ins += 1;
    }
    (ins, total_cyc)
}
