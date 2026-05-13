//! Host-side compositor: one pass over ``per_cycle_vic_flat`` + RAM → RGB888.
//! Mirrors ``graphics.PygameInterface._render_frame_per_cycle`` (border, text/bitmap, sprites).

const COLOR_MEM: usize = 0xD800;
const VIC_D011_BMM: u8 = 0x20;
const VIC_D011_ECM: u8 = 0x40;
const VIC_D016_MCM: u8 = 0x10;

const PC_FIRST_RASTER: u16 = 51;
const PC_HEIGHT: u16 = 200;
const PC_FIRST_CYCLE: u32 = 14;
const PC_CYCLES: u32 = 40;
pub const PC_SAMPLES: usize = (PC_HEIGHT as usize) * (PC_CYCLES as usize);

#[inline]
pub fn per_cycle_flat_index(_video_standard: u8, rl: u16, rc: u32) -> Option<usize> {
    if rl < PC_FIRST_RASTER || rl >= PC_FIRST_RASTER + PC_HEIGHT {
        return None;
    }
    if rc < PC_FIRST_CYCLE || rc >= PC_FIRST_CYCLE + PC_CYCLES {
        return None;
    }
    let y = (rl - PC_FIRST_RASTER) as usize;
    let x = (rc - PC_FIRST_CYCLE) as usize;
    Some(y * (PC_CYCLES as usize) + x)
}

#[inline]
fn pal_rgb(pal48: &[u8], idx: u8) -> [u8; 3] {
    let i = (idx & 0x0F) as usize * 3;
    [pal48[i], pal48[i + 1], pal48[i + 2]]
}

#[derive(Clone, Copy)]
struct ModeBits {
    bitmap: bool,
    ecm: bool,
    mcm: bool,
    screen_base: u16,
    bitmap_base: u16,
    char_base: u16,
}

fn mode_from_regb(regb: &[u8]) -> ModeBits {
    let d011 = *regb.get(0x11).unwrap_or(&0);
    let d016 = *regb.get(0x16).unwrap_or(&0);
    let d018 = *regb.get(0x18).unwrap_or(&0);
    let bitmap = (d011 & VIC_D011_BMM) != 0;
    let ecm = (d011 & VIC_D011_ECM) != 0;
    let mcm = (d016 & VIC_D016_MCM) != 0;
    let vm = (d018 >> 4) & 0x0F;
    let cb = (d018 >> 1) & 0x07;
    let screen_base = (vm as u16) * 0x0400;
    let (bitmap_base, char_base) = if bitmap {
        let bb = if (d018 & 0x08) != 0 { 0x2000u16 } else { 0x0000u16 };
        (bb, 0u16)
    } else {
        (0u16, (cb as u16) * 0x0800)
    };
    ModeBits {
        bitmap,
        ecm,
        mcm,
        screen_base,
        bitmap_base,
        char_base,
    }
}

#[inline]
fn vic_fetches_charset_rom(vic_bank: u32, rel_within_bank: u32) -> bool {
    let rel = rel_within_bank & 0x3FFF;
    if rel < 0x1000 || rel >= 0x2000 {
        return false;
    }
    vic_bank == 0x0000 || vic_bank == 0x8000
}

fn read_glyph_rows(
    ram: &[u8],
    char_rom: Option<&[u8]>,
    vic_bank: u32,
    char_base: u16,
    code: u8,
) -> [u8; 8] {
    let mut rows = [0u8; 8];
    for r in 0..8usize {
        let rel = (char_base as u32).wrapping_add((code as u32) * 8 + r as u32) & 0x3FFF;
        rows[r] = if let Some(cr) = char_rom {
            if vic_fetches_charset_rom(vic_bank, rel) {
                cr.get(rel as usize - 0x1000).copied().unwrap_or(0)
            } else {
                ram[((vic_bank + rel) & 0xFFFF) as usize]
            }
        } else {
            ram[((vic_bank + rel) & 0xFFFF) as usize]
        };
    }
    rows
}

