//! VIC-II cycle engine for Rust hybrid path — PAL 6569R3 and NTSC MOS 6567R8 (VICE ``cycle_tab_*``).
//! BA-stall arbitration: ``step()`` returns ``(irq_edge, ba_blocks_cpu)`` so callers can stall the CPU.

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
    /// VICE-style sprite DMA bitmask (not to be confused with $D015 enable).
    /// BA arbitration gates on this mask, matching ``vicii.sprite_dma`` in VICE
    /// (vicii-fetch.c → ``vicii_check_sprite_ba``). A sprite enters this mask
    /// at cycles 55/56 (``check_sprite_dma``) when Y matches and leaves it at
    /// cycle 16 (``sprite_mcbase_update``) when mcbase reaches 63.
    pub sprite_dma_mask: u32,
    /// Y-expansion flip-flop bitmask (``sprite[i].exp_flop`` in VICE). Bit i
    /// set ⇒ next ``sprite_mcbase_update`` copies mc→mcbase for sprite i.
    /// Toggled at cycle 56 when $D017 bit i is set; reset to 1 when DMA turns on.
    pub sprite_exp_flop: u32,
    /// $D017 Y-expansion mask (cached from ``vic_regs[0x17]`` each cycle).
    pub sprite_y_expand_mask: u32,
    /// Per-sprite Y positions ($D001, $D003, ..., $D00F). Compared against
    /// ``raster_line & 0xFF`` in ``check_sprite_dma``.
    pub sprite_y: [u8; 8],
    /// Per-sprite MC counter (0..63). Incremented by 3 per active-DMA line to
    /// simulate the 3 sprite-DMA fetches (vicii-cycle.c ``sprite_dma_cycle_0/2``).
    pub sprite_mc: [u8; 8],
    /// Per-sprite MCBASE counter (0..63). Copied from ``mc`` at cycle 16 when
    /// ``exp_flop`` is set; hitting 63 turns off DMA for that sprite.
    pub sprite_mcbase: [u8; 8],
    /// Sprite-to-sprite collision register ($D01E)
    pub sprite_sprite_collision: u8,
    /// Sprite-to-background collision register ($D01F)
    pub sprite_bg_collision: u8,
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
            sprite_dma_mask: 0,
            sprite_exp_flop: 0xFF,
            sprite_y_expand_mask: 0,
            sprite_y: [0; 8],
            sprite_mc: [0; 8],
            sprite_mcbase: [0; 8],
            sprite_sprite_collision: 0,
            sprite_bg_collision: 0,
        }
    }
}

