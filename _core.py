"""
Optional native fast-path core (`c64py_rust_core`).

Build/install: from repo root, with ``maturin`` installed::

    maturin develop --manifest-path rust/c64py-core/Cargo.toml

If the extension is missing, ``is_available`` is False and callers keep using
the pure-Python ``CPU6502.step`` loop instead of batched Rust execution when the
emulator would otherwise use it: ``--vic-emulation fast`` and ``accurate-rust``.
``accurate-python`` always steps in Python with cycle-accurate VIC in Python and
never uses this batch path. With ``--video-rendering per-cycle`` and a built
``c64py_rust_core``, ``--vic-emulation accurate-rust`` can use the same batch path
while filling per-cycle VIC/CIA2 flat buffers from the Rust hybrid VIC.
"""

from __future__ import annotations

import os
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


def _pack_sprite_arrays(vic_engine):
    """Pack ``sprite_y``, ``sprite_mc``, ``sprite_mcbase`` into (y_lo, y_hi, mc_lo,
    mc_hi, mcbase_lo, mcbase_hi) u32s for the Rust signature.

    Each u32 carries 4 × u8 little-endian (byte 0 = lowest-indexed sprite).
    Keeping them packed avoids 24 extra PyO3 parameters.
    """
    def pack4(arr, off):
        return (
            int(arr[off]) & 0xFF
            | (int(arr[off + 1]) & 0xFF) << 8
            | (int(arr[off + 2]) & 0xFF) << 16
            | (int(arr[off + 3]) & 0xFF) << 24
        )
    if vic_engine is None:
        return (0, 0, 0, 0, 0, 0)
    y = getattr(vic_engine, "sprite_y", [0] * 8)
    mc = getattr(vic_engine, "sprite_mc", [0] * 8)
    mcb = getattr(vic_engine, "sprite_mcbase", [0] * 8)
    return (
        pack4(y, 0), pack4(y, 4),
        pack4(mc, 0), pack4(mc, 4),
        pack4(mcb, 0), pack4(mcb, 4),
    )


