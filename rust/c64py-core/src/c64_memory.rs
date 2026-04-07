//! C64 memory decode (6510 banking + I/O). Mirrors [memory.py](memory.py) read/write fast path.

use crate::resid_session::ResidSession;

pub const ROM_BASIC_START: u16 = 0xA000;
pub const ROM_BASIC_END: u16 = 0xC000;
pub const ROM_CHAR_START: u16 = 0xD000;
pub const ROM_CHAR_END: u16 = 0xE000;
pub const ROM_KERNAL_START: u16 = 0xE000;
pub const COLOR_MEM: u16 = 0xD800;
pub const VIC_BASE: u16 = 0xD000;
pub const SID_BASE: u16 = 0xD400;
pub const CIA1_BASE: u16 = 0xDC00;
pub const CIA2_BASE: u16 = 0xDD00;

#[derive(Clone)]
pub struct CiaTimer {
    pub latch: u16,
    /// Matches Python `CIATimer.counter` (signed int semantics on underflow).
    pub counter: i32,
    pub running: bool,
    pub irq_enabled: bool,
    pub one_shot: bool,
    pub input_mode: u8,
}

impl Default for CiaTimer {
    fn default() -> Self {
        Self {
            latch: 0xFFFF,
            counter: 0xFFFF,
            running: false,
            irq_enabled: false,
            one_shot: false,
            input_mode: 0,
        }
    }
}

impl CiaTimer {
    pub fn update(&mut self, cycles: u32) -> bool {
        if !self.running {
            return false;
        }
        if self.input_mode != 0 {
            return false;
        }
        let c = cycles as i32;
        let original_counter = self.counter;
        self.counter -= c;
        if original_counter > 0 && self.counter <= 0 {
            self.counter = self.latch as i32;
            if self.irq_enabled {
                return true;
            }
            if self.one_shot {
                self.running = false;
            }
        }
        false
    }
}

pub struct C64MemoryMap<'a> {
    pub ram: &'a mut [u8; 65536],
    pub basic_rom: Option<&'a [u8]>,
    pub kernal_rom: Option<&'a [u8]>,
    pub char_rom: Option<&'a [u8]>,
    pub video_standard: u8,
    pub raster_line: u16,
    pub raster_cycles: u32,
    pub vic_interrupt_state: u8,
    pub vic_regs: [u8; 64],
    pub pending_irq: bool,
    pub cia1_timer_a: CiaTimer,
    pub cia1_timer_b: CiaTimer,
    pub cia1_icr: u8,
    pub cia2_pra: u8,
    pub cia2_ddra: u8,
    /// When true, CIA2 port A reads merge peer IEC CLK/DATA like Python ``MemoryMap._read_cia2``.
    pub iec_merge_cia2: bool,
    /// Non-C64 devices release CLK (high) when true; snapshot at batch start from Python.
    pub iec_peer_clk_high: bool,
    pub iec_peer_data_high: bool,
    port01_cache_valid: bool,
    port01_cache_value: u8,
    /// Set during ``run_fast_batch`` when Rust drives reSID; must be null otherwise.
    pub resid: *mut ResidSession,
}

impl<'a> C64MemoryMap<'a> {
    pub fn new(ram: &'a mut [u8; 65536]) -> Self {
        Self {
            ram,
            basic_rom: None,
            kernal_rom: None,
            char_rom: None,
            video_standard: 0,
            raster_line: 0,
            raster_cycles: 0,
            vic_interrupt_state: 0,
            vic_regs: [0; 64],
            pending_irq: false,
            cia1_timer_a: CiaTimer::default(),
            cia1_timer_b: CiaTimer::default(),
            cia1_icr: 0,
            cia2_pra: 0xFF,
            cia2_ddra: 0xFF,
            iec_merge_cia2: false,
            iec_peer_clk_high: true,
            iec_peer_data_high: true,
            port01_cache_valid: false,
            port01_cache_value: 0,
            resid: std::ptr::null_mut(),
        }
    }

    fn cpu_port01_effective(&mut self) -> u8 {
        let ddr = self.ram[0];
        let latch = self.ram[1];
        let pullups = 0x17u8;
        (latch & ddr) | (pullups & !ddr)
    }

