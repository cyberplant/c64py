"""Emulator snapshot save/load.

A snapshot captures the canonical Python-side state of the emulator that is
sufficient to resume execution: CPU registers, full 64 KiB RAM, VIC register
shadow, raster/timing counters, both CIA1 timers and CIA1 ICR, CIA2 port/DDR,
and the VICII cycle engine state used by the accurate-VIC paths.

What is **NOT** saved (documented caveats):

- ROMs (BASIC/KERNAL/CHAR). The loader must supply the same ROM set when
  resuming, via the normal ``--rom-dir`` / env var mechanism.
- IEC bus / 1541 drive state. After resume the disk side starts fresh; any
  in-flight LOAD/SAVE would break. Snapshots are intended for the moment
  *after* disk I/O finishes, so this is usually fine.
- SID internal state (reSID pipeline, sample queue). Audio may glitch for a
  frame after resume but the register shadow is restored via RAM (I/O writes
  resend during play).
- Beam render buffers, UDP debug sockets, trace file handles, pygame window.

These limits are acceptable for the use case: skip long BASIC boots / loader
phases and resume at a playable state.

The Rust fast-batch core keeps no state of its own between calls — every
invocation reads Python state in and writes it back, so restoring the Python
side is enough to resume Rust-driven execution too.

Format: pickle of a flat ``dict`` with a magic marker and a version field.
Not a stable wire format; use only with the same c64py version that wrote it.
"""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Union

if TYPE_CHECKING:
    from .emulator import Emulator


SNAPSHOT_MAGIC = "C64PY-SNAP"
SNAPSHOT_VERSION = 1

PathLike = Union[str, Path]


class SnapshotError(RuntimeError):
    """Raised on malformed / incompatible snapshot files."""


def _cia_timer_to_dict(t) -> Dict[str, Any]:
    return {
        "latch": int(t.latch),
        "counter": int(t.counter),
        "running": bool(t.running),
        "irq_enabled": bool(t.irq_enabled),
        "one_shot": bool(t.one_shot),
        "input_mode": int(t.input_mode),
    }


def _apply_cia_timer(t, d: Dict[str, Any]) -> None:
    t.latch = int(d["latch"])
    t.counter = int(d["counter"])
    t.running = bool(d["running"])
    t.irq_enabled = bool(d["irq_enabled"])
    t.one_shot = bool(d["one_shot"])
    t.input_mode = int(d["input_mode"])


# VIC cycle engine fields the Rust batch serializes per call. Keeping this
# list in sync with _core.run_fast_batch guarantees the resumed Python side
# hands Rust the same state it had at save time.
_VIC_ENGINE_FIELDS = (
    "raster_line",
    "raster_cycle",
    "allow_bad_lines",
    "bad_line",
    "ysmooth",
    "den",
    "raster_irq_line",
    "raster_irq_triggered",
    "prefetch_cycles",
    "first_dma_line",
    "last_dma_line",
    "sprite_enable_mask",
    "cycles_per_line",
    "num_raster_lines",
    "sprite_sprite_collision",
    "sprite_bg_collision",
    # VICE-style sprite DMA state (vicii-cycle.c). These must round-trip so a
    # snapshot taken during an active sprite DMA (e.g. mid-frame) resumes with
    # the correct BA arbitration and DMA turn-off timing.
    "sprite_dma_mask",
    "sprite_exp_flop",
    "sprite_y_expand_mask",
    "sprite_y",
    "sprite_mc",
    "sprite_mcbase",
)


def build_payload(emu: "Emulator", *, note: str = "") -> Dict[str, Any]:
    """Build a fully-serializable payload dict from the live emulator."""
    mem = emu.memory
    cpu = emu.cpu
    st = cpu.state

    payload: Dict[str, Any] = {
        "magic": SNAPSHOT_MAGIC,
        "version": SNAPSHOT_VERSION,
        "note": str(note),
        "emulator": {
            "current_cycles": int(emu.current_cycles),
            "vic_emulation": getattr(emu, "vic_emulation", "fast"),
        },
        "cpu": {
            "pc": int(st.pc) & 0xFFFF,
            "a": int(st.a) & 0xFF,
            "x": int(st.x) & 0xFF,
            "y": int(st.y) & 0xFF,
            "sp": int(st.sp) & 0xFF,
            "p": int(st.p) & 0xFF,
            "cycles": int(st.cycles),
            "stopped": bool(st.stopped),
            "jiffy_clock": int(getattr(cpu, "jiffy_clock", 0)),
        },
        "memory": {
            "video_standard": str(mem.video_standard),
            "ram": bytes(mem.ram),
            "vic_regs": bytes(mem._vic_regs),
            "raster_line": int(mem.raster_line),
            "raster_cycles": int(mem.raster_cycles),
            "badline_cycles": int(mem.badline_cycles),
            "vic_badline_triggered_line": int(mem.vic_badline_triggered_line),
            "vic_interrupt_state": int(mem.vic_interrupt_state),
            "vic_den_latched": bool(mem.vic_den_latched),
            "vic_yscroll_latched": int(mem.vic_yscroll_latched),
            "jiffy_cycles": int(mem.jiffy_cycles),
            "pending_irq": bool(mem.pending_irq),
            "cia1_icr": int(mem.cia1_icr),
            "cia2_pra": int(mem.cia2_pra) & 0xFF,
            "cia2_ddra": int(mem.cia2_ddra) & 0xFF,
            "cia1_timer_a": _cia_timer_to_dict(mem.cia1_timer_a),
            "cia1_timer_b": _cia_timer_to_dict(mem.cia1_timer_b),
        },
        "vic_engine": {f: _coerce(getattr(cpu.vic, f)) for f in _VIC_ENGINE_FIELDS},
    }
    return payload


