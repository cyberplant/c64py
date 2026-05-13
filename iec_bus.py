"""
IEC Serial Bus emulation for Commodore 64.

The IEC (IEEE-488 derivative) serial bus is used for communication between
the C64 and peripherals like the 1541 disk drive.

Bus signals:
- ATN (Attention): C64 pulls low to signal start of command
- CLK (Clock): Bidirectional clock line
- DATA: Bidirectional data line
- RESET: Reset signal (not implemented in C64, only in drives)
- SRQ (Service Request): Not used in C64

Bit-level handshake (not modelled here):
1. C64 asserts ATN (low)
2. C64 sends command byte (LISTEN/TALK/UNLISTEN/UNTALK)
3. C64 releases ATN (high)
4. Devices respond with data or receive data

Logical (byte-level) protocol — modelled by ``send_command``/``send_byte``/
``receive_byte``:

    Phase tracking (``current_listener`` / ``current_talker`` /
    ``current_secondary`` / ``secondary_phase``)::

        idle  --LISTEN(0x20+dev)-->  listener=dev  secondary_phase=idle
              --TALK  (0x40+dev)-->  talker  =dev

        After LISTEN, secondary byte (still under ATN):
            0x60+ch (DATA)   -> secondary=ch, phase=data
            0xE0+ch (CLOSE)  -> secondary=ch, phase=close, fires close_channel
            0xF0+ch (OPEN)   -> secondary=ch, phase=open  (filename bytes follow
                                until UNLISTEN; UNLISTEN triggers open_channel)

        After TALK, secondary byte:
            0x60+ch          -> secondary=ch, phase=data (talker pumps bytes)
            0xE0+ch          -> close
            0xF0+ch          -> reopen (treat as open)

        UNLISTEN (0x3F): finalize any in-flight open (filename complete) then
            clears listener state, secondary phase resets, eoi_pending cleared.
        UNTALK   (0x5F): clears talker state, eoi_pending cleared.

    Data phase:
        ``send_byte(byte, eoi)``  routes to listener via
        ``Drive1541.iec_receive_byte(byte, eoi)``.
        ``receive_byte()`` pulls from talker via
        ``Drive1541.iec_send_byte() -> (byte, eoi) | None``; ``eoi_pending``
        latches the last EOI seen.

    The legacy ``on_listen``/``on_unlisten``/``on_talk``/``on_untalk``/
    ``on_secondary_address``/``receive_byte``/``send_byte`` device hooks remain
    invoked for back-compat with simple test devices.
"""

from __future__ import annotations

from typing import Optional, List, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .drives.c1541_emulator import Drive1541

Triple = Tuple[bool, bool, bool]