    fn port01_for_read(&mut self) -> u8 {
        if !self.port01_cache_valid {
            self.port01_cache_value = self.cpu_port01_effective();
            self.port01_cache_valid = true;
        }
        self.port01_cache_value
    }

    pub fn invalidate_6510_port_read_cache(&mut self) {
        self.port01_cache_valid = false;
    }

    fn vic_irq_enabled_pending(&self) -> bool {
        let irq_mask = self.vic_regs[0x1A] & 0x0F;
        (self.vic_interrupt_state & irq_mask) != 0
    }

    pub fn recompute_pending_irq(&mut self) {
        if self.vic_interrupt_state == 0 {
            self.pending_irq = (self.cia1_icr & 0x80) != 0;
        } else {
            self.pending_irq =
                (self.cia1_icr & 0x80) != 0 || self.vic_irq_enabled_pending();
        }
    }

    pub fn trigger_vic_irq(&mut self, source_mask: u8) {
        self.vic_interrupt_state |= source_mask & 0x0F;
        self.recompute_pending_irq();
    }

    fn read_vic(&mut self, reg: usize) -> u8 {
        let reg = reg & 0x3F;
        match reg {
            0x11 => {
                let raster_msb = ((self.raster_line >> 8) & 1) as u8;
                (self.vic_regs[0x11] & 0x7F) | (raster_msb << 7)
            }
            0x12 => (self.raster_line & 0xFF) as u8,
            0x19 => {
                let mut v = self.vic_interrupt_state & 0x0F;
                if self.vic_irq_enabled_pending() {
                    v |= 0x80;
                }
                v
            }
            0x1A => self.vic_regs[0x1A] & 0x0F,
            0x20 => self.vic_regs[0x20] & 0x0F,
            0x21 => self.vic_regs[0x21] & 0x0F,
            _ => *self.vic_regs.get(reg).unwrap_or(&0),
        }
    }

    fn write_vic(&mut self, reg: usize, value: u8) {
        let reg = reg & 0x3F;
        self.vic_regs[reg] = value;
        if reg == 0x19 {
            self.vic_interrupt_state &= !(value & 0x0F);
        } else if reg == 0x1A {
            self.vic_regs[0x1A] = value & 0x0F;
        } else if reg == 0x12 {
            self.vic_regs[0x12] = value & 0xFF;
        }
        self.recompute_pending_irq();
    }

    fn read_cia1(&mut self, reg: u8) -> u8 {
        match reg {
            0x00 | 0x01 => 0xFF,
            0x04 => (self.cia1_timer_a.counter as u16) as u8,
            0x05 => ((self.cia1_timer_a.counter as u16) >> 8) as u8,
            0x06 => (self.cia1_timer_b.counter as u16) as u8,
            0x07 => ((self.cia1_timer_b.counter as u16) >> 8) as u8,
            0x0D => {
                let r = self.cia1_icr;
                self.cia1_icr = 0;
                self.recompute_pending_irq();
                r
            }
            0x0E => {
                let mut result = 0u8;
                if self.cia1_timer_a.running {
                    result |= 0x01;
                }
                if self.cia1_timer_a.one_shot {
                    result |= 0x08;
                }
                if self.cia1_timer_a.input_mode != 0 {
                    result |= self.cia1_timer_a.input_mode << 5;
                }
                result
            }
            0x0F => {
                let mut result = 0u8;
                if self.cia1_timer_b.running {
                    result |= 0x01;
                }
                if self.cia1_timer_b.one_shot {
                    result |= 0x08;
                }
                if self.cia1_timer_b.input_mode != 0 {
                    result |= self.cia1_timer_b.input_mode << 5;
                }
                result
            }
            _ => 0,
        }
    }