impl ViciiEngine {
    #[allow(clippy::too_many_arguments)]
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
        // VICE-style sprite DMA state (round-tripped with Python engine).
        sprite_dma_mask: u32,
        sprite_exp_flop: u32,
        sprite_y_expand_mask: u32,
        sprite_y_lo: u32, // Y[0..4] packed low-first (Y0 in low byte)
        sprite_y_hi: u32, // Y[4..8] packed low-first (Y4 in low byte)
        sprite_mc_lo: u32,
        sprite_mc_hi: u32,
        sprite_mcbase_lo: u32,
        sprite_mcbase_hi: u32,
    ) -> Self {
        let unpack4 = |lo: u32, hi: u32| -> [u8; 8] {
            [
                (lo & 0xFF) as u8,
                ((lo >> 8) & 0xFF) as u8,
                ((lo >> 16) & 0xFF) as u8,
                ((lo >> 24) & 0xFF) as u8,
                (hi & 0xFF) as u8,
                ((hi >> 8) & 0xFF) as u8,
                ((hi >> 16) & 0xFF) as u8,
                ((hi >> 24) & 0xFF) as u8,
            ]
        };
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
            sprite_dma_mask,
            sprite_exp_flop,
            sprite_y_expand_mask,
            sprite_y: unpack4(sprite_y_lo, sprite_y_hi),
            sprite_mc: unpack4(sprite_mc_lo, sprite_mc_hi),
            sprite_mcbase: unpack4(sprite_mcbase_lo, sprite_mcbase_hi),
            sprite_sprite_collision: 0,
            sprite_bg_collision: 0,
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
        self.sprite_y_expand_mask = u32::from(mem.vic_regs[0x17]);
        for i in 0..8 {
            self.sprite_y[i] = mem.vic_regs[i * 2 + 1];
        }
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
        // Simulate the 3 sprite-DMA fetches per active-DMA sprite at line wrap.
        // In VICE these happen at per-sprite cycles 58..10 across the line and
        // each increments mc once; collapsing to a single +3 here is
        // BA-equivalent because mcbase_update (cycle 16) only reads the final
        // value. See vicii-cycle.c sprite_dma_cycle_0/2 and fetch_sprite_dma_1.
        if self.sprite_dma_mask != 0 {
            for i in 0..8 {
                if (self.sprite_dma_mask >> i) & 1 != 0 {
                    self.sprite_mc[i] = (self.sprite_mc[i].wrapping_add(3)) & 0x3F;
                }
            }
        }
    }

    /// Mirror of VICE ``sprite_mcbase_update`` (vicii-cycle.c). Runs at PAL cycle 16.
    fn sprite_mcbase_update(&mut self) {
        for i in 0..8 {
            let bit = 1u32 << i;
            if self.sprite_exp_flop & bit != 0 {
                self.sprite_mcbase[i] = self.sprite_mc[i];
                if self.sprite_mcbase[i] == 63 {
                    self.sprite_dma_mask &= !bit;
                }
            }
        }
    }

    /// Mirror of VICE ``check_sprite_dma`` (vicii-cycle.c). Runs at PAL cycles 55 & 56.
    /// For each enabled sprite with Y matching the current raster line's low byte
    /// that is not already DMA-active, turn DMA on.
    fn check_sprite_dma(&mut self) {
        let enable = (self.sprite_enable_mask & 0xFF) as u8;
        let rl_low = (self.raster_line & 0xFF) as u8;
        for i in 0..8 {
            let bit = 1u32 << i;
            if (enable & (1 << i)) != 0
                && self.sprite_y[i] == rl_low
                && (self.sprite_dma_mask & bit) == 0
            {
                self.sprite_dma_mask |= bit;
                self.sprite_mcbase[i] = 0;
                self.sprite_exp_flop |= bit;
            }
        }
    }

    /// Mirror of VICE ``check_exp`` (vicii-cycle.c). Runs at PAL cycle 56.
    fn check_exp(&mut self) {
        let togglable = self.sprite_dma_mask & self.sprite_y_expand_mask & 0xFF;
        self.sprite_exp_flop ^= togglable;
    }

    /// Per-scanline sprite collision detection. Honors sprite multicolor,
    /// X-expand ($D01D) and Y-expand ($D017), reads actual sprite bitmap
    /// data from the VIC bank, and computes sprite-bg collision against
    /// the current scanline's foreground mask in standard text, multicolor
    /// text, hires bitmap, multicolor bitmap, and ECM modes.
    ///
    /// `mem.vic_regs[0x1E]` and `[0x1F]` are the authoritative latches:
    /// new collisions are OR'd into them; clear-on-read in `c64_memory.rs`
    /// resets them. The `sprite_sprite_collision` / `sprite_bg_collision`
    /// shadow fields are kept synced for legacy state-export only.
    fn detect_collisions(&mut self, mem: &mut C64MemoryMap<'_>) {
        let enabled = (self.sprite_enable_mask & 0xFF) as u8;
        if enabled == 0 {
            // Sync shadow → vic_regs (in case external clear happened).
            mem.vic_regs[0x1E] |= self.sprite_sprite_collision;
            mem.vic_regs[0x1F] |= self.sprite_bg_collision;
            self.sprite_sprite_collision = mem.vic_regs[0x1E];
            self.sprite_bg_collision = mem.vic_regs[0x1F];
            return;
        }

        // The hardware sets collision bits as the raster scans through the
        // sprite. We approximate by computing each enabled sprite's row
        // (if any) intersecting the just-completed raster line.
        // step() calls us right after `raster_line` advanced, so collisions
        // for the previous line are detected here.
        let scanline = if self.raster_line == 0 {
            self.num_raster_lines.saturating_sub(1)
        } else {
            self.raster_line - 1
        };

        let d011 = mem.vic_regs[0x11];
        let d016 = mem.vic_regs[0x16];
        let d017 = mem.vic_regs[0x17]; // Y-expand
        let d018 = mem.vic_regs[0x18];
        let d01c = mem.vic_regs[0x1C]; // sprite multicolor
        let d01d = mem.vic_regs[0x1D]; // X-expand
        let xmsb = mem.vic_regs[0x10];

        // Each enabled sprite that has a row visible on this scanline gets
        // a 24- or 48-bit opaque mask. We anchor it in a 512-bit raster
        // bitvector so we can AND across sprites without per-pair shifts.
        let mut row_mask: [[u32; 16]; 8] = [[0u32; 16]; 8];
        let mut row_active = [false; 8];

        // Sprite pointers live in the VIC bank's screen matrix.
        let cia2_pra = mem.cia2_pra;
        let vic_bank: usize = (((!cia2_pra) & 0x03) as usize) * 0x4000;
        let screen_base: usize = vic_bank + (((d018 >> 4) & 0x0F) as usize) * 0x400;

        for i in 0..8 {
            if (enabled & (1 << i)) == 0 {
                continue;
            }
            let y0 = u16::from(mem.vic_regs[i * 2 + 1]);
            let y_exp = (d017 & (1 << i)) != 0;
            let height = if y_exp { 42u16 } else { 21u16 };
            // Sprite Y wraps in raster space; for typical games sprites are
            // placed in the visible range so wrap is a non-issue here.
            if scanline < y0 || scanline >= y0.saturating_add(height) {
                continue;
            }
            let line_in_sprite = (scanline - y0) as u16;
            let src_row = if y_exp {
                (line_in_sprite / 2) as usize
            } else {
                line_in_sprite as usize
            };

            let pointer = mem.ram[(screen_base + 0x3F8 + i) & 0xFFFF];
            let sprite_data_addr = vic_bank + ((pointer as usize) << 6);
            let b0 = mem.ram[(sprite_data_addr + src_row * 3) & 0xFFFF];
            let b1 = mem.ram[(sprite_data_addr + src_row * 3 + 1) & 0xFFFF];
            let b2 = mem.ram[(sprite_data_addr + src_row * 3 + 2) & 0xFFFF];

            let is_mc = (d01c & (1 << i)) != 0;
            // Build 24-bit opaque mask in bits 23..0 (bit 23 = leftmost pixel).
            let raw24: u32 =
                (u32::from(b0) << 16) | (u32::from(b1) << 8) | u32::from(b2);
            let opaque24: u32 = if is_mc {
                // Each "ab" pair is opaque iff (a|b)≠0; both pixels in the pair
                // become 1 in the opacity mask. Pairs span bits (23,22),(21,20),...
                let mut o = 0u32;
                for p in 0..12 {
                    let pair = (raw24 >> (22 - p * 2)) & 0b11;
                    if pair != 0 {
                        o |= 0b11 << (22 - p * 2);
                    }
                }
                o
            } else {
                raw24
            };

            // Apply X-expand: each bit becomes 2 bits (48 hires pixels wide).
            let x_exp = (d01d & (1 << i)) != 0;
            let width: u32 = if x_exp { 48 } else { 24 };
            let expanded: u64 = if x_exp {
                let mut e: u64 = 0;
                for k in 0..24 {
                    if (opaque24 >> (23 - k)) & 1 != 0 {
                        e |= 0b11u64 << (46 - k * 2);
                    }
                }
                e
            } else {
                u64::from(opaque24)
            };

            // Place into the 512-bit raster bitvector at sprite X.
            let x0 = u32::from(mem.vic_regs[i * 2])
                | (if (xmsb & (1 << i)) != 0 { 0x100 } else { 0 });
            place_bits(&mut row_mask[i], x0, expanded, width);
            row_active[i] = true;
        }

        // Sprite-sprite per-pixel: AND every active pair.
        let mut sprite_sprite_new = 0u8;
        for i in 0..8 {
            if !row_active[i] {
                continue;
            }
            for j in (i + 1)..8 {
                if !row_active[j] {
                    continue;
                }
                let mut hit = 0u32;
                for k in 0..16 {
                    hit |= row_mask[i][k] & row_mask[j][k];
                }
                if hit != 0 {
                    sprite_sprite_new |= (1u8 << i) | (1u8 << j);
                }
            }
        }

        // Sprite-bg per-pixel: AND each active sprite row with the actual
        // foreground mask of this raster line within the display window.
        let mut sprite_bg_new = 0u8;
        let any_sprite_row =
            row_active.iter().any(|&v| v) || sprite_sprite_new != 0;
        if any_sprite_row {
            // Display window per $D011 (DEN, RSEL) and $D016 (CSEL).
            let den = (d011 & 0x10) != 0;
            let rsel = (d011 & 0x08) != 0;
            let display_top = if rsel { 51u16 } else { 55u16 };
            let display_bot = if rsel { 251u16 } else { 247u16 };
            if den && scanline >= display_top && scanline < display_bot {
                let bg_fg = compute_bg_fg_mask(mem, scanline, vic_bank, d011, d016, d018);
                for i in 0..8 {
                    if !row_active[i] {
                        continue;
                    }
                    let mut hit = 0u32;
                    for k in 0..16 {
                        hit |= row_mask[i][k] & bg_fg[k];
                    }
                    if hit != 0 {
                        sprite_bg_new |= 1u8 << i;
                    }
                }
            }
        }

        // OR into authoritative latches in vic_regs and keep the shadow
        // copies in sync for state export.
        let new_sprite = mem.vic_regs[0x1E] | self.sprite_sprite_collision | sprite_sprite_new;
        let new_bg = mem.vic_regs[0x1F] | self.sprite_bg_collision | sprite_bg_new;
        mem.vic_regs[0x1E] = new_sprite;
        mem.vic_regs[0x1F] = new_bg;
        self.sprite_sprite_collision = new_sprite;
        self.sprite_bg_collision = new_bg;
    }

    /// One CPU cycle: update raster, optional raster IRQ edge, mirror into ``mem``.
    /// Returns ``(irq_edge, ba_blocks_cpu)`` where ``ba_blocks_cpu`` mirrors Python's
    /// ``ba_blocks_cpu = ba_low and (prefetch_cycles == 0)``: the CPU stall gate.
    pub fn step(&mut self, mem: &mut C64MemoryMap<'_>) -> (bool, bool) {
        self.sync_shadow_from_mem(mem);

        let mut line_advanced = false;
        self.raster_cycle += 1;
        if self.raster_cycle >= self.cycles_per_line {
            self.raster_cycle = 0;
            self.raster_line = (self.raster_line + 1) % self.num_raster_lines;
            line_advanced = true;
            self.cycle_start_of_line();
            // Detect sprite collisions at the start of each new scanline
            self.detect_collisions(mem);
        }

        // VICE sprite DMA state machine (vicii-cycle.c) at PAL cycles 16/55/56
        // (0-based indices 15/54/55). Must run BEFORE BA computation below.
        match self.raster_cycle {
            15 => self.sprite_mcbase_update(),
            54 => self.check_sprite_dma(),
            55 => {
                self.check_sprite_dma();
                self.check_exp();
            }
            _ => {}
        }

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

        let irq_edge = if self.raster_line == self.raster_irq_line && line_advanced {
            // Trigger IRQ when raster line advances to the IRQ line
            true
        } else {
            false
        };

        let idx = self.raster_cycle as usize;
        let (sprite_ba_mask, fetch_ba, _phi2, _vis) = if mem.video_standard == 0 {
            PAL_6569R3_CYCLE_TABLE[idx]
        } else {
            NTSC_6567R8_CYCLE_TABLE[idx]
        };

        let ba_matrix = self.bad_line && fetch_ba != 0;
        // VICE gates sprite BA on ``sprite_dma`` (active DMA), NOT on the CPU-
        // written $D015 enable mask — see ``vicii_check_sprite_ba`` in
        // vicii-fetch.c. Using the enable mask caused spurious per-line stalls
        // whenever sprites were enabled but outside their Y range, drifting
        // our raster IRQ position by several lines (phantom IRQ symptom).
        let sprite_ba = (sprite_ba_mask & self.sprite_dma_mask) != 0;
        let ba_low = ba_matrix || sprite_ba;

        if ba_low {
            if self.prefetch_cycles > 0 {
                self.prefetch_cycles -= 1;
            }
        } else {
            self.prefetch_cycles = 4;
        }

        // ba_blocks_cpu: matches Python ViciiCycleEngine — stall only after prefetch drained.
        let ba_blocks_cpu = ba_low && (self.prefetch_cycles == 0);

        mem.raster_line = self.raster_line;
        mem.raster_cycles = self.raster_cycle;
        if line_advanced {
            mem.beam_capture_current_line();
        }

        (irq_edge, ba_blocks_cpu)
    }

    /// Pack engine state for PyO3 return (22 × u32). Indices 16..=21 carry the
    /// VICE-style sprite DMA state (mask, exp_flop, and packed mc/mcbase arrays).
    pub fn export_u32(&self) -> [u32; 22] {
        let pack4 = |a: &[u8; 8], off: usize| -> u32 {
            u32::from(a[off])
                | (u32::from(a[off + 1]) << 8)
                | (u32::from(a[off + 2]) << 16)
                | (u32::from(a[off + 3]) << 24)
        };
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
            u32::from(self.sprite_sprite_collision),
            u32::from(self.sprite_bg_collision),
            self.sprite_dma_mask,
            self.sprite_exp_flop,
            pack4(&self.sprite_mc, 0),
            pack4(&self.sprite_mc, 4),
            pack4(&self.sprite_mcbase, 0),
            pack4(&self.sprite_mcbase, 4),
        ]
    }
}