class IECBus:
    """IEC serial bus for communication between C64 and peripherals."""
    
    def __init__(self):
        """Initialize IEC bus with all lines released (high)."""
        # Bus lines (True = released/high, False = asserted/low)
        self.atn = True  # Attention line (C64 controls)
        self.clk = True  # Clock line (bidirectional)
        self.data = True  # Data line (bidirectional)
        # When True, :meth:`set_atn` / :meth:`set_clk` / :meth:`set_data` skip
        # :attr:`iec_line_receiver` (used to batch CIA2 port A into one tap event).
        self.iec_line_events_suppressed: bool = False
        # Optional tap / decoder implementing ``on_iec_lines(before, after, cyc=0)``.
        self.iec_line_receiver: Optional[object] = None
        
        # Track who's pulling each line low
        self.clk_pullers = set()  # Set of device IDs pulling CLK low
        self.data_pullers = set()  # Set of device IDs pulling DATA low
        
        # Devices on the bus
        self.devices: List[Drive1541] = []
        
        # Current talker/listener (device numbers)
        self.talker: Optional[int] = None
        self.listener: Optional[int] = None

        # Byte-level protocol phase (see module docstring).
        self.current_listener: Optional[int] = None
        self.current_talker: Optional[int] = None
        self.current_secondary: Optional[int] = None
        # 'idle' | 'open' | 'data' | 'close' | 'reopen'
        self.secondary_phase: str = "idle"

        # EOI (End Of Indicator) flag — last byte seen on the bus.
        self.eoi = False
        # Latches an EOI-on-receive so callers can poll after receive_byte().
        self.eoi_pending = False

    def line_triple(self) -> Triple:
        """Resolved ``(ATN, CLK, DATA)`` with ``True`` = released / high."""
        return (bool(self.atn), bool(self.clk), bool(self.data))

    def _emit_iec_line_change_if_needed(self, before: Triple, after: Triple, cyc: int = 0) -> None:
        if before == after or self.iec_line_events_suppressed:
            return
        recv = self.iec_line_receiver
        if recv is not None and hasattr(recv, "on_iec_lines"):
            recv.on_iec_lines(before, after, cyc)
        
    def attach_device(self, device: Drive1541) -> None:
        """Attach a device to the bus.
        
        Args:
            device: Device to attach (e.g., 1541 drive)
        """
        self.devices.append(device)
        device.iec_bus = self
        
    def detach_device(self, device: Drive1541) -> None:
        """Detach a device from the bus.
        
        Args:
            device: Device to detach
        """
        if device in self.devices:
            self.devices.remove(device)
            device.iec_bus = None
            
    def set_atn(self, state: bool) -> None:
        """Set ATN line state (C64 only).
        
        Args:
            state: True = released (high), False = asserted (low)
        """
        before = self.line_triple()
        if self.atn != state:
            self.atn = state
            # Notify all devices of ATN change
            for device in self.devices:
                device.on_atn_changed(state)
                if hasattr(device, "notify_bus_change"):
                    device.notify_bus_change()
        after = self.line_triple()
        self._emit_iec_line_change_if_needed(before, after, 0)
                
    def set_clk(self, device_id: str, state: bool) -> None:
        """Set CLK line state from a specific device.
        
        Args:
            device_id: ID of device setting the line
            state: True = released (high), False = asserted (low)
        """
        before = self.line_triple()
        prev = self.clk
        if state:
            # Release - remove from pullers
            self.clk_pullers.discard(device_id)
        else:
            # Assert - add to pullers
            self.clk_pullers.add(device_id)
        
        # CLK is low if any device pulls it low
        self.clk = len(self.clk_pullers) == 0
        if self.clk != prev:
            for device in self.devices:
                if hasattr(device, "notify_bus_change"):
                    device.notify_bus_change()
        after = self.line_triple()
        self._emit_iec_line_change_if_needed(before, after, 0)
        
    def set_data(self, device_id: str, state: bool) -> None:
        """Set DATA line state from a specific device.
        
        Args:
            device_id: ID of device setting the line
            state: True = released (high), False = asserted (low)
        """
        before = self.line_triple()
        prev = self.data
        if state:
            # Release - remove from pullers
            self.data_pullers.discard(device_id)
        else:
            # Assert - add to pullers
            self.data_pullers.add(device_id)
        
        # DATA is low if any device pulls it low
        self.data = len(self.data_pullers) == 0
        if self.data != prev:
            for device in self.devices:
                if hasattr(device, "notify_bus_change"):
                    device.notify_bus_change()
        after = self.line_triple()
        self._emit_iec_line_change_if_needed(before, after, 0)
        
    def get_clk(self) -> bool:
        """Get current CLK line state.
        
        Returns:
            True if CLK is high (released), False if low (asserted)
        """
        return self.clk
        
    def get_data(self) -> bool:
        """Get current DATA line state.
        
        Returns:
            True if DATA is high (released), False if low (asserted)
        """
        return self.data

    def peer_clk_high(self) -> bool:
        """True if CLK is high when ignoring only the C64's contribution.

        Used to merge CIA2 port A reads in the Rust fast path: the C64 pulls CLK
        low when bit 4 of PR is 0 (released=False in ``set_clk`` terms).
        """
        pullers = self.clk_pullers - {"c64"}
        return len(pullers) == 0

    def peer_data_high(self) -> bool:
        """True if DATA is high when ignoring only the C64's contribution."""
        pullers = self.data_pullers - {"c64"}
        return len(pullers) == 0
        
    def _find_device(self, device_num: Optional[int]):
        if device_num is None:
            return None
        for device in self.devices:
            if device.device_number == device_num:
                return device
        return None

    def send_byte(self, byte: int, eoi: bool = False) -> bool:
        """Deliver a data byte from the C64 side to the current LISTEN device.

        Full IEC bit-level timing is not modeled; this hooks the logical layer for
        tests and future KERNAL integration.

        Routing:
        - If the listener device exposes ``iec_receive_byte(byte, eoi)``, call
          it (preferred path). It is responsible for filename accumulation
          during the OPEN phase and channel routing during the DATA phase.
        - Otherwise fall back to the legacy ``receive_byte(byte)`` hook.
        """
        self.eoi = bool(eoi)
        if self.listener is None:
            return False
        device = self._find_device(self.listener)
        if device is None:
            return False
        if hasattr(device, "iec_receive_byte"):
            device.iec_receive_byte(byte & 0xFF, eoi=bool(eoi))
        else:
            device.receive_byte(byte & 0xFF)
        return True

    def receive_byte(self):
        """Pull one byte from the current TALK device, if any.

        Returns:
            ``None`` if no talker / no data, an ``int`` (0..255) if the device
            only exposes the legacy ``send_byte()``, or a ``(byte, eoi)`` tuple
            if it exposes ``iec_send_byte()``. ``eoi_pending`` is updated to
            mirror the latched EOI.
        """
        if self.talker is None:
            return None
        device = self._find_device(self.talker)
        if device is None:
            return None
        if hasattr(device, "iec_send_byte"):
            result = device.iec_send_byte()
            if result is None:
                return None
            byte, eoi = result
            self.eoi = bool(eoi)
            self.eoi_pending = bool(eoi)
            return (byte & 0xFF, bool(eoi))
        # Legacy device: returns just an int.
        return device.send_byte()
        
    def deliver_command(self, command: int) -> bool:
        """Apply a logical IEC command byte **without** toggling ATN on the bus.

        Used when a KERNAL wire decoder has already asserted ATN on the CIA2
        lines and only the listener/talker/secondary state machine must run.
        :meth:`send_command` wraps this with ``set_atn(False)`` / ``set_atn(True)``
        for programmatic / test callers.
        """
        handled = True

        if 0x20 <= command <= 0x3E:
            # LISTEN dev
            device_num = command & 0x1F
            self.listener = device_num
            self.current_listener = device_num
            self.talker = None
            self.current_talker = None
            self.current_secondary = None
            self.secondary_phase = "idle"
            self.eoi_pending = False
            device = self._find_device(device_num)
            if device is not None:
                device.on_listen()

        elif command == 0x3F:
            # UNLISTEN
            device = self._find_device(self.current_listener)
            if device is not None:
                # If we were mid-OPEN, finalize the filename.
                if self.secondary_phase == "open" and hasattr(device, "iec_unlisten"):
                    device.iec_unlisten()
                else:
                    if hasattr(device, "iec_unlisten"):
                        device.iec_unlisten()
                device.on_unlisten()
            self.listener = None
            self.current_listener = None
            self.current_secondary = None
            self.secondary_phase = "idle"
            self.eoi_pending = False

        elif 0x40 <= command <= 0x5E:
            # TALK dev
            device_num = command & 0x1F
            self.talker = device_num
            self.current_talker = device_num
            self.listener = None
            self.current_listener = None
            self.current_secondary = None
            self.secondary_phase = "idle"
            self.eoi_pending = False
            device = self._find_device(device_num)
            if device is not None:
                device.on_talk()

        elif command == 0x5F:
            # UNTALK
            device = self._find_device(self.current_talker)
            if device is not None:
                if hasattr(device, "iec_untalk"):
                    device.iec_untalk()
                device.on_untalk()
            self.talker = None
            self.current_talker = None
            self.current_secondary = None
            self.secondary_phase = "idle"
            self.eoi_pending = False

        elif 0x60 <= command <= 0x7F:
            # Secondary after LISTEN or TALK.
            channel = command & 0x0F
            high = command & 0xF0
            self.current_secondary = channel
            if high == 0x60 or high == 0x70:
                self.secondary_phase = "data"
            target = self.current_listener if self.current_listener is not None else self.current_talker
            device = self._find_device(target)
            if device is not None:
                device.on_secondary_address(channel)
                if hasattr(device, "iec_secondary"):
                    device.iec_secondary(channel, "data")

        elif 0xE0 <= command <= 0xEF:
            # CLOSE channel ch (after LISTEN — also accepted after TALK).
            channel = command & 0x0F
            self.current_secondary = channel
            self.secondary_phase = "close"
            target = self.current_listener if self.current_listener is not None else self.current_talker
            device = self._find_device(target)
            if device is not None:
                device.on_secondary_address(channel)
                if hasattr(device, "iec_close_channel"):
                    device.iec_close_channel(channel)

        elif 0xF0 <= command <= 0xFF:
            # OPEN channel ch — filename bytes follow as data until UNLISTEN.
            channel = command & 0x0F
            self.current_secondary = channel
            self.secondary_phase = "open"
            target = self.current_listener if self.current_listener is not None else self.current_talker
            device = self._find_device(target)
            if device is not None:
                device.on_secondary_address(channel)
                if hasattr(device, "iec_open_channel"):
                    device.iec_open_channel(channel)

        else:
            handled = False

        return handled

    def send_command(self, command: int) -> bool:
        """Send a command byte with ATN asserted.

        Primary commands:
            0x20 + dev (0..30)  LISTEN device
            0x3F                UNLISTEN
            0x40 + dev (0..30)  TALK device
            0x5F                UNTALK

        Secondary commands (interpreted relative to the current LISTEN/TALK):
            0x60 + ch  open data on channel ch (assumes file already open)
            0x70 + ch  reserved (treated like 0x60+ch for safety)
            0xE0 + ch  close channel ch
            0xF0 + ch  open new file on channel ch — filename bytes follow
                       (until UNLISTEN) and are streamed to the listener via
                       send_byte/iec_receive_byte.

        Returns:
            True if the command was recognised and dispatched.
        """
        self.set_atn(False)
        handled = self.deliver_command(command)
        self.set_atn(True)
        return handled

    def unlisten(self) -> bool:
        """Convenience wrapper for ``send_command(0x3F)``."""
        return self.send_command(0x3F)

    def untalk(self) -> bool:
        """Convenience wrapper for ``send_command(0x5F)``."""
        return self.send_command(0x5F)

    def open_channel(self, channel: int, filename: Optional[bytes]) -> bool:
        """High-level helper: OPEN ``channel`` on current listener.

        If ``filename`` is None this just sends 0xF0+ch (caller will follow
        with send_byte). If a bytes-like is given, the bytes are streamed to
        the listener with EOI on the last one and UNLISTEN is sent.
        """
        if self.current_listener is None:
            return False
        if not 0 <= channel <= 15:
            return False
        ok = self.send_command(0xF0 | channel)
        if filename is not None and ok:
            data = bytes(filename)
            if data:
                for i, b in enumerate(data):
                    self.send_byte(b, eoi=(i == len(data) - 1))
            self.unlisten()
        return ok

    def close_channel(self, channel: int) -> bool:
        """High-level helper: CLOSE ``channel`` on current listener/talker."""
        if not 0 <= channel <= 15:
            return False
        return self.send_command(0xE0 | channel)
        
    def reset(self) -> None:
        """Reset the bus to initial state."""
        self.atn = True
        self.clk = True
        self.data = True
        self.clk_pullers.clear()
        self.data_pullers.clear()
        self.talker = None
        self.listener = None
        self.current_listener = None
        self.current_talker = None
        self.current_secondary = None
        self.secondary_phase = "idle"
        self.eoi = False
        self.eoi_pending = False
