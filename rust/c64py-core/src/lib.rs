//! PyO3 extension `c64py_rust_core` — optional fast-path CPU batch runner.

mod c64_cpu;
mod c64_fast;
mod c64_memory;
mod c64_timing;
mod c64_vicii;
mod resid_session;

use c64_cpu::CpuState;
use c64_memory::{C64MemoryMap, CiaTimer};
use c64_fast::run_fast_batch;
use c64_vicii::ViciiEngine;
use resid_session::ResidSession;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyByteArray, PyBytes, PyTuple};
use pyo3::IntoPyObjectExt;

#[pyfunction]
fn ping() -> &'static str {
    "pong"
}

#[pyfunction]
fn rust_core_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (
    ram,
    max_instructions,
    pc, a, x, y, sp, p, cycles, stopped,
    video_standard,
    raster_line, raster_cycles,
    vic_regs,
    vic_interrupt_state,
    pending_irq,
    cia1_icr,
    cia2_pra, cia2_ddra,
    ta_latch, ta_counter, ta_running, ta_irq_en, ta_oneshot, ta_input,
    tb_latch, tb_counter, tb_running, tb_irq_en,     tb_oneshot, tb_input,
    basic_rom=None, kernal_rom=None, char_rom=None, stop_pcs=None,
    hybrid_vic_pal=false,
    v_raster_line=0, v_raster_cycle=0, v_allow_bad_lines=false, v_bad_line=false,
    v_ysmooth=0, v_den=false, v_raster_irq_line=0, v_raster_irq_triggered=false,
    v_prefetch_cycles=0, v_first_dma_line=48, v_last_dma_line=247,
    v_sprite_enable_mask=0,     v_cycles_per_line=63, v_num_raster_lines=312,
    resid_lib_path=None, resid_ptr=None,
    iec_enabled=false, iec_peer_clk_high=true, iec_peer_data_high=true,
    beam_render_enabled=false, beam_nlines=0u16,
    beam_vic_flat=None, beam_cia2_flat=None,
))]
fn run_fast_batch_py<'py>(
    py: Python<'py>,
    ram: Bound<'py, PyByteArray>,
    max_instructions: u64,
    pc: u16,
    a: u8,
    x: u8,
    y: u8,
    sp: u8,
    p: u8,
    cycles: u64,
    stopped: bool,
    video_standard: String,
    raster_line: u16,
    raster_cycles: u32,
    vic_regs: [u8; 64],
    vic_interrupt_state: u8,
    pending_irq: bool,
    cia1_icr: u8,
    cia2_pra: u8,
    cia2_ddra: u8,
    ta_latch: u16,
    ta_counter: i32,
    ta_running: bool,
    ta_irq_en: bool,
    ta_oneshot: bool,
    ta_input: u8,
    tb_latch: u16,
    tb_counter: i32,
    tb_running: bool,
    tb_irq_en: bool,
    tb_oneshot: bool,
    tb_input: u8,
    basic_rom: Option<Vec<u8>>,
    kernal_rom: Option<Vec<u8>>,
    char_rom: Option<Vec<u8>>,
    stop_pcs: Option<Vec<u16>>,
    hybrid_vic_pal: bool,
    v_raster_line: u16,
    v_raster_cycle: u32,
    v_allow_bad_lines: bool,
    v_bad_line: bool,
    v_ysmooth: u8,
    v_den: bool,
    v_raster_irq_line: u16,
    v_raster_irq_triggered: bool,
    v_prefetch_cycles: u32,
    v_first_dma_line: u16,
    v_last_dma_line: u16,
    v_sprite_enable_mask: u32,
    v_cycles_per_line: u32,
    v_num_raster_lines: u16,
    resid_lib_path: Option<String>,
    resid_ptr: Option<u64>,
    iec_enabled: bool,
    iec_peer_clk_high: bool,
    iec_peer_data_high: bool,
    beam_render_enabled: bool,
    beam_nlines: u16,
    beam_vic_flat: Option<Bound<'py, PyByteArray>>,
    beam_cia2_flat: Option<Bound<'py, PyByteArray>>,
) -> PyResult<Bound<'py, PyTuple>> {
    let vs = if video_standard.eq_ignore_ascii_case("ntsc") {
        1u8
    } else {
        0u8
    };
    let mut backing = ram.to_vec();
    if backing.len() != 65536 {
        return Err(PyValueError::new_err(format!(
            "ram must be exactly 65536 bytes, got {}",
            backing.len()
        )));
    }
    type OutTuple = (
        u64,
        u64,
        u16,
        u8,
        u8,
        u8,
        u8,
        u8,
        u64,
        bool,
        u16,
        u32,
        [u8; 64],
        u8,
        bool,
        u8,
        u8,
        u8,
        u16,
        i32,
        bool,
        bool,
        bool,
        u8,
        u16,
        i32,
        bool,
        bool,
        bool,
        u8,
    );
    // PAL (vs==0) or NTSC (vs==1): Rust hybrid VIC uses the matching cycle table.
    let hybrid_effective = hybrid_vic_pal;
    let mut vicii_opt = if hybrid_effective {
        Some(ViciiEngine::from_python_state(
            v_raster_line,
            v_raster_cycle,
            v_allow_bad_lines,
            v_bad_line,
            v_ysmooth,
            v_den,
            v_raster_irq_line,
            v_raster_irq_triggered,
            v_prefetch_cycles,
            v_first_dma_line,
            v_last_dma_line,
            v_sprite_enable_mask,
            v_cycles_per_line,
            v_num_raster_lines,
        ))
    } else {
        None
    };

    // Beam: copy Python bytearrays into Rust-owned buffers before releasing the GIL.
    let mut beam_vic_backing: Option<Vec<u8>> = None;
    let mut beam_cia2_backing: Option<Vec<u8>> = None;
    let mut beam_capture_active = false;
    if beam_render_enabled {
        let n = beam_nlines as usize;
        if n == 0 {
            return Err(PyValueError::new_err(
                "beam_render_enabled requires beam_nlines > 0",
            ));
        }
        let Some(ref bv) = beam_vic_flat else {
            return Err(PyValueError::new_err(
                "beam_vic_flat is required when beam_render_enabled",
            ));
        };
        let Some(ref bc) = beam_cia2_flat else {
            return Err(PyValueError::new_err(
                "beam_cia2_flat is required when beam_render_enabled",
            ));
        };
        let (vs, cs) = unsafe {
            (bv.as_bytes().to_vec(), bc.as_bytes().to_vec())
        };
        if vs.len() != n * 64 || cs.len() != n {
            return Err(PyValueError::new_err(format!(
                "beam buffers must be {} and {} bytes, got {} and {}",
                n * 64,
                n,
                vs.len(),
                cs.len()
            )));
        }
        beam_vic_backing = Some(vs);
        beam_cia2_backing = Some(cs);
        beam_capture_active = true;
    }

    // Run the heavy batch outside the GIL so Python-side audio/UI threads can run.
    // Safe: RAM and beam buffers are copied into Rust-owned Vecs; no Python objects are
    // touched inside the detached closure. Beam results are copied back after detach.
    let result: Result<(OutTuple, Vec<u8>, Vec<u8>, [u32; 14], Option<Vec<u8>>, Option<Vec<u8>>), String> =
        py.detach(move || (move || {
        let ram_arr: &mut [u8; 65536] = backing
            .as_mut_slice()
            .try_into()
            .map_err(|_| "ram slice".to_string())?;
        let mut mem = C64MemoryMap::new(ram_arr);
        mem.video_standard = vs;
        mem.raster_line = raster_line;
        mem.raster_cycles = raster_cycles;
        mem.vic_regs = vic_regs;
        mem.vic_interrupt_state = vic_interrupt_state;
        mem.pending_irq = pending_irq;
        mem.cia1_icr = cia1_icr;
        mem.cia2_pra = cia2_pra;
        mem.cia2_ddra = cia2_ddra;
        mem.iec_merge_cia2 = iec_enabled;
        mem.iec_peer_clk_high = iec_peer_clk_high;
        mem.iec_peer_data_high = iec_peer_data_high;
        mem.cia1_timer_a = CiaTimer {
            latch: ta_latch,
            counter: ta_counter,
            running: ta_running,
            irq_enabled: ta_irq_en,
            one_shot: ta_oneshot,
            input_mode: ta_input,
        };
        mem.cia1_timer_b = CiaTimer {
            latch: tb_latch,
            counter: tb_counter,
            running: tb_running,
            irq_enabled: tb_irq_en,
            one_shot: tb_oneshot,
            input_mode: tb_input,
        };
        mem.basic_rom = basic_rom.as_deref();
        mem.kernal_rom = kernal_rom.as_deref();
        mem.char_rom = char_rom.as_deref();
        mem.invalidate_6510_port_read_cache();

        if beam_capture_active {
            mem.beam_enabled = true;
            mem.beam_nlines = beam_nlines;
            if let Some(ref mut bv) = beam_vic_backing {
                mem.beam_vic_ptr = bv.as_mut_ptr();
            }
            if let Some(ref mut bc) = beam_cia2_backing {
                mem.beam_cia2_ptr = bc.as_mut_ptr();
            }
        }

        let mut resid_box: Option<Box<ResidSession>> =
            match (&resid_lib_path, resid_ptr) {
                (Some(path), Some(ptr)) if ptr != 0 => {
                    Some(Box::new(ResidSession::open(path, ptr as usize)?))
                }
                _ => None,
            };
        if let Some(ref mut b) = resid_box {
            mem.resid = std::ptr::from_mut::<ResidSession>(b.as_mut());
        }

        let mut cpu = CpuState {
            pc,
            a,
            x,
            y,
            sp,
            p,
            cycles,
            stopped,
        };
        let mut stops = stop_pcs.unwrap_or_default();
        // Python passes a sorted tuple from CPU6502::_rust_delegate_stop_pcs; skip work when already sorted.
        if stops.len() > 1 && !stops.is_sorted() {
            stops.sort_unstable();
        }
        stops.dedup();
        let (ins, cyc) = run_fast_batch(
            &mut cpu,
            &mut mem,
            max_instructions,
            &stops,
            hybrid_effective,
            vicii_opt.as_mut(),
            resid_box.as_deref_mut(),
        );
        mem.resid = std::ptr::null_mut();

        let pcm_bytes = resid_box.map_or_else(Vec::new, |mut b| b.take_pcm_le_bytes());

        let vpack = if let Some(ref eng) = vicii_opt {
            eng.export_u32()
        } else {
            [0u32; 14]
        };

        let out: OutTuple = (
            ins,
            cyc,
            cpu.pc,
            cpu.a,
            cpu.x,
            cpu.y,
            cpu.sp,
            cpu.p,
            cpu.cycles,
            cpu.stopped,
            mem.raster_line,
            mem.raster_cycles,
            mem.vic_regs,
            mem.vic_interrupt_state,
            mem.pending_irq,
            mem.cia1_icr,
            mem.cia2_pra,
            mem.cia2_ddra,
            mem.cia1_timer_a.latch,
            mem.cia1_timer_a.counter,
            mem.cia1_timer_a.running,
            mem.cia1_timer_a.irq_enabled,
            mem.cia1_timer_a.one_shot,
            mem.cia1_timer_a.input_mode,
            mem.cia1_timer_b.latch,
            mem.cia1_timer_b.counter,
            mem.cia1_timer_b.running,
            mem.cia1_timer_b.irq_enabled,
            mem.cia1_timer_b.one_shot,
            mem.cia1_timer_b.input_mode,
        );
        mem.beam_vic_ptr = std::ptr::null_mut();
        mem.beam_cia2_ptr = std::ptr::null_mut();
        mem.beam_enabled = false;
        Ok((out, backing, pcm_bytes, vpack, beam_vic_backing, beam_cia2_backing))
    })());
    let (out, backing_out, pcm_bytes, vpack, beam_vic_out, beam_cia2_out) =
        result.map_err(|e: String| PyValueError::new_err(e))?;
    let dst = unsafe { ram.as_bytes_mut() };
    dst.copy_from_slice(&backing_out);
    if let (Some(ref bv), Some(ref py_bv)) = (beam_vic_out.as_ref(), beam_vic_flat.as_ref()) {
        let dst = unsafe { py_bv.as_bytes_mut() };
        if dst.len() == bv.len() {
            dst.copy_from_slice(bv);
        }
    }
    if let (Some(ref bc), Some(ref py_bc)) = (beam_cia2_out.as_ref(), beam_cia2_flat.as_ref()) {
        let dst = unsafe { py_bc.as_bytes_mut() };
        if dst.len() == bc.len() {
            dst.copy_from_slice(bc);
        }
    }

    let (
        ins,
        cyc,
        opc,
        oa,
        ox,
        oy,
        osp,
        op,
        ocycles,
        ostopped,
        rline,
        rcycles,
        vregs,
        vist,
        pirq,
        cia_icr,
        c2pra,
        c2ddra,
        tala,
        tac,
        tar,
        taie,
        taos,
        tai,
        tbl,
        tbc,
        tbr,
        tbie,
        tbos,
        tbi,
    ) = out;
    let vic_bytes = PyBytes::new(py, &vregs);
    let pcm_py = PyBytes::new(py, &pcm_bytes);
    // Beam data is written in-place into Python bytearrays; keep empty trailers for tuple shape.
    let beam_vic_py = PyBytes::new(py, &[]);
    let beam_cia2_py = PyBytes::new(py, &[]);
    PyTuple::new(
        py,
        [
            ins.into_bound_py_any(py)?,
            cyc.into_bound_py_any(py)?,
            opc.into_bound_py_any(py)?,
            oa.into_bound_py_any(py)?,
            ox.into_bound_py_any(py)?,
            oy.into_bound_py_any(py)?,
            osp.into_bound_py_any(py)?,
            op.into_bound_py_any(py)?,
            ocycles.into_bound_py_any(py)?,
            ostopped.into_bound_py_any(py)?,
            rline.into_bound_py_any(py)?,
            rcycles.into_bound_py_any(py)?,
            vic_bytes.into_any(),
            vist.into_bound_py_any(py)?,
            pirq.into_bound_py_any(py)?,
            cia_icr.into_bound_py_any(py)?,
            c2pra.into_bound_py_any(py)?,
            c2ddra.into_bound_py_any(py)?,
            tala.into_bound_py_any(py)?,
            tac.into_bound_py_any(py)?,
            tar.into_bound_py_any(py)?,
            taie.into_bound_py_any(py)?,
            taos.into_bound_py_any(py)?,
            tai.into_bound_py_any(py)?,
            tbl.into_bound_py_any(py)?,
            tbc.into_bound_py_any(py)?,
            tbr.into_bound_py_any(py)?,
            tbie.into_bound_py_any(py)?,
            tbos.into_bound_py_any(py)?,
            tbi.into_bound_py_any(py)?,
            pcm_py.into_any(),
            vpack[0].into_bound_py_any(py)?,
            vpack[1].into_bound_py_any(py)?,
            vpack[2].into_bound_py_any(py)?,
            vpack[3].into_bound_py_any(py)?,
            vpack[4].into_bound_py_any(py)?,
            vpack[5].into_bound_py_any(py)?,
            vpack[6].into_bound_py_any(py)?,
            vpack[7].into_bound_py_any(py)?,
            vpack[8].into_bound_py_any(py)?,
            vpack[9].into_bound_py_any(py)?,
            vpack[10].into_bound_py_any(py)?,
            vpack[11].into_bound_py_any(py)?,
            vpack[12].into_bound_py_any(py)?,
            vpack[13].into_bound_py_any(py)?,
            beam_vic_py.into_any(),
            beam_cia2_py.into_any(),
        ],
    )
}

#[pymodule]
fn c64py_rust_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(ping, m)?)?;
    m.add_function(wrap_pyfunction!(rust_core_version, m)?)?;
    m.add_function(wrap_pyfunction!(run_fast_batch_py, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn memory_color_ram_roundtrip() {
        let mut ram = Box::new([0u8; 65536]);
        let mut m = C64MemoryMap::new(&mut *ram);
        m.write(0xDA89, 0xD6);
        assert_eq!(m.read(0xDA89), 0xD6);
    }
}