/// Place `width` bits of `mask` (in bit positions `width-1..0` of `mask`)
/// into the 512-bit raster bitvector `out` starting at raster x = `x_start`,
/// with bit (x_start + k) representing the k-th-leftmost source bit.
/// Bits beyond raster x ≥ 512 are clipped.
fn place_bits(out: &mut [u32; 16], x_start: u32, mask: u64, width: u32) {
    if width == 0 {
        return;
    }
    for k in 0..width {
        let src_bit = (mask >> (width - 1 - k)) & 1;
        if src_bit == 0 {
            continue;
        }
        let x = x_start + k;
        if x >= 512 {
            break;
        }
        out[(x / 32) as usize] |= 1u32 << (x % 32);
    }
}

/// Read a byte through the VIC's address space for a bank-relative address.
/// VIC sees char ROM at bank-relative 0x1000..0x1FFF in banks 0 and 2;
/// elsewhere it reads RAM at `vic_bank + addr`.
fn vic_read(mem: &C64MemoryMap<'_>, vic_bank: usize, addr: usize) -> u8 {
    let bank_idx = vic_bank / 0x4000;
    let in_char_window = (0x1000..0x2000).contains(&addr);
    if in_char_window && (bank_idx == 0 || bank_idx == 2) {
        if let Some(cr) = mem.char_rom {
            let off = addr - 0x1000;
            if off < cr.len() {
                return cr[off];
            }
        }
    }
    mem.ram[(vic_bank + addr) & 0xFFFF]
}

