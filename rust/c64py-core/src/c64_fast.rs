//! Run many instructions on the fast path (GIL released by caller).

use crate::c64_cpu::{execute_opcode, read_word_at, set_flag, CpuState};
use crate::c64_memory::C64MemoryMap;
use crate::c64_timing::{advance_raster, update_cia_timers};

const IRQ_VECTOR: u16 = 0xFFFE;

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

/// Run up to `max_instructions` instructions. Returns (instructions_executed, cycles_consumed).
///
/// If `cpu.pc` is in `stop_pcs` at the start of an instruction, exits immediately without
/// executing that instruction (so Python can run hooks / CHROUT / etc.).
pub fn run_fast_batch(
    cpu: &mut CpuState,
    mem: &mut C64MemoryMap<'_>,
    max_instructions: u64,
    stop_pcs: &[u16],
) -> (u64, u64) {
    let mut ins = 0u64;
    let mut total_cyc = 0u64;
    for _ in 0..max_instructions {
        if cpu.stopped {
            break;
        }
        if pc_in_stop_set(cpu.pc, stop_pcs) {
            break;
        }
        let opc = mem.read(cpu.pc);
        let mut c = execute_opcode(cpu, mem, opc);
        if c == 0 && !cpu.stopped {
            c = 1;
        }
        advance_raster(mem, c as u32);
        cpu.cycles = cpu.cycles.wrapping_add(u64::from(c));
        update_cia_timers(mem, c as u32, false);
        mem.recompute_pending_irq();
        if mem.pending_irq && (cpu.p & 0x04) == 0 {
            if (mem.cia1_icr & 0x80) != 0 {
                handle_irq_fast(cpu, mem);
            }
        }
        total_cyc += u64::from(c);
        ins += 1;
    }
    (ins, total_cyc)
}
