"""IEC wire → logical IEC bus (workstreams A3a–A3c partial).

Observes resolved ``(ATN, CLK, DATA)`` triples (same convention as
:class:`~c64py.iec_bus.IECBus`: ``True`` = line released / high) on each bus
change after CIA2 port A is applied.

**ATN asserted** (``atn`` False): each falling edge on CLK samples DATA into an
8-bit shift register (LSB first; DATA high = serial ``0``). Eight bits invoke
:meth:`~c64py.iec_bus.IECBus.deliver_command` (A3a).

**ATN released** (``atn`` True) while the C64 is sending to a listener
(``listener`` set, ``talker`` is None, ``secondary_phase`` is ``open`` or
``data``): the same bit framing invokes :meth:`~c64py.iec_bus.IECBus.send_byte`
(A3b filename after OPEN; A3c partial for ``PRINT#`` / channel data). Completed
bytes are held briefly so the **last** byte before the next ATN-held command
(typically UNLISTEN) is sent with ``eoi=True``; earlier bytes use
``eoi=False``.

**Not implemented:** EOI inferred solely from inter-byte timing, ``TALK`` /
``INPUT#`` (drive as clock master), and full hardware listener ``CLK`` holds.
Enable with ``C64PY_IEC_WIRE_DECODE=1`` via :class:`~c64py.iec_kernal_bridge.KernalIecTap`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from .iec_bus import IECBus

Triple = Tuple[bool, bool, bool]  # (atn released?, clk high?, data high?)


class IecAtnWireDecoder:
    """Decode ATN command bytes and C64→listener data bytes from CLK/DATA edges."""

    __slots__ = (
        "_bus",
        "_bit_idx",
        "_pending_byte",
        "_shift",
        "bytes_sent",
        "commands_delivered",
    )

    def __init__(self, bus: "IECBus") -> None:
        self._bus = bus
        self._bit_idx = 0
        self._shift = 0
        self._pending_byte: Optional[int] = None
        self.commands_delivered: List[int] = []
        self.bytes_sent: List[Tuple[int, bool]] = []

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

    def feed_transition(self, prev: Triple, cur: Triple, _cyc: int = 0) -> None:
        """Handle one resolved-bus transition (previous → new triple)."""
        patn, pclk, pdata = prev
        catn, cclk, _cdata = cur

        if patn and not catn:
            self._flush_last_pending()
            self._reset_shift()
        elif not patn and catn:
            self._reset_shift()
            return

        if not patn and not catn and pclk and not cclk:
            self._sample_atn_command_bit(pdata)
            return

        if patn and catn and pclk and not cclk and self._data_phase_active():
            self._sample_data_phase_bit(pdata)
