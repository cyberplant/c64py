//! Run many instructions on the fast path (GIL held by PyO3 caller).

use crate::c64_cpu::{execute_opcode, read_word_at, set_flag, CpuState};
use crate::c64_memory::C64MemoryMap;
use crate::c64_timing::{advance_raster, update_cia_timers};
use crate::c64_vicii::ViciiEngine;
use crate::resid_session::ResidSession;

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

fn service_irq_if_any(cpu: &mut CpuState, mem: &mut C64MemoryMap<'_>) {
    mem.recompute_pending_irq();
    if mem.pending_irq && (cpu.p & 0x04) == 0 {
        if (mem.cia1_icr & 0x80) != 0 {
            handle_irq_fast(cpu, mem);
        }
    }
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

/// Run up to `max_instructions` instructions.
///
/// * `hybrid_vic` — after each opcode, step VIC-II engine once per CPU cycle (PAL or NTSC table).
///   Does **not** model BA CPU stalls (see ``c64_vicii``).
/// * `resid` — optional reSID session (lockstep); clocks each emulated cycle appropriately.
pub fn run_fast_batch(
    cpu: &mut CpuState,
    mem: &mut C64MemoryMap<'_>,
    max_instructions: u64,
    stop_pcs: &[u16],
    hybrid_vic: bool,
    mut vicii: Option<&mut ViciiEngine>,
    mut resid: Option<&mut ResidSession>,
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
        let c = c as u32;

        if hybrid_vic {
            if let Some(eng) = vicii.as_deref_mut() {
                for _ in 0..c {
                    let irq_edge = eng.step(mem);
                    if irq_edge {
                        mem.trigger_vic_irq(0x01);
                    }
                    cpu.cycles = cpu.cycles.wrapping_add(1);
                    update_cia_timers(mem, 1, false);
                    if let Some(r) = resid.as_deref_mut() {
                        r.clock_cycles(1);
                    }
                    service_irq_if_any(cpu, mem);
                    total_cyc += 1;
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
