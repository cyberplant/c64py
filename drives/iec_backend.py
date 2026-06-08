"""
IECDriveBackend — abstract base class for IEC bus drive backends.

Both Drive1541 (local ROM emulation) and TcpDriveClient (remote TCP server)
implement this interface.  IECBus already calls these methods via duck-typing;
the ABC just makes the contract explicit and enables type-checking.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Tuple


class IECDriveBackend(ABC):
    """Abstract base for anything that can act as an IEC bus device."""

    device_number: int

    # ------------------------------------------------------------------
    # Bus-level callbacks (called by IECBus)
    # ------------------------------------------------------------------

    @abstractmethod
    def notify_bus_change(self) -> None:
        """Called whenever ATN/CLK/DATA state changes on the bus."""

    @abstractmethod
    def on_atn_changed(self, atn_state: bool) -> None:
        """Called when the ATN line changes.

        Args:
            atn_state: True = released (high), False = asserted (low)
        """

    # ------------------------------------------------------------------
    # Byte-level protocol callbacks (called by IECBus.send_command /
    # send_byte / receive_byte)
    # ------------------------------------------------------------------

    @abstractmethod
    def on_listen(self) -> None:
        """Drive has been selected as a listener."""

    @abstractmethod
    def on_unlisten(self) -> None:
        """UNLISTEN received."""

    @abstractmethod
    def on_talk(self) -> None:
        """Drive has been selected as a talker."""

    @abstractmethod
    def on_untalk(self) -> None:
        """UNTALK received."""

    @abstractmethod
    def on_secondary_address(self, channel: int) -> None:
        """Secondary address / channel selected."""

    @abstractmethod
    def iec_open_channel(self, channel: int) -> None:
        """OPEN sequence started on ``channel``; filename bytes follow."""

    @abstractmethod
    def iec_close_channel(self, channel: int) -> None:
        """CLOSE ``channel``."""

    @abstractmethod
    def iec_secondary(self, channel: int, kind: str) -> None:
        """0x60+ch secondary (data channel select)."""

    @abstractmethod
    def iec_unlisten(self) -> None:
        """UNLISTEN — finalise any in-flight OPEN."""

    @abstractmethod
    def iec_untalk(self) -> None:
        """UNTALK — talker disengaged."""

    @abstractmethod
    def iec_receive_byte(self, byte: int, eoi: bool = False) -> None:
        """Receive one byte from the C64."""

    @abstractmethod
    def iec_send_byte(self) -> Optional[Tuple[int, bool]]:
        """Return next ``(byte, eoi)`` for the active channel, or ``None``."""

    # ------------------------------------------------------------------
    # Emulator integration
    # ------------------------------------------------------------------

    @abstractmethod
    def step(self, cycles: int = 1) -> int:
        """Advance the backend by ``cycles`` host-clock cycles.

        Returns the number of cycles actually consumed.
        """
