//! Fast-path raster + CIA updates (mirrors `cpu.py` coarse mode).

use crate::c64_memory::C64MemoryMap;

pub fn advance_raster(mem: &mut C64MemoryMap<'_>, cycles: u32) {
    let raster_max = if mem.video_standard == 0 { 312u16 } else { 263 };
    let cycles_per_line = if mem.video_standard == 0 { 63u32 } else { 65 };
    let step_cycles = cycles.max(1);
    mem.raster_cycles = mem.raster_cycles.saturating_add(step_cycles);
    while mem.raster_cycles >= cycles_per_line {
        mem.raster_cycles -= cycles_per_line;
        mem.raster_line = (mem.raster_line + 1) % raster_max;
    }
}

pub fn update_cia_timers(mem: &mut C64MemoryMap<'_>, cycles: u32, recompute_irq: bool) {
    let t_a_running = mem.cia1_timer_a.running;
    let t_b_running = mem.cia1_timer_b.running;
    if !recompute_irq && !t_a_running && !t_b_running {
        return;
    }

    if mem.cia1_timer_a.update(cycles) {
        if mem.cia1_timer_a.irq_enabled {
            mem.cia1_icr |= 0x01;
            mem.cia1_icr |= 0x80;
        }
        mem.cia1_timer_a.counter = i32::from(mem.cia1_timer_a.latch);
    }

    let timer_a_underflow =
        mem.cia1_timer_a.counter <= 0 && mem.cia1_timer_a.running;

    if mem.cia1_timer_b.input_mode == 2 {
        if timer_a_underflow {
            if mem.cia1_timer_b.update(1) {
                if mem.cia1_timer_b.irq_enabled {
                    mem.cia1_icr |= 0x02;
                    mem.cia1_icr |= 0x80;
                }
                mem.cia1_timer_b.counter = i32::from(mem.cia1_timer_b.latch);
            }
        }
    } else if mem.cia1_timer_b.update(cycles) {
        if mem.cia1_timer_b.irq_enabled {
            mem.cia1_icr |= 0x02;
            mem.cia1_icr |= 0x80;
        }
        mem.cia1_timer_b.counter = i32::from(mem.cia1_timer_b.latch);
    }

    if recompute_irq {
        mem.recompute_pending_irq();
    }
}
