"""IEC wire → logical command decoder (workstream A3a).

Observes resolved ``(ATN, CLK, DATA)`` triples (same convention as
:class:`~c64py.iec_bus.IECBus`: ``True`` = line released / high) on each **bus
change** after CIA2 port A is applied. While ATN is asserted (``atn is False``),
each **falling edge** on CLK samples DATA into an 8-bit shift register (LSB
first; DATA released/high encodes serial ``0``, asserted/low encodes serial
``1`` — Commodore open-collector sense).

After eight sampled bits, the assembled byte is passed to
:meth:`~c64py.iec_bus.IECBus.deliver_command` so devices such as
:class:`~c64py.drives.tcp_drive_client.TcpDriveClient` see the same logical
hooks as programmatic :meth:`~c64py.iec_bus.IECBus.send_command` callers,
without the decoder fighting KERNAL by toggling ATN.

This is a **minimal** first pass: it does not model listener ``CLK`` holds,
EOI timing in the data phase, or ``INPUT#`` / ``TALK`` bit streams — only ATN
command bytes (A3a). Enable alongside the KERNAL tap via
``C64PY_IEC_WIRE_DECODE=1`` (see :class:`~c64py.iec_kernal_bridge.KernalIecTap`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    from .iec_bus import IECBus

Triple = Tuple[bool, bool, bool]  # (atn released?, clk high?, data high?)


class IecAtnWireDecoder:
    """Decode ATN-phase command bytes from CLK/DATA transitions."""

    __slots__ = ("_bus", "_bit_idx", "_shift", "commands_delivered")

    def __init__(self, bus: "IECBus") -> None:
        self._bus = bus
        self._bit_idx = 0
        self._shift = 0
        self.commands_delivered: List[int] = []

    def reset(self) -> None:
        self._bit_idx = 0
        self._shift = 0

    def feed_transition(self, prev: Triple, cur: Triple) -> None:
        """Handle one resolved-bus transition (previous → new triple)."""
        patn, pclk, pdata = prev
        catn, cclk, _cdata = cur

        if patn and not catn:
            self.reset()
        elif not patn and catn:
            self.reset()
            return

        if not patn and pclk and not cclk:
            wire_bit = 0 if pdata else 1
            self._shift |= wire_bit << self._bit_idx
            self._bit_idx += 1
            if self._bit_idx == 8:
                cmd = self._shift & 0xFF
                self._bus.deliver_command(cmd)
                self.commands_delivered.append(cmd)
                self.reset()
