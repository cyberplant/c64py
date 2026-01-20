"""
Commodore 1541 disk drive emulation.

The 1541 is a full computer with:
- 6502 CPU @ 1MHz
- 2KB RAM
- 16KB ROM (DOS + Serial/GCR routines)
- VIA chips for I/O (6522)
- IEC serial bus interface

This implementation emulates the drive at a high enough level to:
1. Respond to IEC bus commands
2. Execute ROM code for disk operations
3. Read/write D64 disk images
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from .cpu import CPU6502
from .cpu_state import CPUState

if TYPE_CHECKING:
    from .iec_bus import IECBus
    from .d64 import D64Image


class Drive1541Memory:
    """Memory map for 1541 drive (64KB address space)."""
    
    def __init__(self):
        """Initialize 1541 memory."""
        # 2KB RAM (repeated 4 times in $0000-$07FF range)
        self.ram = bytearray(0x0800)
        
        # ROM areas (loaded from ROM files)
        self.rom_dos = None  # 16KB at $C000-$FFFF
        self.rom_serial = None  # 8KB at $8000-$9FFF (not always present)
        
        # VIA chip registers (simplified)
        self.via1_registers = bytearray(16)  # $1800-$180F
        self.via2_registers = bytearray(16)  # $1C00-$1C0F
        
    def load_rom(self, dos_rom: bytes, serial_rom: Optional[bytes] = None) -> None:
        """Load ROM images.
        
        Args:
            dos_rom: DOS ROM (16KB)
            serial_rom: Serial ROM (8KB, optional)
        """
        if len(dos_rom) != 16384:
            raise ValueError(f"DOS ROM must be 16KB, got {len(dos_rom)} bytes")
        self.rom_dos = bytes(dos_rom)
        
        if serial_rom is not None:
            if len(serial_rom) != 8192:
                raise ValueError(f"Serial ROM must be 8KB, got {len(serial_rom)} bytes")
            self.rom_serial = bytes(serial_rom)
            
    def read(self, addr: int) -> int:
        """Read byte from memory.
        
        Args:
            addr: Address to read from (0x0000-0xFFFF)
            
        Returns:
            Byte value (0-255)
        """
        addr = addr & 0xFFFF
        
        # RAM: $0000-$07FF (repeated in $0800-$0FFF, $1000-$17FF, $1800-$1FFF)
        if addr < 0x0800:
            return self.ram[addr]
        elif addr < 0x1000:
            return self.ram[addr & 0x07FF]
        elif addr < 0x1800:
            return self.ram[addr & 0x07FF]
            
        # VIA #1: $1800-$180F
        elif 0x1800 <= addr < 0x1810:
            return self.via1_registers[addr & 0x0F]
            
        # VIA #2: $1C00-$1C0F
        elif 0x1C00 <= addr < 0x1C10:
            return self.via2_registers[addr & 0x0F]
            
        # Serial ROM: $8000-$9FFF (if loaded)
        elif 0x8000 <= addr < 0xA000:
            if self.rom_serial is not None:
                return self.rom_serial[addr - 0x8000]
            return 0xFF
            
        # DOS ROM: $C000-$FFFF
        elif addr >= 0xC000:
            if self.rom_dos is not None:
                return self.rom_dos[addr - 0xC000]
            return 0xFF
            
        # Unmapped areas
        return 0xFF
        
    def write(self, addr: int, value: int) -> None:
        """Write byte to memory.
        
        Args:
            addr: Address to write to (0x0000-0xFFFF)
            value: Byte value to write (0-255)
        """
        addr = addr & 0xFFFF
        value = value & 0xFF
        
        # RAM: $0000-$07FF (repeated in other banks)
        if addr < 0x0800:
            self.ram[addr] = value
        elif addr < 0x1000:
            self.ram[addr & 0x07FF] = value
        elif addr < 0x1800:
            self.ram[addr & 0x07FF] = value
            
        # VIA #1: $1800-$180F
        elif 0x1800 <= addr < 0x1810:
            self.via1_registers[addr & 0x0F] = value
            
        # VIA #2: $1C00-$1C0F
        elif 0x1C00 <= addr < 0x1C10:
            self.via2_registers[addr & 0x0F] = value
            
        # ROM areas are not writable (ignored)


class Drive1541:
    """Emulates a Commodore 1541 disk drive."""
    
    def __init__(self, device_number: int = 8):
        """Initialize 1541 drive.
        
        Args:
            device_number: IEC bus device number (typically 8-11)
        """
        self.device_number = device_number
        
        # Drive memory and CPU
        self.memory = Drive1541Memory()
        self.cpu = CPU6502(self.memory)
        
        # IEC bus (set when attached to bus)
        self.iec_bus: Optional[IECBus] = None
        
        # Disk image
        self.disk: Optional[D64Image] = None
        self.disk_filename: Optional[str] = None
        
        # Drive state
        self.listening = False
        self.talking = False
        self.current_channel = 0
        
        # Buffer for command channel (channel 15)
        self.command_buffer = bytearray()
        
    def load_rom(self, dos_rom: bytes, serial_rom: Optional[bytes] = None) -> None:
        """Load 1541 ROM files.
        
        Args:
            dos_rom: DOS ROM data (16KB)
            serial_rom: Serial ROM data (8KB, optional)
        """
        self.memory.load_rom(dos_rom, serial_rom)
        # Reset CPU to ROM start
        self.cpu.state.pc = self.memory.read(0xFFFC) | (self.memory.read(0xFFFD) << 8)
        
    def attach_disk(self, disk: D64Image, filename: str = "") -> None:
        """Attach a D64 disk image.
        
        Args:
            disk: D64 disk image
            filename: Original filename (for reference)
        """
        self.disk = disk
        self.disk_filename = filename
        
    def detach_disk(self) -> None:
        """Detach the current disk image."""
        self.disk = None
        self.disk_filename = None
        
    def has_disk(self) -> bool:
        """Check if a disk is attached.
        
        Returns:
            True if disk is attached
        """
        return self.disk is not None
        
    def step(self, cycles: int = 1) -> int:
        """Execute one or more CPU cycles.
        
        Args:
            cycles: Number of cycles to execute
            
        Returns:
            Actual cycles executed
        """
        if self.memory.rom_dos is None:
            return 0
            
        total_cycles = 0
        for _ in range(cycles):
            total_cycles += self.cpu.step()
            
        return total_cycles
        
    # IEC bus event handlers
    
    def on_atn_changed(self, atn_state: bool) -> None:
        """Called when ATN line changes.
        
        Args:
            atn_state: True if ATN released (high), False if asserted (low)
        """
        # ATN assertion indicates command follows
        # This would trigger an interrupt in real hardware
        pass
        
    def on_listen(self) -> None:
        """Called when this device is commanded to LISTEN."""
        self.listening = True
        self.talking = False
        
    def on_unlisten(self) -> None:
        """Called when UNLISTEN command is received."""
        self.listening = False
        
    def on_talk(self) -> None:
        """Called when this device is commanded to TALK."""
        self.talking = True
        self.listening = False
        
    def on_untalk(self) -> None:
        """Called when UNTALK command is received."""
        self.talking = False
        
    def on_secondary_address(self, channel: int) -> None:
        """Called when a secondary address (channel) is specified.
        
        Args:
            channel: Channel number (0-15)
        """
        self.current_channel = channel
        
    def receive_byte(self, byte: int) -> None:
        """Receive a byte from the IEC bus.
        
        Called when device is listening and C64 sends data.
        
        Args:
            byte: Byte received (0-255)
        """
        if self.current_channel == 15:
            # Command channel
            self.command_buffer.append(byte)
        # Other channels would handle file data
        
    def send_byte(self) -> Optional[int]:
        """Send a byte to the IEC bus.
        
        Called when device is talking and C64 requests data.
        
        Returns:
            Byte to send (0-255), or None if no data
        """
        # This would read from the current file/channel
        return None
