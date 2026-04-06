//! PyO3 extension `c64py_rust_core` — optional fast-path CPU batch runner.

mod c64_cpu;
mod c64_fast;
mod c64_memory;
mod c64_timing;

use c64_cpu::CpuState;
use c64_memory::{C64MemoryMap, CiaTimer};
use c64_fast::run_fast_batch;
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
    basic_rom=None, kernal_rom=None, char_rom=None, stop_pcs=None
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
    let result: Result<(OutTuple, Vec<u8>), String> = (move || {
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
        stops.sort_unstable();
        stops.dedup();
        let (ins, cyc) = run_fast_batch(&mut cpu, &mut mem, max_instructions, &stops);
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
        Ok((out, backing))
    })();
    let (out, backing_out) = result.map_err(|e: String| PyValueError::new_err(e))?;
    let dst = unsafe { ram.as_bytes_mut() };
    dst.copy_from_slice(&backing_out);

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