/// Build a 512-bit foreground mask for raster line `scanline` covering 320
/// display pixels starting at raster x = 24 (left edge of the 40-column area).
/// `bit (24 + k)` is set iff the rendered pixel at column k of this line is
/// "foreground" for sprite-bg collision purposes (Christian Bauer, "VIC
/// article", §3.7.1):
///
///   - Standard text / hires bitmap: pixel set ⇒ FG
///   - Multicolor text / multicolor bitmap: pair "10" or "11" ⇒ FG; "00" / "01" ⇒ BG
///   - ECM: top 2 bits select bg color; remaining 6 bits index char data
///     and the pixel rule is the same as standard text
fn compute_bg_fg_mask(
    mem: &C64MemoryMap<'_>,
    scanline: u16,
    vic_bank: usize,
    d011: u8,
    d016: u8,
    d018: u8,
) -> [u32; 16] {
    let mut out = [0u32; 16];
    // 25-row mode top is raster 51, 24-row top is 55. Display has 25 rows × 8 = 200 lines.
    let rsel = (d011 & 0x08) != 0;
    let display_top = if rsel { 51u16 } else { 55u16 };
    if scanline < display_top || scanline >= display_top + 200 {
        return out;
    }
    let display_y = scanline - display_top;
    let row = (display_y / 8) as usize;
    let line_in_row = (display_y & 7) as usize;

    let bmm = (d011 & 0x20) != 0;
    let ecm = (d011 & 0x40) != 0;
    let mcm = (d016 & 0x10) != 0;

    let screen_base: usize = (((d018 >> 4) & 0x0F) as usize) * 0x400;
    let charset_base: usize = (((d018 >> 1) & 0x07) as usize) * 0x800;
    let bitmap_base: usize = (((d018 >> 3) & 0x01) as usize) * 0x2000;

    for col in 0..40usize {
        let screen_addr = screen_base + row * 40 + col;
        let scr = vic_read(mem, vic_bank, screen_addr);
        // Color RAM is on the system data bus (not VIC bank-relative).
        let col_ram = mem.ram[0xD800 + row * 40 + col] & 0x0F;

        let char_byte: u8 = if bmm {
            // Bitmap mode: each row of 8 lines × 40 cols × 8 bits is a contiguous
            // 8000-byte block. Pixel byte = bitmap_base + row*320 + col*8 + line.
            vic_read(mem, vic_bank, bitmap_base + row * 320 + col * 8 + line_in_row)
        } else {
            // Text mode (incl. ECM): char code indexes char data.
            let code = if ecm { scr & 0x3F } else { scr };
            vic_read(mem, vic_bank, charset_base + (code as usize) * 8 + line_in_row)
        };

        // Multicolor selector: in MC text it's color RAM bit 3; in MC bitmap
        // every cell is multicolor; in standard text/bitmap none are.
        let cell_mc = if !mcm {
            false
        } else if bmm {
            true
        } else {
            (col_ram & 0x08) != 0
        };

        // Compute 8-bit FG mask for this cell.
        let cell_fg: u8 = if cell_mc {
            // Pair "10"/"11" → FG (both pixels of the pair are FG).
            let mut m = 0u8;
            for p in 0..4 {
                let pair = (char_byte >> (6 - p * 2)) & 0b11;
                if pair >= 0b10 {
                    m |= 0b11 << (6 - p * 2);
                }
            }
            m
        } else {
            char_byte
        };

        // Place at raster x = 24 + col*8.
        let x_start = 24 + col * 8;
        for bit in 0..8 {
            if (cell_fg >> (7 - bit)) & 1 != 0 {
                let x = x_start + bit;
                if x < 512 {
                    out[x / 32] |= 1u32 << (x % 32);
                }
            }
        }
    }
    out
}