fn plot_hires_scanline(buf: &mut [u8], w: usize, x: usize, y: usize, row_b: u8, fg: [u8; 3]) {
    if y >= buf.len() / (w * 3) {
        return;
    }
    let base = y * w * 3;
    for xx in 0..8usize {
        if row_b & (1 << (7 - xx)) == 0 {
            continue;
        }
        let px = x + xx;
        if px >= w {
            continue;
        }
        let i = base + px * 3;
        buf[i] = fg[0];
        buf[i + 1] = fg[1];
        buf[i + 2] = fg[2];
    }
}

fn fill_rect_rgb(buf: &mut [u8], w: usize, x0: usize, y0: usize, rw: usize, rh: usize, c: [u8; 3]) {
    let h = buf.len() / (w * 3);
    let x1 = (x0 + rw).min(w);
    let y1 = (y0 + rh).min(h);
    if x0 >= x1 || y0 >= y1 {
        return;
    }
    for yy in y0..y1 {
        let row = yy * w * 3;
        for xx in x0..x1 {
            let i = row + xx * 3;
            buf[i] = c[0];
            buf[i + 1] = c[1];
            buf[i + 2] = c[2];
        }
    }
}

fn plot_mcm_text_scanline(buf: &mut [u8], w: usize, x: usize, y: usize, row_b: u8, pal48: &[u8], cc: u8, bg: u8, c1: u8, c2: u8) {
    let p0 = pal_rgb(pal48, bg);
    let p1 = pal_rgb(pal48, c1);
    let p2 = pal_rgb(pal48, c2);
    let p3 = pal_rgb(pal48, cc);
    for pair in 0..4usize {
        let bits = (row_b >> (6 - pair * 2)) & 0x03;
        let c = if bits == 0 {
            p0
        } else if bits == 1 {
            p1
        } else if bits == 2 {
            p2
        } else {
            p3
        };
        let px = x + pair * 2;
        fill_rect_rgb(buf, w, px, y, 2, 1, c);
    }
}

fn plot_bitmap_scanline(
    buf: &mut [u8],
    w: usize,
    base_x: usize,
    yy: usize,
    byte: u8,
    multicolor: bool,
    bg0: u8,
    color_data: u8,
    color_mem: u8,
    pal48: &[u8],
) {
    if multicolor {
        let c1 = (color_data >> 4) & 0x0F;
        let c2 = color_data & 0x0F;
        let c3 = color_mem & 0x0F;
        for bit_pair in 0..4usize {
            let pixel_bits = (byte >> (6 - bit_pair * 2)) & 0x03;
            let c = if pixel_bits == 0 {
                pal_rgb(pal48, bg0)
            } else if pixel_bits == 1 {
                pal_rgb(pal48, c1)
            } else if pixel_bits == 2 {
                pal_rgb(pal48, c2)
            } else {
                pal_rgb(pal48, c3)
            };
            fill_rect_rgb(buf, w, base_x + bit_pair * 2, yy, 2, 1, c);
        }
    } else {
        let c1 = (color_data >> 4) & 0x0F;
        let c0 = color_data & 0x0F;
        let p0 = pal_rgb(pal48, c0);
        let p1 = pal_rgb(pal48, c1);
        if yy >= buf.len() / (w * 3) {
            return;
        }
        let base_row = yy * w * 3;
        for bit in 0..8usize {
            let px = base_x + bit;
            if px >= w {
                continue;
            }
            let c = if (byte >> (7 - bit)) & 1 != 0 { p1 } else { p0 };
            let i = base_row + px * 3;
            buf[i] = c[0];
            buf[i + 1] = c[1];
            buf[i + 2] = c[2];
        }
    }
}

