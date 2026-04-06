"""
Optional native fast-path core (`c64py_rust_core`).

Build/install: from repo root, with ``maturin`` installed::

    maturin develop --manifest-path rust/c64py-core/Cargo.toml

If the extension is missing, ``is_available`` is False and callers keep using
the pure-Python ``CPU6502.step`` path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Tuple

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
    vic = memory._vic_regs
    if len(vic) != 64:
        raise ValueError("VIC register shadow must be 64 bytes")

    ta = memory.cia1_timer_a
    tb = memory.cia1_timer_b
    vs = memory.video_standard
    if vs not in ("pal", "ntsc"):
        vs = "pal"

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
    memory.invalidate_6510_port_read_cache()
    return ins, cyc, opc, oa, ox, oy, osp, op, ocycles, ostopped