#[cfg(test)]
mod collision_tests {
    use super::*;
    use crate::c64_memory::C64MemoryMap;

    /// Build an engine + memory pair primed for collision testing.
    /// Sprite enable, X/Y, multicolor, expansion are set via VIC regs.
    /// `sprite_data` is keyed by sprite index → 63-byte bitmap (rows of 3 bytes).
    fn setup(
        ram: &mut [u8; 65536],
        sprites: &[(usize, u16, u16, u8, bool, bool, bool, &[u8; 63])],
    ) -> (ViciiEngine, *mut [u8; 65536]) {
        // sprites: (idx, x, y, color, mc, x_exp, y_exp, data)
        // Place screen base at $0400 (default), sprite data ptrs at $07F8..
        // Sprite i pointer points to $40*i+$0800 → data at $0800+$40i.
        let raw: *mut [u8; 65536] = ram as *mut _;
        for (idx, _, _, _, _, _, _, data) in sprites {
            let ptr_addr = 0x07F8 + idx;
            let data_block = 0x20 + idx; // pointer * 64 = $0800 + $40*idx
            ram[ptr_addr] = data_block as u8;
            let data_addr = (data_block as usize) * 64;
            ram[data_addr..data_addr + 63].copy_from_slice(*data);
        }
        let mut eng = ViciiEngine::default();
        let mut enable = 0u8;
        let mut mc = 0u8;
        let mut xexp = 0u8;
        let mut yexp = 0u8;
        let mut xmsb = 0u8;
        let mut x_lo = [0u8; 8];
        let mut y_lo = [0u8; 8];
        for (idx, x, y, _color, m, xe, ye, _) in sprites {
            enable |= 1 << idx;
            x_lo[*idx] = (*x & 0xFF) as u8;
            if *x >= 256 {
                xmsb |= 1 << idx;
            }
            y_lo[*idx] = (*y & 0xFF) as u8;
            if *m { mc |= 1 << idx; }
            if *xe { xexp |= 1 << idx; }
            if *ye { yexp |= 1 << idx; }
        }
        eng.sprite_enable_mask = u32::from(enable);
        // VIC regs are written into mem below via the constructor wrapper.
        // We can't call C64MemoryMap::new here without lifetime gymnastics in the
        // test; the caller will set them before invoking detect_collisions.
        // Stash them in eng for the caller to copy out:
        eng.sprite_y_expand_mask = u32::from(yexp);
        // Caller must set vic_regs[0x10/0x15/0x17/0x1C/0x1D] and per-sprite x/y.
        // We pre-pack sprite_y for completeness but detect_collisions reads
        // vic_regs directly.
        let _ = (mc, xexp, x_lo, y_lo, xmsb, raw);
        (eng, ram as *mut _)
    }

