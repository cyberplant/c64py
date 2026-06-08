"""
C64 Emulator Package
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__version__ = "1.0.1"

__all__ = [
    "C64",
    "CPU6502",
    "MemoryMap",
    "CPUState",
    "CIATimer",
    "UdpDebugLogger",
    "TextualInterface",
    "EmulatorServer",
]

if TYPE_CHECKING:
    from .cpu import CPU6502
    from .cpu_state import CPUState, CIATimer
    from .debug import UdpDebugLogger
    from .emulator import C64
    from .server import EmulatorServer
    from .ui import TextualInterface


def __getattr__(name: str):
    if name == "C64":
        from .emulator import C64

        return C64
    if name == "CPU6502":
        from .cpu import CPU6502

        return CPU6502
    if name == "MemoryMap":
        from .memory import MemoryMap

        return MemoryMap
    if name == "CPUState":
        from .cpu_state import CPUState

        return CPUState
    if name == "CIATimer":
        from .cpu_state import CIATimer

        return CIATimer
    if name == "UdpDebugLogger":
        from .debug import UdpDebugLogger

        return UdpDebugLogger
    if name == "TextualInterface":
        from .ui import TextualInterface

        return TextualInterface
    if name == "EmulatorServer":
        from .server import EmulatorServer

        return EmulatorServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
