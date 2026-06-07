"""
Optional native fast-path core (`c64py_rust_core`).

Build/install: from repo root, with ``maturin`` installed::

    maturin develop --manifest-path rust/c64py-core/Cargo.toml

If the extension is missing, ``is_available`` is False and callers keep using
the pure-Python ``CPU6502.step`` path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from .cpu import CPU6502
    from .memory import MemoryMap

try:
    import c64py_rust_core as _rust  # type: ignore[import-not-found]
except ImportError:
    _rust = None

is_available: bool = _rust is not None


def rust_core_version() -> str:
    if not _rust:
        return ""
    return str(_rust.rust_core_version())


def run_fast_batch(
    memory: "MemoryMap",
    *,
    max_instructions: int,
    pc: int,
    a: int,
    x: int,
    y: int,
    sp: int,
    p: int,
    cycles: int,
    stopped: bool,
    basic_rom: Optional[bytes] = None,
    kernal_rom: Optional[bytes] = None,
    char_rom: Optional[bytes] = None,
    stop_pcs: Optional[Sequence[int]] = None,
    hybrid_vic_pal: bool = False,  # misnomer: enables Rust hybrid VIC for PAL or NTSC
    vic_engine: Optional[object] = None,
    resid_lib_path: Optional[str] = None,
    resid_ptr: Optional[int] = None,
) -> Tuple[int, int, int, int, int, int, int, int, int, bool]:
    """Run the Rust fast batch; sync ``memory.ram`` via a shared ``bytearray``.

    Returns
    -------
    (instructions_run, cycles_emulated, pc, a, x, y, sp, p, cpu_cycles, stopped)
    """
    if not _rust:
        raise RuntimeError("c64py_rust_core is not built/importable")
    ram = memory.ram
    if not isinstance(ram, bytearray):
        raise TypeError("memory.ram must be a bytearray for the Rust fast path")
    old_v_line = int(getattr(vic_engine, "raster_line", 0)) if vic_engine is not None else 0
    old_v_cycle = int(getattr(vic_engine, "raster_cycle", 0)) if vic_engine is not None else 0
    vic = memory._vic_regs
    if len(vic) != 64:
        raise ValueError("VIC register shadow must be 64 bytes")

    ta = memory.cia1_timer_a
    tb = memory.cia1_timer_b
    vs = memory.video_standard
    if vs not in ("pal", "ntsc"):
        vs = "pal"

    stops: List[int]
    if stop_pcs is None:
        stops = []
    else:
        stops = [int(x) & 0xFFFF for x in stop_pcs]

    t = _rust.run_fast_batch_py(
        ram,
        max_instructions,
        pc,
        a,
        x,
        y,
        sp,
        p,
        cycles,
        stopped,
        vs,
        memory.raster_line,
        memory.raster_cycles,
        bytes(vic),
        memory.vic_interrupt_state,
        memory.pending_irq,
        memory.cia1_icr,
        memory.cia2_pra,
        memory.cia2_ddra,
        ta.latch,
        ta.counter,
        ta.running,
        ta.irq_enabled,
        ta.one_shot,
        ta.input_mode,
        tb.latch,
        tb.counter,
        tb.running,
        tb.irq_enabled,
        tb.one_shot,
        tb.input_mode,
        None if basic_rom is None else bytes(basic_rom),
        None if kernal_rom is None else bytes(kernal_rom),
        None if char_rom is None else bytes(char_rom),
        stops,
        bool(hybrid_vic_pal),
        0 if vic_engine is None else int(getattr(vic_engine, "raster_line", 0)),
        0 if vic_engine is None else int(getattr(vic_engine, "raster_cycle", 0)),
        False if vic_engine is None else bool(getattr(vic_engine, "allow_bad_lines", False)),
        False if vic_engine is None else bool(getattr(vic_engine, "bad_line", False)),
        0 if vic_engine is None else int(getattr(vic_engine, "ysmooth", 0)),
        False if vic_engine is None else bool(getattr(vic_engine, "den", False)),
        0 if vic_engine is None else int(getattr(vic_engine, "raster_irq_line", 0)),
        False if vic_engine is None else bool(getattr(vic_engine, "raster_irq_triggered", False)),
        0 if vic_engine is None else int(getattr(vic_engine, "prefetch_cycles", 0)),
        48 if vic_engine is None else int(getattr(vic_engine, "first_dma_line", 48)),
        247 if vic_engine is None else int(getattr(vic_engine, "last_dma_line", 247)),
        0 if vic_engine is None else int(getattr(vic_engine, "sprite_enable_mask", 0)),
        63 if vic_engine is None else int(getattr(vic_engine, "cycles_per_line", 63)),
        312 if vic_engine is None else int(getattr(vic_engine, "num_raster_lines", 312)),
        resid_lib_path,
        None if resid_ptr is None else int(resid_ptr),
    )
    (
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
        vic_blob,
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
        pcm_bytes,
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
    ) = t
    memory.raster_line = rline
    memory.raster_cycles = rcycles
    memory._vic_regs[:] = vic_blob
    memory.vic_interrupt_state = vist
    memory.pending_irq = pirq
    memory.cia1_icr = cia_icr
    memory.cia2_pra = c2pra
    memory.cia2_ddra = c2ddra
    ta.latch = tala
    ta.counter = tac
    ta.running = tar
    ta.irq_enabled = taie
    ta.one_shot = taos
    ta.input_mode = tai
    tb.latch = tbl
    tb.counter = tbc
    tb.running = tbr
    tb.irq_enabled = tbie
    tb.one_shot = tbos
    tb.input_mode = tbi
    if vic_engine is not None:
        vic_engine.raster_line = int(v_raster_line)
        vic_engine.raster_cycle = int(v_raster_cycle)
        vic_engine.allow_bad_lines = bool(v_allow_bad_lines)
        vic_engine.bad_line = bool(v_bad_line)
        vic_engine.ysmooth = int(v_ysmooth)
        vic_engine.den = bool(v_den)
        vic_engine.raster_irq_line = int(v_raster_irq_line)
        vic_engine.raster_irq_triggered = bool(v_raster_irq_triggered)
        vic_engine.prefetch_cycles = int(v_prefetch_cycles)
        vic_engine.first_dma_line = int(v_first_dma_line)
        vic_engine.last_dma_line = int(v_last_dma_line)
        vic_engine.sprite_enable_mask = int(v_sprite_enable_mask)
        vic_engine.cycles_per_line = int(v_cycles_per_line)
        vic_engine.num_raster_lines = int(v_num_raster_lines)
        if hybrid_vic_pal and memory.vic_snapshot_each_emulated_frame:
            # Rust hybrid VIC advances per emulated CPU cycle; detect frame wrap(s) in batch and
            # preserve the same render-latch behavior as Python accurate path.
            frame_len = int(vic_engine.cycles_per_line) * int(vic_engine.num_raster_lines)
            if frame_len > 0 and cyc > 0:
                old_pos = old_v_line * int(vic_engine.cycles_per_line) + old_v_cycle
                to_wrap = frame_len - old_pos
                if to_wrap <= 0:
                    to_wrap = frame_len
                if cyc >= to_wrap:
                    memory.snapshot_vic_render_state()
    memory.invalidate_6510_port_read_cache()
    if pcm_bytes and hasattr(memory.sid, "extend_pcm_from_rust"):
        memory.sid.extend_pcm_from_rust(pcm_bytes)
    return ins, cyc, opc, oa, ox, oy, osp, op, ocycles, ostopped