    /// Make a 63-byte sprite bitmap that is fully opaque (all bits set).
    fn solid_sprite() -> [u8; 63] {
        [0xFF; 63]
    }

    fn empty_sprite() -> [u8; 63] {
        [0x00; 63]
    }

    /// Wire a sprite's enable/x/y/multicolor/expand bits into vic_regs.
    fn write_sprite_regs(
        mem: &mut C64MemoryMap<'_>,
        idx: usize,
        x: u16,
        y: u16,
        mc: bool,
        xe: bool,
        ye: bool,
    ) {
        mem.vic_regs[idx * 2] = (x & 0xFF) as u8;
        if x >= 256 {
            mem.vic_regs[0x10] |= 1 << idx;
        }
        mem.vic_regs[idx * 2 + 1] = (y & 0xFF) as u8;
        mem.vic_regs[0x15] |= 1 << idx;
        if mc { mem.vic_regs[0x1C] |= 1 << idx; }
        if xe { mem.vic_regs[0x1D] |= 1 << idx; }
        if ye { mem.vic_regs[0x17] |= 1 << idx; }
    }

    /// Place sprite data for `idx` so VIC fetches `data` from $0800+$40*idx.
    fn place_sprite_data(ram: &mut [u8; 65536], idx: usize, data: &[u8; 63]) {
        // VIC bank 0 (default cia2_pra all-bits-low → bank 3? we set explicitly).
        // We set screen_base $0400 and use VIC bank 0 in the test wrappers below.
        let ptr_addr = 0x0400 + 0x3F8 + idx;
        let data_block: u8 = (0x800 / 64 + idx as u16 * 1) as u8; // $20, $21, ...
        ram[ptr_addr] = data_block;
        let data_addr = (data_block as usize) * 64;
        ram[data_addr..data_addr + 63].copy_from_slice(data);
    }