fn overlay_sprites_column(
    buf: &mut [u8],
    w: usize,
    ram: &[u8],
    regb: &[u8; 64],
    pra: u8,
    y_win: usize,
    col: usize,
    py: usize,
    screen_left: usize,
    screen_right: usize,
    screen_top: usize,
    screen_bottom: usize,
    pal48: &[u8],
) {
    let vic_bank = (3 - (pra & 0x03)) as u32 * 0x4000;
    let m = mode_from_regb(regb);
    let matrix = (vic_bank + m.screen_base as u32) as usize & 0xFFFF;

    let mc0 = pal_rgb(pal48, regb[0x25]);
    let mc1 = pal_rgb(pal48, regb[0x26]);

    let x0 = col * 8;
    for sn in 0..8usize {
        if regb[0x15] & (1 << sn) == 0 {
            continue;
        }
        let y_vic = regb[sn * 2 + 1];
        let row = y_win as i32 - y_vic as i32 + 50;
        if row < 0 || row >= 21 {
            continue;
        }
        let row = row as usize;
        let mut xv = regb[sn * 2] as u16;
        if (regb[0x10] & (1 << sn)) != 0 {
            xv |= 256;
        }
        let x_vic = xv;
        let sprite_x = x_vic as i32 - 24;
        let multicolor = (regb[0x1C] & (1 << sn)) != 0;
        let sp = pal_rgb(pal48, regb[0x27 + sn]);

        let ptr = ram[(matrix + 0x3F8 + sn) & 0xFFFF] as usize;
        let sprite_addr = (vic_bank as usize + ((ptr & 0xFF) << 6)) & 0xFFFF;
        let bo = (sprite_addr + row * 3) & 0xFFFF;
        let row_data = (u32::from(ram[bo]) << 16) | (u32::from(ram[(bo + 1) & 0xFFFF]) << 8) | u32::from(ram[(bo + 2) & 0xFFFF]);

        if multicolor {
            for dx in 0..8usize {
                let cx = x0 + dx;
                let rel_x = cx as i32 - sprite_x;
                if !(0..24).contains(&rel_x) {
                    continue;
                }
                let bp = rel_x / 2;
                let bits = (row_data >> (22 - bp * 2)) & 0x03;
                if bits == 0 {
                    continue;
                }
                let (r, g, b) = if bits == 1 {
                    (mc0[0], mc0[1], mc0[2])
                } else if bits == 2 {
                    (sp[0], sp[1], sp[2])
                } else {
                    (mc1[0], mc1[1], mc1[2])
                };
                let px = screen_left + cx;
                if px >= screen_left && px < screen_right && py >= screen_top && py < screen_bottom {
                    let i = py * w * 3 + px * 3;
                    buf[i] = r;
                    buf[i + 1] = g;
                    buf[i + 2] = b;
                }
            }
        } else {
            for dx in 0..8usize {
                let cx = x0 + dx;
                let rel_x = cx as i32 - sprite_x;
                if !(0..24).contains(&rel_x) {
                    continue;
                }
                if (row_data >> (23 - rel_x)) & 1 == 0 {
                    continue;
                }
                let px = screen_left + cx;
                if px >= screen_left && px < screen_right && py >= screen_top && py < screen_bottom {
                    let i = py * w * 3 + px * 3;
                    buf[i] = sp[0];
                    buf[i + 1] = sp[1];
                    buf[i + 2] = sp[2];
                }
            }
        }
    }
}

