//! VIC-II cycle engine for Rust hybrid path — PAL 6569R3 and NTSC MOS 6567R8 (VICE ``cycle_tab_*``).
//! **Limitation:** no CPU BA-stall arbitration here; that still requires per-bus-phase work in Python.

use crate::c64_memory::C64MemoryMap;

/// (sprite_ba_mask, fetch_ba as 0/1, phi2_fetch_c, visible) per PAL cycle index 0..62 → VICE cycles 1..63.
const PAL_6569R3_CYCLE_TABLE: [(u32, u32, u32, u32); 63] = [
    (0b00011000, 0, 0, 0),
    (0b00111000, 0, 0, 0),
    (0b00110000, 0, 0, 0),
    (0b01110000, 0, 0, 0),
    (0b01100000, 0, 0, 0),
    (0b11100000, 0, 0, 0),
    (0b11000000, 0, 0, 0),
    (0b11000000, 0, 0, 0),
    (0b10000000, 0, 0, 0),
    (0b10000000, 0, 0, 0),
    (0x00, 0, 0, 0),
    (0x00, 1, 0, 0),
    (0x00, 1, 0, 0),
    (0x00, 1, 0, 0),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0b00000001, 0, 0, 0),
    (0b00000001, 0, 0, 0),
    (0b00000011, 0, 0, 0),
    (0b00000111, 0, 0, 0),
    (0b00000110, 0, 0, 0),
    (0b00001110, 0, 0, 0),
    (0b00001100, 0, 0, 0),
    (0b00011100, 0, 0, 0),
    (0b00011000, 0, 0, 0),
];

/// NTSC MOS 6567R8 — VICE ``cycle_tab_ntsc`` (65 entries; index 0..64 ≡ cycles 1..65).
const NTSC_6567R8_CYCLE_TABLE: [(u32, u32, u32, u32); 65] = [
    (0b00111000, 0, 0, 0),
    (0b00110000, 0, 0, 0),
    (0b01110000, 0, 0, 0),
    (0b01100000, 0, 0, 0),
    (0b11100000, 0, 0, 0),
    (0b11000000, 0, 0, 0),
    (0b11000000, 0, 0, 0),
    (0b10000000, 0, 0, 0),
    (0b10000000, 0, 0, 0),
    (0x00, 0, 0, 0),
    (0x00, 0, 0, 0),
    (0x00, 1, 0, 0),
    (0x00, 1, 0, 0),
    (0x00, 1, 0, 0),
    (0x00, 1, 1, 0),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 1, 1, 1),
    (0x00, 0, 0, 1),
    (0b00000001, 0, 0, 0),
    (0b00000001, 0, 0, 0),
    (0b00000011, 0, 0, 0),
    (0b00000011, 0, 0, 0),
    (0b00000111, 0, 0, 0),
    (0b00000110, 0, 0, 0),
    (0b00001110, 0, 0, 0),
    (0b00001100, 0, 0, 0),
    (0b00011100, 0, 0, 0),
    (0b00011000, 0, 0, 0),
];

#[derive(Clone, Debug)]
pub struct ViciiEngine {
    pub raster_line: u16,
    pub raster_cycle: u32,
    pub cycles_per_line: u32,
    pub num_raster_lines: u16,
    pub allow_bad_lines: bool,
    pub bad_line: bool,
    pub ysmooth: u8,
    pub den: bool,
    pub raster_irq_line: u16,
    pub raster_irq_triggered: bool,
    pub prefetch_cycles: u32,
    pub first_dma_line: u16,
    pub last_dma_line: u16,
    pub sprite_enable_mask: u32,
}

impl Default for ViciiEngine {
    fn default() -> Self {
        Self {
            raster_line: 0,
            raster_cycle: 0,
            cycles_per_line: 63,
            num_raster_lines: 312,
            allow_bad_lines: false,
            bad_line: false,
            ysmooth: 0,
            den: false,
            raster_irq_line: 0,
            raster_irq_triggered: false,
            prefetch_cycles: 0,
            first_dma_line: 48,
            last_dma_line: 247,
            sprite_enable_mask: 0,
        }
    }
}