    fn write_cia1(&mut self, reg: u8, value: u8) {
        match reg {
            0x04 => {
                self.cia1_timer_a.latch =
                    (self.cia1_timer_a.latch & 0xFF00) | u16::from(value);
                if !self.cia1_timer_a.running {
                    let hi = (self.cia1_timer_a.counter as u16) & 0xFF00;
                    self.cia1_timer_a.counter = i32::from(hi | u16::from(value));
                }
            }
            0x05 => {
                self.cia1_timer_a.latch =
                    (self.cia1_timer_a.latch & 0x00FF) | (u16::from(value) << 8);
                if !self.cia1_timer_a.running {
                    let lo = (self.cia1_timer_a.counter as u16) & 0x00FF;
                    self.cia1_timer_a.counter =
                        i32::from(lo | (u16::from(value) << 8));
                }
            }
            0x06 => {
                self.cia1_timer_b.latch =
                    (self.cia1_timer_b.latch & 0xFF00) | u16::from(value);
                if !self.cia1_timer_b.running {
                    let hi = (self.cia1_timer_b.counter as u16) & 0xFF00;
                    self.cia1_timer_b.counter = i32::from(hi | u16::from(value));
                }
            }
            0x07 => {
                self.cia1_timer_b.latch =
                    (self.cia1_timer_b.latch & 0x00FF) | (u16::from(value) << 8);
                if !self.cia1_timer_b.running {
                    let lo = (self.cia1_timer_b.counter as u16) & 0x00FF;
                    self.cia1_timer_b.counter =
                        i32::from(lo | (u16::from(value) << 8));
                }
            }
            0x0D => {
                if value & 0x80 != 0 {
                    if value & 0x01 != 0 {
                        self.cia1_timer_a.irq_enabled = true;
                    }
                    if value & 0x02 != 0 {
                        self.cia1_timer_b.irq_enabled = true;
                    }
                } else {
                    if value & 0x01 != 0 {
                        self.cia1_timer_a.irq_enabled = false;
                    }
                    if value & 0x02 != 0 {
                        self.cia1_timer_b.irq_enabled = false;
                    }
                }
            }
            0x0E => {
                if value & 0x01 != 0 {
                    if !self.cia1_timer_a.running {
                        self.cia1_timer_a.counter = i32::from(self.cia1_timer_a.latch);
                    }
                    self.cia1_timer_a.running = true;
                } else {
                    self.cia1_timer_a.running = false;
                }
                self.cia1_timer_a.one_shot = (value & 0x08) != 0;
                self.cia1_timer_a.input_mode = (value >> 5) & 0x03;
            }
            0x0F => {
                if value & 0x01 != 0 {
                    if !self.cia1_timer_b.running {
                        self.cia1_timer_b.counter = i32::from(self.cia1_timer_b.latch);
                    }
                    self.cia1_timer_b.running = true;
                } else {
                    self.cia1_timer_b.running = false;
                }
                self.cia1_timer_b.one_shot = (value & 0x08) != 0;
                self.cia1_timer_b.input_mode = (value >> 5) & 0x03;
            }
            _ => {}
        }
    }

    fn read_cia2(&self, reg: u8) -> u8 {
        match reg {
            0x00 => {
                if !self.iec_merge_cia2 {
                    return self.cia2_pra;
                }
                let c64_clk_rel = (self.cia2_pra & 0x10) != 0;
                let c64_data_rel = (self.cia2_pra & 0x20) != 0;
                let clk_hi = self.iec_peer_clk_high && c64_clk_rel;
                let data_hi = self.iec_peer_data_high && c64_data_rel;
                (self.cia2_pra & 0x3F) | (u8::from(clk_hi) << 6) | (u8::from(data_hi) << 7)
            }
            0x02 => self.cia2_ddra,
            _ => 0,
        }
    }

    fn write_cia2(&mut self, reg: u8, value: u8) {
        match reg {
            0x00 => self.cia2_pra = value,
            0x02 => self.cia2_ddra = value,
            _ => {}
        }
    }

    fn read_io(&mut self, addr: u16) -> u8 {
        let addr = addr as usize;
        if (COLOR_MEM as usize) <= addr && addr < (COLOR_MEM as usize) + 1000 {
            return self.ram[addr];
        }
        if (VIC_BASE as usize) <= addr && addr < (VIC_BASE as usize) + 0x40 {
            return self.read_vic(addr - VIC_BASE as usize);
        }
        if (SID_BASE as usize) <= addr && addr < (SID_BASE as usize) + 0x20 {
            if !self.resid.is_null() {
                let off = (addr - SID_BASE as usize) as u8;
                return unsafe { (*self.resid).read_reg(off) };
            }
            return 0;
        }
        if (CIA1_BASE as usize) <= addr && addr < (CIA1_BASE as usize) + 0x10 {
            return self.read_cia1((addr - CIA1_BASE as usize) as u8);
        }
        if (CIA2_BASE as usize) <= addr && addr < (CIA2_BASE as usize) + 0x10 {
            return self.read_cia2((addr - CIA2_BASE as usize) as u8);
        }
        self.ram[addr]
    }