/// Composite one frame into *rgb_out* (row-major RGB888). ``vic_flat`` length must be ``PC_SAMPLES * 64``.
pub fn composite_per_cycle_frame(
    ram: &[u8; 65536],
    vic_flat: &[u8],
    cia_flat: &[u8],
    char_rom: Option<&[u8]>,
    video_standard: u8,
    pal48: &[u8; 48],
    rgb_out: &mut [u8],
    native_w: usize,
    native_h: usize,
    screen_left: usize,
    screen_top: usize,
    screen_w: usize,
    screen_h: usize,
    border_px: usize,
    skip_sprites: bool,
    live_regb: &[u8; 64],
    live_cia2_pra: u8,
) -> Result<(), &'static str> {
    if vic_flat.len() != PC_SAMPLES * 64 || cia_flat.len() != PC_SAMPLES {
        return Err("per_cycle flat wrong size");
    }
    let exp = native_w.saturating_mul(native_h).saturating_mul(3);
    if rgb_out.len() < exp {
        return Err("rgb_out too small");
    }
    let screen_right = screen_left + screen_w;
    let screen_bottom = screen_top + screen_h;

    let nlines = if video_standard == 1 { 263u16 } else { 312u16 };
    let content_first = PC_FIRST_RASTER;
    let content_h = PC_HEIGHT as usize;
    let top_lines = (content_first as usize).min(nlines as usize);
    let bottom_lines = nlines as usize - (content_first as usize + content_h);

    let flat_mv = vic_flat;
    let cia_mv = cia_flat;

    for y in 0..native_h {
        let rl = if border_px > 0 && y < border_px {
            if top_lines > 0 {
                (y * top_lines / border_px) as u16
            } else {
                0
            }
        } else if y < border_px + content_h {
            content_first + (y - border_px) as u16
        } else {
            let yy = y - (border_px + content_h);
            if border_px > 0 && bottom_lines > 0 {
                (content_first + content_h as u16) + (yy * bottom_lines / border_px) as u16
            } else {
                content_first + content_h as u16
            }
        };
        let rl = (rl as usize % nlines as usize) as u16;

        let y_win = rl as i32 - PC_FIRST_RASTER as i32;
        let regb: [u8; 64] = if y_win >= 0 && (y_win as usize) < content_h {
            let o = (y_win as usize) * (PC_CYCLES as usize) * 64;
            if o + 64 <= flat_mv.len() {
                flat_mv[o..o + 64].try_into().unwrap()
            } else {
                *live_regb
            }
        } else {
            *live_regb
        };

        let mut border_code = regb[0x20] & 0x0F;
        if border_code == 0 && regb.iter().all(|&b| b == 0) {
            border_code = 0x0E;
        }
        let c = pal_rgb(pal48, border_code);
        if y < screen_top || y >= screen_top + screen_h {
            fill_rect_rgb(rgb_out, native_w, 0, y, native_w, 1, c);
        } else {
            if screen_left > 0 {
                fill_rect_rgb(rgb_out, native_w, 0, y, screen_left, 1, c);
            }
            if screen_right < native_w {
                fill_rect_rgb(rgb_out, native_w, screen_right, y, native_w - screen_right, 1, c);
            }
        }
    }

    let content_px_w = (PC_CYCLES as usize) * 8;
    for y_win in 0..content_h {
        let o_bg = y_win * (PC_CYCLES as usize) * 64;
        let regb_bg: [u8; 64] = if o_bg + 64 <= flat_mv.len() {
            flat_mv[o_bg..o_bg + 64].try_into().unwrap()
        } else {
            *live_regb
        };
        let use_live = regb_bg.iter().all(|&b| b == 0);
        let regb_bg_ref = if use_live { live_regb } else { &regb_bg };
        let bg_scan = pal_rgb(pal48, regb_bg_ref[0x21]);
        let py = screen_top + y_win;
        fill_rect_rgb(
            rgb_out,
            native_w,
            screen_left,
            py,
            content_px_w,
            1,
            bg_scan,
        );

        let text_row = y_win / 8;
        let scan = y_win % 8;
        for col in 0..(PC_CYCLES as usize) {
            let idx = y_win * (PC_CYCLES as usize) + col;
            let o = idx * 64;
            let mut regb: [u8; 64] = if o + 64 <= flat_mv.len() {
                flat_mv[o..o + 64].try_into().unwrap()
            } else {
                *live_regb
            };
            let mut pra = cia_mv.get(idx).copied().unwrap_or(live_cia2_pra);
            if regb.iter().all(|&b| b == 0) {
                regb = *live_regb;
                pra = live_cia2_pra;
            }
            let m = mode_from_regb(&regb);
            let vic_bank = (3 - (pra & 0x03)) as u32 * 0x4000;
            let bg0 = regb[0x21] & 0x0F;
            let bg_colors = [
                regb[0x21] & 0x0F,
                regb[0x22] & 0x0F,
                regb[0x23] & 0x0F,
                regb[0x24] & 0x0F,
            ];
            let screen_base = (vic_bank + m.screen_base as u32) as usize & 0xFFFF;
            let bitmap_base = (vic_bank + m.bitmap_base as u32) as usize & 0xFFFF;
            let x = screen_left + col * 8;
            let char_index = text_row * 40 + col;

            if m.bitmap {
                let color_data = ram[(screen_base + char_index) & 0xFFFF];
                let color_mem = ram[(COLOR_MEM + char_index) & 0xFFFF] & 0x0F;
                let bitmap_offset = char_index * 8;
                let byte = ram[(bitmap_base + bitmap_offset + scan) & 0xFFFF];
                plot_bitmap_scanline(
                    rgb_out,
                    native_w,
                    x,
                    py,
                    byte,
                    m.mcm,
                    bg0,
                    color_data,
                    color_mem,
                    pal48,
                );
                if !skip_sprites {
                    overlay_sprites_column(
                        rgb_out,
                        native_w,
                        ram,
                        &regb,
                        pra,
                        y_win,
                        col,
                        py,
                        screen_left,
                        screen_right,
                        screen_top,
                        screen_bottom,
                        pal48,
                    );
                }
                continue;
            }

            let char_base = m.char_base;
            let raw_code = ram[(screen_base + char_index) & 0xFFFF];
            let color_code = ram[(COLOR_MEM + char_index) & 0xFFFF] & 0x0F;

            if m.ecm {
                let bg_index = (raw_code >> 6) & 0x03;
                let code_ecm = raw_code & 0x3F;
                let char_bg = pal_rgb(pal48, bg_colors[bg_index as usize]);
                fill_rect_rgb(rgb_out, native_w, x, py, 8, 1, char_bg);
                let rows = read_glyph_rows(ram, char_rom, vic_bank, char_base, code_ecm);
                plot_hires_scanline(
                    rgb_out,
                    native_w,
                    x,
                    py,
                    rows[scan],
                    pal_rgb(pal48, color_code),
                );
            } else {
                let rows = read_glyph_rows(ram, char_rom, vic_bank, char_base, raw_code);
                let multicolor_text = m.mcm && !m.ecm;
                if multicolor_text && (color_code & 0x08) != 0 {
                    plot_mcm_text_scanline(
                        rgb_out,
                        native_w,
                        x,
                        py,
                        rows[scan],
                        pal48,
                        color_code & 0x07,
                        bg_colors[0],
                        bg_colors[1],
                        bg_colors[2],
                    );
                } else {
                    let fg = if multicolor_text {
                        color_code & 0x07
                    } else {
                        color_code
                    };
                    plot_hires_scanline(
                        rgb_out,
                        native_w,
                        x,
                        py,
                        rows[scan],
                        pal_rgb(pal48, fg),
                    );
                }
            }
            if !skip_sprites {
                overlay_sprites_column(
                    rgb_out,
                    native_w,
                    ram,
                    &regb,
                    pra,
                    y_win,
                    col,
                    py,
                    screen_left,
                    screen_right,
                    screen_top,
                    screen_bottom,
                    pal48,
                );
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn idx_corners() {
        assert_eq!(per_cycle_flat_index(0, 51, 14), Some(0));
        assert_eq!(per_cycle_flat_index(0, 51, 53), Some(39));
        assert_eq!(per_cycle_flat_index(0, 250, 53), Some(7999));
        assert!(per_cycle_flat_index(0, 50, 14).is_none());
        assert!(per_cycle_flat_index(0, 51, 13).is_none());
    }
}