impl ViciiEngine {
    pub fn from_python_state(
        raster_line: u16,
        raster_cycle: u32,
        allow_bad_lines: bool,
        bad_line: bool,
        ysmooth: u8,
        den: bool,
        raster_irq_line: u16,
        raster_irq_triggered: bool,
        prefetch_cycles: u32,
        first_dma_line: u16,
        last_dma_line: u16,
        sprite_enable_mask: u32,
        cycles_per_line: u32,
        num_raster_lines: u16,
    ) -> Self {
        Self {
            raster_line,
            raster_cycle,
            allow_bad_lines,
            bad_line,
            ysmooth,
            den,
            raster_irq_line,
            raster_irq_triggered,
            prefetch_cycles,
            first_dma_line,
            last_dma_line,
            sprite_enable_mask,
            cycles_per_line,
            num_raster_lines,
        }
    }

    fn set_d011(&mut self, d011: u8, _current_raster_msb: u8) {
        self.ysmooth = d011 & 0x07;
        self.den = (d011 & 0x10) != 0;
        self.raster_irq_line =
            (self.raster_irq_line & 0xFF) | (u16::from(d011 & 0x80) << 1);
    }

    fn set_d012(&mut self, d012: u8) {
        self.raster_irq_line = (self.raster_irq_line & 0x100) | u16::from(d012);
    }

    fn sync_shadow_from_mem(&mut self, mem: &C64MemoryMap<'_>) {
        let d011 = mem.vic_regs[0x11];
        let d012 = mem.vic_regs[0x12];
        let sp = mem.vic_regs[0x15];
        self.set_d011(d011, 0);
        self.set_d012(d012);
        self.sprite_enable_mask = u32::from(sp);
    }

    fn cycle_start_of_line(&mut self) {
        if self.raster_line == self.first_dma_line
            && !self.allow_bad_lines
            && self.den
        {
            self.allow_bad_lines = true;
        }
        if self.raster_line == self.last_dma_line {
            self.allow_bad_lines = false;
        }
        self.bad_line = false;
    }

    /// One CPU cycle: update raster, optional raster IRQ edge, mirror into ``mem``.
    pub fn step(&mut self, mem: &mut C64MemoryMap<'_>) -> bool {
        self.sync_shadow_from_mem(mem);

        let rc = self.raster_cycle;

        if !self.allow_bad_lines {
            self.bad_line = false;
        } else {
            let rl = self.raster_line;
            if rl < self.first_dma_line || rl > self.last_dma_line {
                self.bad_line = false;
            } else {
                self.bad_line = (rl & 7) == u16::from(self.ysmooth & 7);
            }
        }

        let irq_edge = if self.raster_line == self.raster_irq_line {
            let e = !self.raster_irq_triggered;
            self.raster_irq_triggered = true;
            e
        } else {
            self.raster_irq_triggered = false;
            false
        };

        let idx = rc as usize;
        let (sprite_ba_mask, fetch_ba, _phi2, _vis) = if mem.video_standard == 0 {
            PAL_6569R3_CYCLE_TABLE[idx]
        } else {
            NTSC_6567R8_CYCLE_TABLE[idx]
        };

        let ba_matrix = self.bad_line && fetch_ba != 0;
        let sprite_ba = (sprite_ba_mask & self.sprite_enable_mask) != 0;
        let ba_low = ba_matrix || sprite_ba;

        if ba_low {
            if self.prefetch_cycles > 0 {
                self.prefetch_cycles -= 1;
            }
        } else {
            self.prefetch_cycles = 4;
        }

        self.raster_cycle += 1;
        let mut line_advanced = false;
        if self.raster_cycle >= self.cycles_per_line {
            self.raster_cycle = 0;
            self.raster_line = (self.raster_line + 1) % self.num_raster_lines;
            line_advanced = true;
            self.cycle_start_of_line();
        }

        mem.raster_line = self.raster_line;
        mem.raster_cycles = self.raster_cycle;
        if line_advanced {
            mem.beam_capture_current_line();
        }

        irq_edge
    }

    /// Pack engine state for PyO3 return (14 × u32).
    pub fn export_u32(&self) -> [u32; 14] {
        [
            u32::from(self.raster_line),
            self.raster_cycle,
            u32::from(self.allow_bad_lines as u8),
            u32::from(self.bad_line as u8),
            u32::from(self.ysmooth),
            u32::from(self.den as u8),
            u32::from(self.raster_irq_line),
            u32::from(self.raster_irq_triggered as u8),
            self.prefetch_cycles,
            u32::from(self.first_dma_line),
            u32::from(self.last_dma_line),
            self.sprite_enable_mask,
            self.cycles_per_line,
            u32::from(self.num_raster_lines),
        ]
    }
}
