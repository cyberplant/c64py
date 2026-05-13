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

Later phases (see ``docs/plans/release_blockers_iec_percycle_vic.md``): bit-layer
state machine, ATN command framing, OPEN filename assembly, UNLISTEN →
``send_command`` / ``send_byte`` synthesis, PRINT# data phase, and drive
``request_byte`` replies.
"""

from __future__ import annotations

import json
import os
from collections import deque
from typing import TYPE_CHECKING, Deque, List, Optional, TextIO, Tuple, Union

if TYPE_CHECKING:
    from .memory import MemoryMap

# Ring cap keeps RAM bounded if KERNAL chatters during hung OPEN.
_DEFAULT_MAX_EVENTS = 8192


class KernalIecTap:
    """Records IEC line triples after each CIA2-derived bus update."""

    __slots__ = ("_events", "_jsonl_file", "_prev", "jsonl_path", "transition_count")

    def __init__(
        self,
        *,
        maxlen: int = _DEFAULT_MAX_EVENTS,
        jsonl_path: Optional[Union[str, os.PathLike[str]]] = None,
    ) -> None:
        self._events: Deque[Tuple[int, bool, bool, bool]] = deque(maxlen=maxlen)
        self._prev: Optional[Tuple[bool, bool, bool]] = None
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

    def after_cia2_applied(self, mem: "MemoryMap") -> None:
        bus = mem.iec_bus
        if bus is None:
            return
        cur = (bool(bus.atn), bool(bus.clk), bool(bus.data))
        if self._prev is None:
            self._prev = cur
            return
        if cur != self._prev:
            cyc = int(getattr(mem, "debug_last_cycles", 0))
            self._events.append((cyc, cur[0], cur[1], cur[2]))
            self._write_jsonl(cyc, cur[0], cur[1], cur[2])
            self.transition_count += 1
            self._prev = cur

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
        if self._jsonl_file is not None:
            self._jsonl_file.close()
            self._jsonl_file = None

    def clear(self) -> None:
        self._events.clear()
        self._prev = None
        self.transition_count = 0