def _coerce(v: Any) -> Any:
    """Force primitives (bools stay bools, ints stay ints)."""
    if isinstance(v, bool):
        return bool(v)
    if isinstance(v, int):
        return int(v)
    return v


def save_snapshot(emu: "Emulator", path: PathLike, *, note: str = "") -> Path:
    """Save *emu* state to *path* (pickle format). Returns the final Path."""
    payload = build_payload(emu, note=note)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(out)
    return out


def _validate(payload: Dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise SnapshotError("snapshot payload is not a dict")
    if payload.get("magic") != SNAPSHOT_MAGIC:
        raise SnapshotError(
            f"bad magic {payload.get('magic')!r} (expected {SNAPSHOT_MAGIC!r})"
        )
    ver = payload.get("version")
    if ver != SNAPSHOT_VERSION:
        raise SnapshotError(
            f"unsupported snapshot version {ver} (expected {SNAPSHOT_VERSION})"
        )
    for key in ("cpu", "memory", "emulator", "vic_engine"):
        if key not in payload:
            raise SnapshotError(f"missing section {key!r} in snapshot")
    ram = payload["memory"].get("ram")
    if not isinstance(ram, (bytes, bytearray)) or len(ram) != 0x10000:
        raise SnapshotError("memory.ram must be 65536 bytes")
    vic = payload["memory"].get("vic_regs")
    if not isinstance(vic, (bytes, bytearray)) or len(vic) != 0x40:
        raise SnapshotError("memory.vic_regs must be 64 bytes")


def apply_payload(emu: "Emulator", payload: Dict[str, Any]) -> None:
    """Restore *emu* from a validated payload dict (in place)."""
    _validate(payload)
    mem = emu.memory
    cpu = emu.cpu

    em = payload["emulator"]
    emu.current_cycles = int(em["current_cycles"])

    cpu_d = payload["cpu"]
    cpu.state.pc = int(cpu_d["pc"]) & 0xFFFF
    cpu.state.a = int(cpu_d["a"]) & 0xFF
    cpu.state.x = int(cpu_d["x"]) & 0xFF
    cpu.state.y = int(cpu_d["y"]) & 0xFF
    cpu.state.sp = int(cpu_d["sp"]) & 0xFF
    cpu.state.p = int(cpu_d["p"]) & 0xFF
    cpu.state.cycles = int(cpu_d["cycles"])
    cpu.state.stopped = bool(cpu_d["stopped"])
    cpu.jiffy_clock = int(cpu_d.get("jiffy_clock", 0))

    m = payload["memory"]
    # Preserve the shared bytearray identity: Rust batch holds references to
    # memory.ram / memory._vic_regs; slice-assign instead of rebinding.
    mem.ram[:] = bytes(m["ram"])
    mem._vic_regs[:] = bytes(m["vic_regs"])
    mem.video_standard = str(m["video_standard"])
    mem.raster_line = int(m["raster_line"])
    mem.raster_cycles = int(m["raster_cycles"])
    mem.badline_cycles = int(m["badline_cycles"])
    mem.vic_badline_triggered_line = int(m["vic_badline_triggered_line"])
    mem.vic_interrupt_state = int(m["vic_interrupt_state"])
    mem.vic_den_latched = bool(m["vic_den_latched"])
    mem.vic_yscroll_latched = int(m["vic_yscroll_latched"])
    mem.jiffy_cycles = int(m["jiffy_cycles"])
    mem.pending_irq = bool(m["pending_irq"])
    mem.cia1_icr = int(m["cia1_icr"])
    mem.cia2_pra = int(m["cia2_pra"]) & 0xFF
    mem.cia2_ddra = int(m["cia2_ddra"]) & 0xFF
    _apply_cia_timer(mem.cia1_timer_a, m["cia1_timer_a"])
    _apply_cia_timer(mem.cia1_timer_b, m["cia1_timer_b"])

    for f, v in payload["vic_engine"].items():
        setattr(cpu.vic, f, v)

    # Invalidate derived caches so the next read/step sees the restored state.
    mem.invalidate_6510_port_read_cache()
    cpu._vic_shadow_tuple = None
    cpu.apply_video_standard_geometry()
    if mem.iec_bus is not None:
        mem.apply_cia2_port_a_to_iec_bus()


def load_snapshot(emu: "Emulator", path: PathLike) -> Dict[str, Any]:
    """Load a snapshot file and apply it to *emu*. Returns the raw payload."""
    with open(path, "rb") as f:
        payload = pickle.load(f)
    apply_payload(emu, payload)
    return payload


def describe_payload(payload: Dict[str, Any]) -> str:
    """One-line human summary (for log messages)."""
    try:
        em = payload["emulator"]
        cpu = payload["cpu"]
        m = payload["memory"]
        return (
            f"snapshot v{payload.get('version')} "
            f"cycles={em.get('current_cycles')} "
            f"PC=${cpu.get('pc', 0):04X} "
            f"raster={m.get('raster_line')} "
            f"video={m.get('video_standard')}"
        )
    except Exception:
        return "snapshot (incomplete metadata)"
