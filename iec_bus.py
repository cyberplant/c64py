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

Protocol:
1. C64 asserts ATN (low)
2. C64 sends command byte (LISTEN/TALK/UNLISTEN/UNTALK)
3. C64 releases ATN (high)
4. Devices respond with data or receive data
"""

from __future__ import annotations

from typing import Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from .drive1541 import Drive1541


class IECBus:
    """IEC serial bus for communication between C64 and peripherals."""
    
    def __init__(self):
        """Initialize IEC bus with all lines released (high)."""
        # Bus lines (True = released/high, False = asserted/low)
        self.atn = True  # Attention line (C64 controls)
        self.clk = True  # Clock line (bidirectional)
        self.data = True  # Data line (bidirectional)
        
        # Track who's pulling each line low
        self.clk_pullers = set()  # Set of device IDs pulling CLK low
        self.data_pullers = set()  # Set of device IDs pulling DATA low
        
        # Devices on the bus
        self.devices: List[Drive1541] = []
        
        # Current talker/listener (device numbers)
        self.talker: Optional[int] = None
        self.listener: Optional[int] = None
        
        # EOI (End Of Indicator) flag
        self.eoi = False
        
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
        if self.atn != state:
            self.atn = state
            # Notify all devices of ATN change
            for device in self.devices:
                device.on_atn_changed(state)
                
    def set_clk(self, device_id: str, state: bool) -> None:
        """Set CLK line state from a specific device.
        
        Args:
            device_id: ID of device setting the line
            state: True = released (high), False = asserted (low)
        """
        if state:
            # Release - remove from pullers
            self.clk_pullers.discard(device_id)
        else:
            # Assert - add to pullers
            self.clk_pullers.add(device_id)
        
        # CLK is low if any device pulls it low
        self.clk = len(self.clk_pullers) == 0
        
    def set_data(self, device_id: str, state: bool) -> None:
        """Set DATA line state from a specific device.
        
        Args:
            device_id: ID of device setting the line
            state: True = released (high), False = asserted (low)
        """
        if state:
            # Release - remove from pullers
            self.data_pullers.discard(device_id)
        else:
            # Assert - add to pullers
            self.data_pullers.add(device_id)
        
        # DATA is low if any device pulls it low
        self.data = len(self.data_pullers) == 0
        
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
        
    def send_byte(self, byte: int, eoi: bool = False) -> bool:
        """Send a byte from C64 to listener device.
        
        This implements the C64 side of the IEC protocol for sending data.
        
        Args:
            byte: Byte to send (0-255)
            eoi: True if this is the last byte (End Of Indicator)
            
        Returns:
            True if byte was acknowledged, False on error
        """
        # This will be implemented with proper timing
        # For now, just a placeholder
        return True
        
    def receive_byte(self) -> Optional[int]:
        """Receive a byte from talker device to C64.
        
        This implements the C64 side of the IEC protocol for receiving data.
        
        Returns:
            Byte received (0-255), or None on error/timeout
        """
        # This will be implemented with proper timing
        # For now, just a placeholder
        return None
        
    def send_command(self, command: int) -> bool:
        """Send a command byte with ATN asserted.
        
        Commands:
        - 0x20-0x3F: LISTEN (device = command & 0x1F)
        - 0x40-0x5F: TALK (device = command & 0x1F)
        - 0x60-0x6F: Secondary address after LISTEN
        - 0xE0-0xEF: Secondary address after TALK  
        - 0x3F: UNLISTEN
        - 0x5F: UNTALK
        
        Args:
            command: Command byte to send
            
        Returns:
            True if command was sent successfully
        """
        # Assert ATN
        self.set_atn(False)
        
        # Parse command
        if 0x20 <= command <= 0x3E:
            # LISTEN command
            device_num = command & 0x1F
            self.listener = device_num
            self.talker = None
            # Notify device
            for device in self.devices:
                if device.device_number == device_num:
                    device.on_listen()
                    
        elif command == 0x3F:
            # UNLISTEN
            if self.listener is not None:
                for device in self.devices:
                    if device.device_number == self.listener:
                        device.on_unlisten()
            self.listener = None
            
        elif 0x40 <= command <= 0x5E:
            # TALK command
            device_num = command & 0x1F
            self.talker = device_num
            self.listener = None
            # Notify device
            for device in self.devices:
                if device.device_number == device_num:
                    device.on_talk()
                    
        elif command == 0x5F:
            # UNTALK
            if self.talker is not None:
                for device in self.devices:
                    if device.device_number == self.talker:
                        device.on_untalk()
            self.talker = None
            
        elif 0x60 <= command <= 0x6F:
            # Secondary address after LISTEN
            channel = command & 0x0F
            if self.listener is not None:
                for device in self.devices:
                    if device.device_number == self.listener:
                        device.on_secondary_address(channel)
                        
        elif 0xE0 <= command <= 0xEF:
            # Secondary address after TALK
            channel = command & 0x0F
            if self.talker is not None:
                for device in self.devices:
                    if device.device_number == self.talker:
                        device.on_secondary_address(channel)
        
        # Release ATN
        self.set_atn(True)
        
        return True
        
    def reset(self) -> None:
        """Reset the bus to initial state."""
        self.atn = True
        self.clk = True
        self.data = True
        self.clk_pullers.clear()
        self.data_pullers.clear()
        self.talker = None
        self.listener = None
        self.eoi = False
