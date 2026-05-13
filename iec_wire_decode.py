"""IEC wire → logical IEC bus (workstreams A3a–A3c partial).

Observes resolved ``(ATN, CLK, DATA)`` triples (same convention as
:class:`~c64py.iec_bus.IECBus`: ``True`` = line released / high) on each bus
change after CIA2 port A is applied **or** when peripherals toggle CLK/DATA
(see :attr:`~c64py.iec_bus.IECBus.iec_line_receiver`).

**ATN asserted** (``atn`` False): each falling edge on CLK samples DATA into an
8-bit shift register (LSB first; DATA high = serial ``0``). Eight bits invoke
:meth:`~c64py.iec_bus.IECBus.deliver_command` (A3a).

**ATN released** (``atn`` True) while the C64 is sending to a listener
(``listener`` set, ``talker`` is None, ``secondary_phase`` is ``open`` or
``data``): each falling CLK edge while ATN stays released invokes
:meth:`~c64py.iec_bus.IECBus.send_byte` (A3b filename after OPEN; A3c partial for
``PRINT#`` / channel data). A CLK fall on the **same** CIA2 update that releases
ATN (leaving the ATN-held secondary command) is counted too, so the first payload
bit is not lost. Completed bytes are held briefly so the **last** byte before the
next ATN-held command (typically UNLISTEN) is sent with ``eoi=True``; earlier bytes
use ``eoi=False``.

**TALK / INPUT# (experimental):** when ``talk_pull_receive`` is true (env
``C64PY_IEC_WIRE_DECODE_TALK`` via :class:`~c64py.iec_kernal_bridge.KernalIecTap`),
during ``TALK`` data phase the same CLK sampling calls
:meth:`~c64py.iec_bus.IECBus.receive_byte` after each assembled byte. This can
prefetch TCP drive replies and is **off by default**.

**Not implemented:** EOI inferred solely from inter-byte timing, full hardware
listener ``CLK`` holds.

Enable wire decode with ``C64PY_IEC_WIRE_DECODE=1`` via
:class:`~c64py.iec_kernal_bridge.KernalIecTap`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional, Tuple

if TYPE_CHECKING:
    from .iec_bus import IECBus

Triple = Tuple[bool, bool, bool]  # (atn released?, clk high?, data high?)


class IecAtnWireDecoder:
    """Decode ATN command bytes and C64↔listener data bytes from CLK/DATA edges."""

    __slots__ = (
        "_bus",
        "_bit_idx",
        "_pending_byte",
        "_shift",
        "_talk_pull_receive",
        "bytes_sent",
        "commands_delivered",
        "talk_logical_reads",
    )

    def __init__(self, bus: "IECBus", *, talk_pull_receive: bool = False) -> None:
        self._bus = bus
        self._bit_idx = 0
        self._shift = 0
        self._pending_byte: Optional[int] = None
        self._talk_pull_receive = bool(talk_pull_receive)
        self.commands_delivered: List[int] = []
        self.bytes_sent: List[Tuple[int, bool]] = []
        self.talk_logical_reads: List[Any] = []

    def reset(self) -> None:
        self._bit_idx = 0
        self._shift = 0
        self._pending_byte = None

    def _reset_shift(self) -> None:
        self._bit_idx = 0
        self._shift = 0

    def _flush_last_pending(self) -> None:
        if self._pending_byte is None:
            return
        if (
            self._bus.listener is not None
            and self._bus.secondary_phase in ("open", "data")
        ):
            self._bus.send_byte(self._pending_byte & 0xFF, eoi=True)
            self.bytes_sent.append((self._pending_byte & 0xFF, True))
        self._pending_byte = None

    def _data_phase_active(self) -> bool:
        b = self._bus
        return (
            b.listener is not None
            and b.talker is None
            and b.secondary_phase in ("open", "data")
        )

    def _talk_receive_active(self) -> bool:
        b = self._bus
        return (
            self._talk_pull_receive
            and b.talker is not None
            and b.listener is None
            and b.secondary_phase == "data"
        )

    def _sample_atn_command_bit(self, pdata: bool) -> None:
        wire_bit = 0 if pdata else 1
        self._shift |= wire_bit << self._bit_idx
        self._bit_idx += 1
        if self._bit_idx != 8:
            return
        completed = self._shift & 0xFF
        self._reset_shift()
        self._bus.deliver_command(completed)
        self.commands_delivered.append(completed)

    def _sample_data_phase_bit(self, pdata: bool) -> None:
        wire_bit = 0 if pdata else 1
        self._shift |= wire_bit << self._bit_idx
        self._bit_idx += 1
        if self._bit_idx != 8:
            return
        completed = self._shift & 0xFF
        self._reset_shift()
        if self._pending_byte is not None:
            self._bus.send_byte(self._pending_byte & 0xFF, eoi=False)
            self.bytes_sent.append((self._pending_byte & 0xFF, False))
        self._pending_byte = completed

    def _sample_talk_data_bit(self, pdata: bool) -> None:
        wire_bit = 0 if pdata else 1
        self._shift |= wire_bit << self._bit_idx
        self._bit_idx += 1
        if self._bit_idx != 8:
            return
        self._reset_shift()
        result = self._bus.receive_byte()
        self.talk_logical_reads.append(result)

    def feed_transition(self, prev: Triple, cur: Triple, _cyc: int = 0) -> None:
        """Handle one resolved-bus transition (previous → new triple)."""
        patn, pclk, pdata = prev
        catn, cclk, cdata = cur

        if patn and not catn:
            self._flush_last_pending()
            self._reset_shift()
        elif not patn and catn:
            # ATN released (end of ATN-held command bytes). Do not return yet:
            # KERNAL often releases ATN and clocks the first data-phase bit in one
            # CIA2 port A write; that combined transition must still sample DATA.
            self._reset_shift()

        if not patn and not catn and pclk and not cclk:
            self._sample_atn_command_bit(pdata)
            return

        # Data / TALK sampling: ATN must be released in *cur* (open-collector idle).
        if catn and pclk and not cclk:
            if self._data_phase_active():
                # When ATN is released in the same transition as this CLK fall, ``prev``
                # still reflects the last ATN-held command bit on DATA; the payload bit is
                # already visible in ``cur``.
                dsamp = cdata if not patn else pdata
                self._sample_data_phase_bit(dsamp)
            elif self._talk_receive_active():
                self._sample_talk_data_bit(pdata)
