//! 6502 + fast-path stepping (mirrors Python `CPU6502` / `_execute_opcode` + helpers).
//!
//! Generated opcode bodies: `cpu_ops_generated.rs` (see `scripts/emit_rust_cpu_ops.py`).
//! Match arms: `execute_opcode_match.rs` (see `scripts/py_execute_to_rust_match.py`).

use crate::c64_memory::C64MemoryMap;

#[derive(Clone, Debug, Default)]
pub struct CpuState {
    pub pc: u16,
    pub a: u8,
    pub x: u8,
    pub y: u8,
    pub sp: u8,
    pub p: u8,
    pub cycles: u64,
    pub stopped: bool,
}

#[inline]
pub fn read_word_at(mem: &mut C64MemoryMap<'_>, addr: u16) -> u16 {
    let lo = u16::from(mem.read(addr));
    let hi = u16::from(mem.read(addr.wrapping_add(1)));
    lo | (hi << 8)
}

#[inline]
pub fn mr(mem: &mut C64MemoryMap<'_>, _cpu: &CpuState, addr: u16) -> u8 {
    mem.read(addr)
}

#[inline]
pub fn mw(mem: &mut C64MemoryMap<'_>, _cpu: &CpuState, addr: u16, value: u8) {
    mem.write(addr, value);
}

#[inline]
pub fn page_crossed(base: u16, off: u8) -> bool {
    (base & 0xFF00) != base.wrapping_add(u16::from(off)) & 0xFF00
}

#[inline]
pub fn update_nz(cpu: &mut CpuState, value: u8) {
    set_flag(cpu, 0x02, value == 0);
    set_flag(cpu, 0x80, (value & 0x80) != 0);
}

#[inline]
pub fn set_flag(cpu: &mut CpuState, mask: u8, on: bool) {
    if on {
        cpu.p |= mask;
    } else {
        cpu.p &= !mask;
    }
}

#[inline]
pub fn adc_finish(cpu: &mut CpuState, old_a: u8, value: u8, wide_result: u32) {
    set_flag(cpu, 0x01, wide_result > 0xFF);
    let r = (wide_result & 0xFF) as u8;
    let v = ((!(old_a ^ value)) & (old_a ^ r)) & 0x80 != 0;
    set_flag(cpu, 0x40, v);
    cpu.a = r;
    update_nz(cpu, cpu.a);
}

#[inline]
pub fn rmw_dummy_6510(mem: &mut C64MemoryMap<'_>, cpu: &CpuState, addr: u16, read_value: u8) {
    let a = addr as usize;
    if a <= 1 {
        mw(mem, cpu, addr, read_value);
    }
}

pub fn branch(cpu: &mut CpuState, mem: &mut C64MemoryMap<'_>, condition: bool) -> u32 {
    let off_b = mr(mem, cpu, cpu.pc.wrapping_add(1));
    let offset = i32::from(off_b as i8);
    if condition {
        let old_pc = cpu.pc.wrapping_add(2);
        let new_pc = (i32::from(old_pc) + offset) as u16;
        cpu.pc = new_pc;
        if (old_pc & 0xFF00) != (new_pc & 0xFF00) {
            4
        } else {
            3
        }
    } else {
        cpu.pc = cpu.pc.wrapping_add(2);
        2
    }
}

/// JMP indirect (6502 page-wrap bug).
pub fn jmp_ind(cpu: &mut CpuState, mem: &mut C64MemoryMap<'_>) -> u32 {
    let addr = read_word_at(mem, cpu.pc.wrapping_add(1));
    let (lo, hi) = if (addr & 0xFF) == 0xFF {
        let lo = mr(mem, cpu, addr);
        let hi = mr(mem, cpu, addr & 0xFF00);
        (lo, hi)
    } else {
        let lo = mr(mem, cpu, addr);
        let hi = mr(mem, cpu, addr.wrapping_add(1));
        (lo, hi)
    };
    cpu.pc = u16::from(lo) | (u16::from(hi) << 8);
    5
}

pub fn execute_opcode(cpu: &mut CpuState, mem: &mut C64MemoryMap<'_>, opcode: u8) -> u32 {
    include!(concat!(env!("CARGO_MANIFEST_DIR"), "/src/execute_opcode_match.rs"));
}

include!("cpu_ops_generated.rs");