    /// Build a fresh engine + RAM in VIC bank 0, screen $0400, char $1000.
    fn make_eng_mem<'a>(ram: &'a mut [u8; 65536]) -> (ViciiEngine, C64MemoryMap<'a>) {
        let mut mem = C64MemoryMap::new(ram);
        // VIC bank 0 = cia2_pra bits both 1 → ((!pra)&3)*0x4000 = 0.
        mem.cia2_pra = 0xFF;
        // $D018 = $14 → screen $0400, charset $1000 (irrelevant here).
        mem.vic_regs[0x18] = 0x14;
        // $D011 = $1B (DEN on, RSEL on, 25 rows, ysmooth 3) for sprite-bg path.
        mem.vic_regs[0x11] = 0x1B;
        let mut eng = ViciiEngine::default();
        // detect_collisions uses self.raster_line - 1; set so scanline = 100.
        eng.raster_line = 101;
        eng.num_raster_lines = 312;
        (eng, mem)
    }

    fn run_detect(eng: &mut ViciiEngine, mem: &mut C64MemoryMap<'_>) {
        eng.detect_collisions(mem);
    }

    #[test]
    fn x_far_apart_no_sprite_collision() {
        let mut ram = Box::new([0u8; 65536]);
        place_sprite_data(&mut ram, 0, &solid_sprite());
        place_sprite_data(&mut ram, 1, &solid_sprite());
        let (mut eng, mut mem) = make_eng_mem(&mut *ram);
        // Both sprites on raster 100 (scanline = raster_line-1 = 100).
        write_sprite_regs(&mut mem, 0, 30, 90, false, false, false);
        write_sprite_regs(&mut mem, 1, 200, 90, false, false, false);
        eng.sprite_enable_mask = 0x03;
        run_detect(&mut eng, &mut mem);
        assert_eq!(mem.vic_regs[0x1E] & 0x03, 0, "no X overlap → no collision");
    }

    #[test]
    fn x_overlap_collides() {
        let mut ram = Box::new([0u8; 65536]);
        place_sprite_data(&mut ram, 0, &solid_sprite());
        place_sprite_data(&mut ram, 1, &solid_sprite());
        let (mut eng, mut mem) = make_eng_mem(&mut *ram);
        write_sprite_regs(&mut mem, 0, 50, 90, false, false, false);
        write_sprite_regs(&mut mem, 1, 60, 90, false, false, false);
        eng.sprite_enable_mask = 0x03;
        run_detect(&mut eng, &mut mem);
        assert_eq!(mem.vic_regs[0x1E] & 0x03, 0x03);
    }

    #[test]
    fn x_just_touching_no_collision() {
        let mut ram = Box::new([0u8; 65536]);
        place_sprite_data(&mut ram, 0, &solid_sprite());
        place_sprite_data(&mut ram, 1, &solid_sprite());
        let (mut eng, mut mem) = make_eng_mem(&mut *ram);
        // Sprite 0 spans X 50..73 (24 px), sprite 1 starts at 74 → no pixel overlap.
        write_sprite_regs(&mut mem, 0, 50, 90, false, false, false);
        write_sprite_regs(&mut mem, 1, 74, 90, false, false, false);
        eng.sprite_enable_mask = 0x03;
        run_detect(&mut eng, &mut mem);
        assert_eq!(mem.vic_regs[0x1E] & 0x03, 0);
    }

    #[test]
    fn x_expand_extends_collision() {
        let mut ram = Box::new([0u8; 65536]);
        place_sprite_data(&mut ram, 0, &solid_sprite());
        place_sprite_data(&mut ram, 1, &solid_sprite());
        let (mut eng, mut mem) = make_eng_mem(&mut *ram);
        // Sprite 0 X-expanded → 48 px wide (50..97). Sprite 1 at 90 overlaps.
        write_sprite_regs(&mut mem, 0, 50, 90, false, true, false);
        write_sprite_regs(&mut mem, 1, 90, 90, false, false, false);
        eng.sprite_enable_mask = 0x03;
        run_detect(&mut eng, &mut mem);
        assert_eq!(mem.vic_regs[0x1E] & 0x03, 0x03);
    }

    #[test]
    fn x_msb_high_x_collision() {
        let mut ram = Box::new([0u8; 65536]);
        place_sprite_data(&mut ram, 0, &solid_sprite());
        place_sprite_data(&mut ram, 1, &solid_sprite());
        let (mut eng, mut mem) = make_eng_mem(&mut *ram);
        // Both sprites at X=300 (>255, requires $D010 high bit).
        write_sprite_regs(&mut mem, 0, 300, 90, false, false, false);
        write_sprite_regs(&mut mem, 1, 305, 90, false, false, false);
        eng.sprite_enable_mask = 0x03;
        run_detect(&mut eng, &mut mem);
        assert_eq!(mem.vic_regs[0x1E] & 0x03, 0x03);
    }

    #[test]
    fn y_far_apart_no_collision() {
        let mut ram = Box::new([0u8; 65536]);
        place_sprite_data(&mut ram, 0, &solid_sprite());
        place_sprite_data(&mut ram, 1, &solid_sprite());
        let (mut eng, mut mem) = make_eng_mem(&mut *ram);
        // Sprite 0 on raster 100 (Y 90..110), sprite 1 Y=150 → not on this scanline.
        write_sprite_regs(&mut mem, 0, 50, 90, false, false, false);
        write_sprite_regs(&mut mem, 1, 50, 150, false, false, false);
        eng.sprite_enable_mask = 0x03;
        run_detect(&mut eng, &mut mem);
        assert_eq!(mem.vic_regs[0x1E] & 0x03, 0);
    }

    #[test]
    fn y_expand_extends_visibility() {
        let mut ram = Box::new([0u8; 65536]);
        place_sprite_data(&mut ram, 0, &solid_sprite());
        place_sprite_data(&mut ram, 1, &solid_sprite());
        let (mut eng, mut mem) = make_eng_mem(&mut *ram);
        // Sprite 0 Y-expanded covers Y 70..111 (42 px). Sprite 1 at Y 90.
        write_sprite_regs(&mut mem, 0, 50, 70, false, false, true);
        write_sprite_regs(&mut mem, 1, 50, 90, false, false, false);
        eng.sprite_enable_mask = 0x03;
        run_detect(&mut eng, &mut mem);
        assert_eq!(mem.vic_regs[0x1E] & 0x03, 0x03);
    }

    #[test]
    fn transparent_sprite_no_collision() {
        let mut ram = Box::new([0u8; 65536]);
        place_sprite_data(&mut ram, 0, &solid_sprite());
        place_sprite_data(&mut ram, 1, &empty_sprite());
        let (mut eng, mut mem) = make_eng_mem(&mut *ram);
        // bbox overlaps perfectly, but sprite 1 has no opaque pixels.
        write_sprite_regs(&mut mem, 0, 50, 90, false, false, false);
        write_sprite_regs(&mut mem, 1, 50, 90, false, false, false);
        eng.sprite_enable_mask = 0x03;
        run_detect(&mut eng, &mut mem);
        assert_eq!(mem.vic_regs[0x1E] & 0x03, 0);
    }

    #[test]
    fn sprite_bg_only_when_pixel_overlaps_fg() {
        // Sprite-bg collision requires actual FG pixels under the sprite.
        // Two cases:
        //   (a) sprite over an empty (bg-only) screen char → no collision
        //   (b) sprite over a non-empty char → collision
        let mut ram = Box::new([0u8; 65536]);
        place_sprite_data(&mut ram, 0, &solid_sprite());
        // Build screen at $0400 with all $20 (space, blank in PETSCII).
        // Chars come from char ROM at $1000 in VIC bank 0; we don't have
        // char ROM loaded, so charset reads return 0 → no FG.
        // (a) check no bg collision when chars are blank.
        let (mut eng, mut mem) = make_eng_mem(&mut *ram);
        write_sprite_regs(&mut mem, 0, 50, 100, false, false, false);
        eng.sprite_enable_mask = 0x01;
        // scanline 100 → display_y = 100-51 = 49 → row 6, line 1.
        run_detect(&mut eng, &mut mem);
        assert_eq!(mem.vic_regs[0x1F] & 0x01, 0, "blank chars → no bg collision");

        // (b) Place a non-empty char in the column the sprite covers and
        // load a synthetic charset into RAM at the corresponding VIC bank
        // location. We use bitmap mode for simplicity (no char ROM dance).
        let mut ram2 = Box::new([0u8; 65536]);
        place_sprite_data(&mut ram2, 0, &solid_sprite());
        // Switch to bitmap mode: $D011 bit 5 set. Bitmap base $D018 bit 3.
        // Use bitmap_base = $2000 (d018 bit 3 = 1), screen_base = $0400 (bits 4..7 = 1).
        let (mut eng2, mut mem2) = make_eng_mem(&mut *ram2);
        mem2.vic_regs[0x11] = 0x3B; // DEN+RSEL+BMM+ysmooth=3
        mem2.vic_regs[0x18] = 0x18; // screen $0400, bitmap $2000
        // Sprite at X=50, Y=100 → covers raster x 50..73 on scanline 100.
        // Display pixel x = scanline_x - 24 = 26..49 → cols 3..6 on row 6.
        // Bitmap byte for col 3 row 6 line 1 = $2000 + 6*320 + 3*8 + 1 = $2785.
        // Set bytes solid for cols 3..6 (24 px = 3 cells), line 1.
        for col in 3..7 {
            let addr = 0x2000 + 6 * 320 + col * 8 + 1;
            mem2.ram[addr] = 0xFF;
        }
        write_sprite_regs(&mut mem2, 0, 50, 100, false, false, false);
        eng2.sprite_enable_mask = 0x01;
        run_detect(&mut eng2, &mut mem2);
        assert_eq!(mem2.vic_regs[0x1F] & 0x01, 0x01, "sprite over solid bitmap → bg collision");
    }

    #[test]
    fn collision_accumulates_until_clear_v2() {
        let mut ram = Box::new([0u8; 65536]);
        place_sprite_data(&mut ram, 0, &solid_sprite());
        place_sprite_data(&mut ram, 1, &solid_sprite());
        let (mut eng, mut mem) = make_eng_mem(&mut *ram);
        write_sprite_regs(&mut mem, 0, 50, 90, false, false, false);
        write_sprite_regs(&mut mem, 1, 50, 90, false, false, false);
        eng.sprite_enable_mask = 0x03;
        run_detect(&mut eng, &mut mem);
        assert_eq!(mem.vic_regs[0x1E] & 0x03, 0x03);
        // Move sprite 1 far away on a later scanline — bit stays set.
        eng.raster_line = 105;
        mem.vic_regs[3] = 200; // sprite 1 Y far away
        run_detect(&mut eng, &mut mem);
        assert_eq!(mem.vic_regs[0x1E] & 0x03, 0x03);
    }

    #[test]
    fn multicolor_pair_opacity() {
        let mut ram = Box::new([0u8; 65536]);
        // Sprite 0: row 0 = $40,$00,$00 → MC pair bits "01" at top → FG color
        // pixel; in MC mode pair "01" is opaque (any non-00 pair is opaque).
        let mut data0 = empty_sprite();
        data0[0] = 0x40; // 01_00_00_00 in pairs → first pair = 01 (opaque)
        // Sprite 1: solid, X aligned with sprite 0's leftmost pair.
        let data1 = solid_sprite();
        place_sprite_data(&mut ram, 0, &data0);
        place_sprite_data(&mut ram, 1, &data1);
        let (mut eng, mut mem) = make_eng_mem(&mut *ram);
        // Y=100 so scanline=100 is row 0 (where the FG pair lives).
        write_sprite_regs(&mut mem, 0, 50, 100, true, false, false);
        write_sprite_regs(&mut mem, 1, 50, 100, false, false, false);
        eng.sprite_enable_mask = 0x03;
        run_detect(&mut eng, &mut mem);
        assert_eq!(mem.vic_regs[0x1E] & 0x03, 0x03);
    }
}
