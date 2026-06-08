"""KERNAL CIA2 bit-level IEC → logical :class:`IECBus` bridge (work in progress).

The stock KERNAL performs disk I/O by bit-banging CIA2 port A (ATN/CLK/DATA).
c64py mirrors those levels onto :class:`~c64py.iec_bus.IECBus`, but
:class:`~c64py.drives.tcp_drive_client.TcpDriveClient` only emits JSON when the
**logical** layer runs (``send_command`` / ``send_byte``). Until this bridge
decodes wire transitions into that layer, ``OPEN`` / ``PRINT#`` / ``INPUT#`` hang
against TCP drives while ``LOAD`` / ``SAVE`` still work via KERNAL hooks.

**Phase 0** (implemented here): when any TCP drive is attached, install
:class:`KernalIecTap` on :attr:`MemoryMap.iec_kernal_tap`. Each CIA2 port A write
that changes resolved bus lines records ``(debug_last_cycles, ATN, CLK, DATA)``
in a bounded ring for tests and future decoders.

Set ``C64PY_IEC_TAP_JSONL=/path/to/tap.jsonl`` to append every transition to
newline-delimited JSON for offline analysis. Each line has the stable schema
``{"cyc": int, "atn": bool, "clk": bool, "data": bool}``; booleans use the
same :class:`IECBus` convention as the in-memory ring (``true`` = released/high,
``false`` = asserted/low).

**Phase A3a–A3c (partial)** (optional, ``C64PY_IEC_WIRE_DECODE=1``):
:class:`KernalIecTap` may host :class:`~c64py.iec_wire_decode.IecAtnWireDecoder`
which decodes ATN-held commands via :meth:`~c64py.iec_bus.IECBus.deliver_command`
and C64→listener data (OPEN filename and ``PRINT#`` payload) via
:meth:`~c64py.iec_bus.IECBus.send_byte`.

**Drive-initiated line changes** (in-process 1541 VIA, etc.): when the tap is
attached via :meth:`KernalIecTap.attach_line_receiver`, :class:`~c64py.iec_bus.IECBus`
invokes :meth:`KernalIecTap.on_iec_lines` on every resolved ``(ATN, CLK, DATA)``
change (CIA2 updates are batched so they still produce a single tap event per
port write).

Experimental ``C64PY_IEC_WIRE_DECODE_TALK=1``: after wire decode is enabled,
also call :meth:`~c64py.iec_bus.IECBus.receive_byte` when a byte is assembled
from drive-clocked edges during ``TALK`` data phase (can desync TCP peers — for
debugging only).
"""

from __future__ import annotations

import json
import os
from collections import deque
from typing import TYPE_CHECKING, Deque, List, Optional, TextIO, Tuple, Union

if TYPE_CHECKING:
    from .iec_bus import IECBus
    from .iec_wire_decode import IecAtnWireDecoder
    from .memory import MemoryMap

# Ring cap keeps RAM bounded if KERNAL chatters during hung OPEN.
_DEFAULT_MAX_EVENTS = 8192

_TRUTHY = frozenset({"1", "yes", "true", "on"})


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


class KernalIecTap:
    """Records IEC line triples after CIA2 updates and optional drive line changes."""

    __slots__ = (
        "_attached_bus",
        "_events",
        "_jsonl_file",
        "_mem_ref",
        "_prev",
        "_wire_decoder",
        "jsonl_path",
        "transition_count",
    )

    def __init__(
        self,
        *,
        maxlen: int = _DEFAULT_MAX_EVENTS,
        jsonl_path: Optional[Union[str, os.PathLike[str]]] = None,
        wire_decode_bus: Optional["IECBus"] = None,
    ) -> None:
        self._events: Deque[Tuple[int, bool, bool, bool]] = deque(maxlen=maxlen)
        self._prev: Optional[Tuple[bool, bool, bool]] = None
        self._mem_ref: Optional["MemoryMap"] = None
        self._attached_bus: Optional["IECBus"] = None
        self._wire_decoder: Optional["IecAtnWireDecoder"] = None
        if wire_decode_bus is not None:
            from .iec_wire_decode import IecAtnWireDecoder

            self._wire_decoder = IecAtnWireDecoder(
                wire_decode_bus,
                talk_pull_receive=_env_truthy("C64PY_IEC_WIRE_DECODE_TALK"),
            )
        env_path = os.environ.get("C64PY_IEC_TAP_JSONL") if jsonl_path is None else None
        chosen_path = jsonl_path if jsonl_path is not None else env_path
        self.jsonl_path = os.fspath(chosen_path) if chosen_path else None
        self._jsonl_file: Optional[TextIO] = None
        if self.jsonl_path:
            parent = os.path.dirname(os.path.abspath(self.jsonl_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._jsonl_file = open(self.jsonl_path, "a", encoding="utf-8", buffering=1)
        self.transition_count = 0

    def attach_line_receiver(self, bus: "IECBus") -> None:
        """Register on ``bus`` to observe all resolved IEC line transitions."""
        self.detach_line_receiver()
        self._attached_bus = bus
        bus.iec_line_receiver = self

    def detach_line_receiver(self) -> None:
        """Undo :meth:`attach_line_receiver` if this tap is the active receiver."""
        bus = self._attached_bus
        if bus is not None and getattr(bus, "iec_line_receiver", None) is self:
            bus.iec_line_receiver = None
        self._attached_bus = None

    def on_iec_lines(self, before: Tuple[bool, bool, bool], after: Tuple[bool, bool, bool], cyc: int = 0) -> None:
        """:class:`~c64py.iec_bus.IECBus` callback for non-CIA line changes (e.g. drive CLK)."""
        self._record_line_change(self._mem_ref, before, after, cyc)

    def _record_line_change(
        self,
        mem: Optional["MemoryMap"],
        before: Tuple[bool, bool, bool],
        after: Tuple[bool, bool, bool],
        cyc_fallback: int = 0,
    ) -> None:
        if before == after:
            return
        cyc = cyc_fallback
        if mem is not None:
            cyc = int(getattr(mem, "debug_last_cycles", 0))
        self._events.append((cyc, after[0], after[1], after[2]))
        self._write_jsonl(cyc, after[0], after[1], after[2])
        self.transition_count += 1
        wd = self._wire_decoder
        if wd is not None:
            wd.feed_transition(before, after, cyc)
        self._prev = after

    def after_cia2_applied(self, mem: "MemoryMap") -> None:
        bus = mem.iec_bus
        if bus is None:
            return
        self._mem_ref = mem
        cur = bus.line_triple()
        if self._prev is None:
            self._prev = cur
            return
        if cur != self._prev:
            self._record_line_change(mem, self._prev, cur, 0)

    def _write_jsonl(self, cyc: int, atn: bool, clk: bool, data: bool) -> None:
        if self._jsonl_file is None:
            return
        rec = {"cyc": cyc, "atn": atn, "clk": clk, "data": data}
        self._jsonl_file.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def recent_events(self) -> List[Tuple[int, bool, bool, bool]]:
        """Copy of recorded transitions (oldest first within the ring)."""
        return list(self._events)

    def flush(self) -> None:
        """Flush the optional JSONL sink."""
        if self._jsonl_file is not None:
            self._jsonl_file.flush()

    def close(self) -> None:
        """Close the optional JSONL sink."""
        self.detach_line_receiver()
        if self._jsonl_file is not None:
            self._jsonl_file.close()
            self._jsonl_file = None

    def clear(self) -> None:
        self._events.clear()
        self._prev = None
        self.transition_count = 0
        wd = self._wire_decoder
        if wd is not None:
            wd.reset()