    fn write_io(&mut self, addr: u16, value: u8) {
        let addr_us = addr as usize;
        if (COLOR_MEM as usize) <= addr_us && addr_us < (COLOR_MEM as usize) + 1000 {
            self.ram[addr_us] = value;
            return;
        }
        if (VIC_BASE as usize) <= addr_us && addr_us < (VIC_BASE as usize) + 0x40 {
            self.write_vic(addr_us - VIC_BASE as usize, value);
            return;
        }
        if (SID_BASE as usize) <= addr_us && addr_us < (SID_BASE as usize) + 0x20 {
            if !self.resid.is_null() {
                let off = (addr_us - SID_BASE as usize) as u8;
                unsafe {
                    (*self.resid).write_reg(off, value);
                }
            }
            return;
        }
        if (CIA1_BASE as usize) <= addr_us && addr_us < (CIA1_BASE as usize) + 0x10 {
            self.write_cia1((addr_us - CIA1_BASE as usize) as u8, value);
            return;
        }
        if (CIA2_BASE as usize) <= addr_us && addr_us < (CIA2_BASE as usize) + 0x10 {
            self.write_cia2((addr_us - CIA2_BASE as usize) as u8, value);
            return;
        }
        self.ram[addr_us] = value;
    }

    pub fn read(&mut self, addr: u16) -> u8 {
        let addr = addr as usize;
        if addr == 0 {
            return self.ram[0];
        }
        if addr == 1 {
            return self.port01_for_read();
        }
        if (COLOR_MEM as usize) <= addr && addr < (COLOR_MEM as usize) + 1000 {
            return self.ram[addr];
        }
        if (2..ROM_BASIC_START as usize).contains(&addr) {
            return self.ram[addr];
        }
        if (ROM_BASIC_END as usize..ROM_CHAR_START as usize).contains(&addr) {
            return self.ram[addr];
        }

        let port_01 = self.port01_for_read();
        let loram = (port_01 & 0x01) != 0;
        let hiram = (port_01 & 0x02) != 0;
        let charen = (port_01 & 0x04) != 0;

        if (ROM_CHAR_START as usize) <= addr && addr < (ROM_CHAR_END as usize) {
            if charen {
                return self.read_io(addr as u16);
            }
            if let Some(cr) = self.char_rom {
                if hiram {
                    return cr[addr - ROM_CHAR_START as usize];
                }
            }
            return self.ram[addr];
        }

        if (ROM_BASIC_START as usize) <= addr && addr < (ROM_BASIC_END as usize) {
            if loram && hiram {
                if let Some(br) = self.basic_rom {
                    return br[addr - ROM_BASIC_START as usize];
                }
            }
            return self.ram[addr];
        }

        if (ROM_KERNAL_START as usize) <= addr && addr <= 0xFFFF {
            if hiram {
                if let Some(kr) = self.kernal_rom {
                    return kr[addr - ROM_KERNAL_START as usize];
                }
            }
            return self.ram[addr];
        }

        self.ram[addr]
    }

    pub fn write(&mut self, addr: u16, value: u8) {
        let addr = addr as usize;
        let value = value;
        if (COLOR_MEM as usize) <= addr && addr < (COLOR_MEM as usize) + 1000 {
            self.ram[addr] = value;
            return;
        }
        if addr <= 1 {
            self.ram[addr] = value;
            self.invalidate_6510_port_read_cache();
            return;
        }

        let charen = (self.port01_for_read() & 0x04) != 0;

        if (ROM_BASIC_START as usize) <= addr && addr < (ROM_BASIC_END as usize) {
            self.ram[addr] = value;
        } else if (ROM_KERNAL_START as usize) <= addr && addr <= 0xFFFF {
            self.ram[addr] = value;
        } else if (ROM_CHAR_START as usize) <= addr && addr < (ROM_CHAR_END as usize) {
            if charen {
                self.write_io(addr as u16, value);
            } else {
                self.ram[addr] = value;
            }
        } else {
            self.ram[addr] = value;
        }
    }
}