def _unpack_sprite_array(lo: int, hi: int) -> list:
    """Inverse of :func:`_pack_sprite_arrays` for a single 8-byte packed pair."""
    return [
        lo & 0xFF, (lo >> 8) & 0xFF, (lo >> 16) & 0xFF, (lo >> 24) & 0xFF,
        hi & 0xFF, (hi >> 8) & 0xFF, (hi >> 16) & 0xFF, (hi >> 24) & 0xFF,
    ]


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
    trace_path: Optional[str] = None,
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
    t2a = memory.cia2_timer_a
    t2b = memory.cia2_timer_b
    vs = memory.video_standard
    if vs not in ("pal", "ntsc"):
        vs = "pal"

    stops: List[int]
    if stop_pcs is None:
        stops = []
    else:
        stops = [int(x) & 0xFFFF for x in stop_pcs]

    entry_cia2_pra = int(memory.cia2_pra) & 0xFF
    iec_wire_replay = (
        memory.iec_bus is not None
        and getattr(getattr(memory, "iec_kernal_tap", None), "_wire_decoder", None) is not None
    )

    if memory.iec_bus is not None:
        memory.apply_cia2_port_a_to_iec_bus()
        iec_enabled = True
        peer_clk = bool(memory.iec_bus.peer_clk_high())
        peer_data = bool(memory.iec_bus.peer_data_high())
    else:
        iec_enabled = False
        peer_clk = True
        peer_data = True

    beam_enabled = bool(getattr(memory, "beam_render_enabled", False))
    beam_n = 0
    bv_ba: Optional[bytearray] = None
    bc_ba: Optional[bytearray] = None
    if beam_enabled:
        memory.ensure_beam_buffers()
        vf = getattr(memory, "beam_vic_flat", None)
        cf = getattr(memory, "beam_cia2_flat", None)
        blines = memory.beam_vic_lines
        if (
            vf is not None
            and cf is not None
            and blines is not None
            and len(vf) == len(blines) * 64
            and len(cf) == len(blines)
        ):
            beam_n = len(blines)
            bv_ba = vf
            bc_ba = cf
    beam_rust = bool(beam_enabled and beam_n > 0 and bv_ba is not None and bc_ba is not None)

    def _want_rust_per_cycle_capture() -> bool:
        v = os.environ.get("C64PY_RUST_PER_CYCLE", "1").strip().lower()
        return v not in ("0", "no", "false", "python", "off")

    per_cycle_cap = (
        bool(getattr(memory, "per_cycle_render_enabled", False))
        and bool(hybrid_vic_pal)
        and _want_rust_per_cycle_capture()
    )
    pvc_ba: Optional[bytearray] = None
    pcc_ba: Optional[bytearray] = None
    if per_cycle_cap:
        memory.ensure_per_cycle_buffers()
        pvf = getattr(memory, "per_cycle_vic_flat", None)
        pcf = getattr(memory, "per_cycle_cia2_flat", None)
        if (
            pvf is not None
            and pcf is not None
            and len(pvf) == 512000
            and len(pcf) == 8000
        ):
            pvc_ba = pvf
            pcc_ba = pcf
    per_cycle_rust = bool(per_cycle_cap and pvc_ba is not None and pcc_ba is not None)

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
        memory.cia2_icr,
        t2a.latch,
        t2a.counter,
        t2a.running,
        t2a.irq_enabled,
        t2a.one_shot,
        t2a.input_mode,
        t2b.latch,
        t2b.counter,
        t2b.running,
        t2b.irq_enabled,
        t2b.one_shot,
        t2b.input_mode,
        int(memory.cia1_pra) & 0xFF,
        int(memory.cia1_prb) & 0xFF,
        int(memory.cia1_ddra) & 0xFF,
        int(memory.cia1_ddrb) & 0xFF,
        bytes(memory.keyboard_matrix),
        int(memory.joy_inject1_clear) & 0xFF,
        int(memory.joy_inject2_clear) & 0xFF,
        int(memory.joy_held1_clear) & 0xFF,
        int(memory.joy_held2_clear) & 0xFF,
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
        # VICE-style sprite DMA state round-tripped with the engine. Packed as
        # u32 pairs (low/high) for the 8-sprite per-sprite arrays so the PyO3
        # signature stays compact.
        0 if vic_engine is None else int(getattr(vic_engine, "sprite_dma_mask", 0)),
        0xFF if vic_engine is None else int(getattr(vic_engine, "sprite_exp_flop", 0xFF)),
        0 if vic_engine is None else int(getattr(vic_engine, "sprite_y_expand_mask", 0)),
        *_pack_sprite_arrays(vic_engine),
        resid_lib_path,
        None if resid_ptr is None else int(resid_ptr),
        iec_enabled,
        peer_clk,
        peer_data,
        beam_rust,
        beam_n if beam_rust else 0,
        bv_ba if beam_rust else None,
        bc_ba if beam_rust else None,
        per_cycle_rust,
        pvc_ba if per_cycle_rust else None,
        pcc_ba if per_cycle_rust else None,
        trace_path,
        iec_wire_replay,
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
        c2icr,
        c2tala,
        c2tac,
        c2tar,
        c2taie,
        c2taos,
        c2tai,
        c2tbl,
        c2tbc,
        c2tbr,
        c2tbie,
        c2tbos,
        c2tbi,
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
        out_cia1_pra,
        out_cia1_prb,
        out_cia1_ddra,
        out_cia1_ddrb,
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
        v_sprite_sprite_collision,
        v_sprite_bg_collision,
        v_sprite_dma_mask,
        v_sprite_exp_flop,
        v_sprite_mc_lo,
        v_sprite_mc_hi,
        v_sprite_mcbase_lo,
        v_sprite_mcbase_hi,
        beam_vic_bytes,
        beam_cia2_bytes,
        iec_cia2_log_bytes,
    ) = t
    memory.raster_line = rline
    memory.raster_cycles = rcycles
    memory._vic_regs[:] = vic_blob
    memory.vic_interrupt_state = vist
    memory.pending_irq = pirq
    memory.cia1_icr = cia_icr
    memory.cia2_ddra = c2ddra
    memory.cia2_icr = c2icr & 0xFF
    t2a.latch = c2tala
    t2a.counter = c2tac
    t2a.running = c2tar
    t2a.irq_enabled = c2taie
    t2a.one_shot = c2taos
    t2a.input_mode = c2tai
    t2b.latch = c2tbl
    t2b.counter = c2tbc
    t2b.running = c2tbr
    t2b.irq_enabled = c2tbie
    t2b.one_shot = c2tbos
    t2b.input_mode = c2tbi
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
    memory.cia1_pra = int(out_cia1_pra) & 0xFF
    memory.cia1_prb = int(out_cia1_prb) & 0xFF
    memory.cia1_ddra = int(out_cia1_ddra) & 0xFF
    memory.cia1_ddrb = int(out_cia1_ddrb) & 0xFF
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
        vic_engine.sprite_sprite_collision = int(v_sprite_sprite_collision)
        vic_engine.sprite_bg_collision = int(v_sprite_bg_collision)
        vic_engine.sprite_dma_mask = int(v_sprite_dma_mask) & 0xFF
        vic_engine.sprite_exp_flop = int(v_sprite_exp_flop) & 0xFF
        vic_engine.sprite_mc = _unpack_sprite_array(int(v_sprite_mc_lo), int(v_sprite_mc_hi))
        vic_engine.sprite_mcbase = _unpack_sprite_array(int(v_sprite_mcbase_lo), int(v_sprite_mcbase_hi))
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
    if memory.iec_bus is not None:
        if iec_wire_replay and iec_cia2_log_bytes:
            memory.cia2_pra = entry_cia2_pra
            memory.apply_cia2_port_a_to_iec_bus()
            for b in iec_cia2_log_bytes:
                memory.cia2_pra = int(b) & 0xFF
                memory.apply_cia2_port_a_to_iec_bus()
        memory.cia2_pra = int(c2pra) & 0xFF
        memory.apply_cia2_port_a_to_iec_bus()
    else:
        memory.cia2_pra = int(c2pra) & 0xFF
    # Beam VIC/CIA2: Rust wrote in-place into memory.beam_vic_flat / beam_cia2_flat.
    if beam_rust and beam_n > 0 and getattr(memory, "beam_vic_flat", None) is not None:
        memory.beam_snapshots_primed = True
    return ins, cyc, opc, oa, ox, oy, osp, op, ocycles, ostopped


