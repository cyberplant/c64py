"""
Commodore 1541 disk drive emulation.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .d64 import D64Image


class DiskDrive:
    """Emulates a Commodore 1541 disk drive.
    
    This is a simplified emulation that provides basic disk operations:
    - Loading files
    - Directory listing
    - File operations via command channel
    """
    
    def __init__(self, device_number: int = 8):
        """Initialize disk drive.
        
        Args:
            device_number: Drive device number (typically 8-11)
        """
        self.device_number = device_number
        self.disk: Optional[D64Image] = None
        self.disk_filename: Optional[str] = None
    
    def attach_disk(self, disk: D64Image, filename: str = "") -> None:
        """Attach a D64 disk image to this drive.
        
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
        """Check if a disk is attached."""
        return self.disk is not None
    
    def load_file(self, filename: str, secondary_address: int = 0) -> Optional[bytes]:
        """Load a file from the attached disk.
        
        Args:
            filename: File to load (use "$" for directory)
            secondary_address: Secondary address (0 for load, 1 for verify)
            
        Returns:
            File data as bytes, or None if file not found
        """
        if not self.has_disk():
            return None
        
        # Special case: "$" loads directory
        if filename == "$":
            return self._load_directory()
        
        # Find file in directory
        entries = self.disk.read_directory()
        
        # Clean up filename for comparison (remove quotes, spaces)
        clean_filename = filename.strip().upper().strip('"')
        
        for entry in entries:
            if entry.filename.upper() == clean_filename:
                # Load file data
                file_data = self.disk.read_file(entry)
                
                # For PRG files, prepend load address
                if entry.filetype == 2:  # PRG
                    # First 2 bytes are load address (stored in file)
                    return file_data
                else:
                    # For other types, add a default load address
                    load_addr = 0x0801  # Default BASIC start
                    return bytes([load_addr & 0xFF, (load_addr >> 8) & 0xFF]) + file_data
        
        return None
    
    def _load_directory(self) -> bytes:
        """Load directory as a PRG file (as C64 does).
        
        The directory is formatted as a BASIC program with line numbers.
        Each directory entry becomes a BASIC line.
        
        Returns:
            Directory as PRG format bytes
        """
        if not self.has_disk():
            return bytes()
        
        # Get directory entries
        disk_name, disk_id = self.disk.read_bam()
        entries = self.disk.read_directory()
        
        # Build directory as BASIC program
        # Load address for BASIC programs
        prg_data = bytearray()
        prg_data.extend([0x01, 0x08])  # Load address $0801
        
        # File type names
        type_names = {
            0: "DEL",
            1: "SEQ",
            2: "PRG",
            3: "USR",
            4: "REL"
        }
        
        # Current address in memory (for line pointers)
        current_addr = 0x0801
        
        # First line: disk header
        # Format: 0 "DISK NAME" ID
        header_line = f'0 "{disk_name}" {disk_id}'
        current_addr = self._add_basic_line(prg_data, current_addr, 0, header_line)
        
        # Add each file as a line
        for entry in entries:
            type_name = type_names.get(entry.filetype, "???")
            # Format: blocks "FILENAME" TYPE
            # Pad blocks to 4 characters, filename to 18 (with quotes)
            file_line = f'{entry.blocks:4d} "{entry.filename:16s}" {type_name}'
            # Use block count as line number
            current_addr = self._add_basic_line(prg_data, current_addr, entry.blocks, file_line)
        
        # Last line: blocks free
        total_blocks = sum(e.blocks for e in entries)
        blocks_free = max(0, 664 - total_blocks)
        free_line = f"{blocks_free} BLOCKS FREE."
        current_addr = self._add_basic_line(prg_data, current_addr, blocks_free, free_line)
        
        # End of program marker
        prg_data.extend([0x00, 0x00])
        
        return bytes(prg_data)
    
    def _add_basic_line(self, prg_data: bytearray, current_addr: int, line_number: int, text: str) -> int:
        """Add a BASIC line to PRG data.
        
        BASIC line format:
        - 2 bytes: pointer to next line (little endian)
        - 2 bytes: line number (little endian)
        - N bytes: line text (PETSCII)
        - 1 byte: $00 (end of line)
        
        Args:
            prg_data: PRG data to append to
            current_addr: Current address in memory
            line_number: BASIC line number
            text: Line text
            
        Returns:
            New current address after this line
        """
        # Calculate line length
        # 2 (next pointer) + 2 (line number) + len(text) + 1 (null terminator)
        line_length = 2 + 2 + len(text) + 1
        
        # Next line pointer
        next_addr = current_addr + line_length
        prg_data.extend([next_addr & 0xFF, (next_addr >> 8) & 0xFF])
        
        # Line number
        prg_data.extend([line_number & 0xFF, (line_number >> 8) & 0xFF])
        
        # Line text (convert to PETSCII)
        for ch in text:
            # Simple ASCII to PETSCII conversion
            if ch == '"':
                prg_data.append(ord('"'))
            elif ch.isupper():
                prg_data.append(ord(ch))
            elif ch.islower():
                # Lowercase letters in PETSCII
                prg_data.append(ord(ch.upper()))
            elif ch.isdigit() or ch == ' ' or ch == '.':
                prg_data.append(ord(ch))
            else:
                prg_data.append(ord(ch))
        
        # End of line
        prg_data.append(0x00)
        
        return next_addr
    
    def get_status(self) -> str:
        """Get drive status string.
        
        Returns:
            Status string (e.g., "00, OK,00,00")
        """
        if not self.has_disk():
            return "74,DRIVE NOT READY,00,00"
        return "00, OK,00,00"