def composite_per_cycle_frame(
    memory: "MemoryMap",
    palette_rgb48: bytes,
    rgb_out: bytearray,
    *,
    video_standard: str,
    native_width: int,
    native_height: int,
    screen_left: int,
    screen_top: int,
    screen_width: int,
    screen_height: int,
    border_px: int,
    skip_sprites: bool = False,
) -> None:
    """Fill *rgb_out* (packed RGB888, row-major) using the Rust per-cycle compositor."""
    if not _rust:
        raise RuntimeError("c64py_rust_core is not built/importable")
    flat = getattr(memory, "per_cycle_vic_flat", None)
    cia = getattr(memory, "per_cycle_cia2_flat", None)
    if flat is None or cia is None or len(flat) != 512000 or len(cia) != 8000:
        raise ValueError("per_cycle flat buffers must be allocated (512000 + 8000 bytes)")
    ram = memory.ram
    if not isinstance(ram, bytearray):
        raise TypeError("memory.ram must be a bytearray")
    if len(palette_rgb48) != 48:
        raise ValueError("palette_rgb48 must be exactly 48 bytes")
    vs = video_standard if video_standard in ("pal", "ntsc") else "pal"
    cr = memory.char_rom
    cr_py = None if not cr else bytes(cr)
    _rust.composite_per_cycle_frame_py(
        ram,
        flat,
        cia,
        cr_py,
        vs,
        palette_rgb48,
        rgb_out,
        int(native_width),
        int(native_height),
        int(screen_left),
        int(screen_top),
        int(screen_width),
        int(screen_height),
        int(border_px),
        bool(skip_sprites),
        bytes(memory._vic_regs[:64]),
        int(memory.cia2_pra) & 0xFF,
    )
