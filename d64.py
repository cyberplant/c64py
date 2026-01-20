"""
D64 disk image format parser for Commodore 1541 disk drive emulation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple


# D64 disk image constants
D64_SIZE_STANDARD = 174848  # Standard 35-track D64 without error bytes
D64_SIZE_WITH_ERRORS = 175531  # D64 with error bytes appended
TOTAL_DISK_BLOCKS = 664  # Total blocks on a standard 1541 disk


@dataclass
class D64DirEntry:
    """Represents a directory entry in a D64 disk image."""
    filetype: int  # File type (0=DEL, 1=SEQ, 2=PRG, 3=USR, 4=REL)
    filename: str  # File name (PETSCII, max 16 chars)
    track: int  # Starting track
    sector: int  # Starting sector
    blocks: int  # File size in blocks


class D64Image:
    """Parser and reader for D64 disk image format.
    
    D64 format stores a 1541 disk image:
    - 35 tracks (numbered 1-35)
    - Variable sectors per track (21/19/18/17)
    - 256 bytes per sector
    - Track 18 contains directory and BAM
    """
    
    # Sectors per track for different zones
    SECTORS_PER_TRACK = {
        range(1, 18): 21,   # Tracks 1-17: 21 sectors
        range(18, 25): 19,  # Tracks 18-24: 19 sectors
        range(25, 31): 18,  # Tracks 25-30: 18 sectors
        range(31, 36): 17,  # Tracks 31-35: 17 sectors
    }
    
    def __init__(self, data: bytes):
        """Initialize D64 image from bytes.
        
        Args:
            data: Raw D64 disk image data
        """
        # Use bytearray for mutable data
        self.data = bytearray(data)
        # Validate size (should be 174848 bytes for standard 35-track D64)
        if len(data) not in (D64_SIZE_STANDARD, D64_SIZE_WITH_ERRORS):
            raise ValueError(
                f"Invalid D64 size: {len(data)} bytes "
                f"(expected {D64_SIZE_STANDARD} or {D64_SIZE_WITH_ERRORS})"
            )
    
    def _get_sectors_per_track(self, track: int) -> int:
        """Get number of sectors for a given track."""
        for track_range, sectors in self.SECTORS_PER_TRACK.items():
            if track in track_range:
                return sectors
        raise ValueError(f"Invalid track number: {track}")
    
    def _track_sector_to_offset(self, track: int, sector: int) -> int:
        """Convert track/sector to byte offset in D64 image."""
        if track < 1 or track > 35:
            raise ValueError(f"Track out of range: {track}")
        
        offset = 0
        # Calculate offset for all tracks before this one
        for t in range(1, track):
            offset += self._get_sectors_per_track(t) * 256
        
        # Add offset for sector within this track
        max_sectors = self._get_sectors_per_track(track)
        if sector < 0 or sector >= max_sectors:
            raise ValueError(f"Sector {sector} out of range for track {track} (max {max_sectors})")
        
        offset += sector * 256
        return offset
    
    def read_sector(self, track: int, sector: int) -> bytes:
        """Read a 256-byte sector from the disk image.
        
        Args:
            track: Track number (1-35)
            sector: Sector number (0-based, varies by track)
            
        Returns:
            256 bytes of sector data
        """
        offset = self._track_sector_to_offset(track, sector)
        return self.data[offset:offset + 256]
    
    def read_bam(self) -> Tuple[str, str]:
        """Read BAM (Block Availability Map) from track 18, sector 0.
        
        Returns:
            Tuple of (disk_name, disk_id)
        """
        bam = self.read_sector(18, 0)
        
        # Disk name is at offset 0x90-0x9F (16 bytes, PETSCII)
        disk_name_bytes = bam[0x90:0xA0]
        disk_name = self._petscii_to_ascii(disk_name_bytes).rstrip()
        
        # Disk ID is at offset 0xA2-0xA3 (2 bytes, PETSCII)
        disk_id_bytes = bam[0xA2:0xA4]
        disk_id = self._petscii_to_ascii(disk_id_bytes).rstrip()
        
        return disk_name, disk_id
    
    def _petscii_to_ascii(self, data: bytes) -> str:
        """Convert PETSCII bytes to ASCII string.
        
        Args:
            data: PETSCII encoded bytes
            
        Returns:
            ASCII string
        """
        result = []
        for byte in data:
            # Convert PETSCII to ASCII
            if byte == 0xA0 or byte == 0x00:  # Shifted space or null
                result.append(' ')
            elif 0x41 <= byte <= 0x5A:  # A-Z
                result.append(chr(byte))
            elif 0x61 <= byte <= 0x7A:  # a-z (PETSCII lowercase)
                result.append(chr(byte - 32))  # Convert to uppercase
            elif 0x30 <= byte <= 0x39:  # 0-9
                result.append(chr(byte))
            elif byte == 0x20:  # Space
                result.append(' ')
            elif 0x21 <= byte <= 0x2F:  # Punctuation
                result.append(chr(byte))
            elif 0x3A <= byte <= 0x40:  # More punctuation
                result.append(chr(byte))
            elif 0x5B <= byte <= 0x60:  # Brackets, etc.
                result.append(chr(byte))
            else:
                result.append('?')  # Unknown character
        return ''.join(result)
    
    def read_directory(self) -> List[D64DirEntry]:
        """Read directory entries from the disk.
        
        The directory starts at track 18, sector 1.
        Each sector can hold up to 8 directory entries (32 bytes each).
        
        Returns:
            List of directory entries
        """
        entries = []
        track = 18
        sector = 1
        
        while track != 0:
            sector_data = self.read_sector(track, sector)
            
            # First 2 bytes are link to next directory sector
            next_track = sector_data[0]
            next_sector = sector_data[1]
            
            # Read up to 8 directory entries from this sector
            for i in range(8):
                offset = 2 + (i * 32)
                entry_data = sector_data[offset:offset + 32]
                
                # File type is at offset 0 (bits 0-3 = type, bit 7 = closed flag)
                file_type_byte = entry_data[0]
                
                # Skip if entry is not used (type 0 = scratched/deleted)
                if file_type_byte == 0 or file_type_byte == 0x00:
                    continue
                
                filetype = file_type_byte & 0x07
                
                # Starting track/sector at offset 1-2
                start_track = entry_data[1]
                start_sector = entry_data[2]
                
                # Filename at offset 3-18 (16 bytes, PETSCII, padded with 0xA0)
                filename_bytes = entry_data[3:19]
                filename = self._petscii_to_ascii(filename_bytes).rstrip()
                
                # File size in blocks at offset 28-29 (little endian)
                blocks = entry_data[28] | (entry_data[29] << 8)
                
                # Only add valid entries
                if start_track != 0:
                    entries.append(D64DirEntry(
                        filetype=filetype,
                        filename=filename,
                        track=start_track,
                        sector=start_sector,
                        blocks=blocks
                    ))
            
            # Move to next directory sector
            track = next_track
            if track != 0:
                sector = next_sector
        
        return entries
    
    def read_file(self, entry: D64DirEntry) -> bytes:
        """Read file data from disk image.
        
        Args:
            entry: Directory entry for the file to read
            
        Returns:
            File data as bytes
        """
        data = []
        track = entry.track
        sector = entry.sector
        
        while track != 0:
            sector_data = self.read_sector(track, sector)
            
            # First 2 bytes are link to next sector
            next_track = sector_data[0]
            next_sector = sector_data[1]
            
            if next_track == 0:
                # Last sector - next_sector contains number of bytes used (1-255)
                bytes_used = next_sector if next_sector > 0 else 1
                data.extend(sector_data[2:2 + bytes_used])
            else:
                # Not last sector - use all 254 bytes
                data.extend(sector_data[2:256])
            
            track = next_track
            if track != 0:
                sector = next_sector
        
        return bytes(data)
    
    def format_directory_listing(self) -> str:
        """Format directory listing as C64 would display it.
        
        Returns:
            Formatted directory listing string
        """
        disk_name, disk_id = self.read_bam()
        entries = self.read_directory()
        
        # File type codes
        type_names = {
            0: "DEL",
            1: "SEQ",
            2: "PRG",
            3: "USR",
            4: "REL"
        }
        
        lines = []
        lines.append(f'0 "{disk_name}" {disk_id}')
        
        for entry in entries:
            # Format: blocks "filename" type
            type_name = type_names.get(entry.filetype, "???")
            # Pad filename to 16 characters for proper alignment
            padded_name = entry.filename.ljust(16)
            lines.append(f'{entry.blocks:4d} "{padded_name}" {type_name}')
        
        # Calculate blocks free (simplified - just count total used)
        total_blocks = sum(e.blocks for e in entries)
        # Standard 1541 has 664 blocks total
        blocks_free = max(0, TOTAL_DISK_BLOCKS - total_blocks)
        lines.append(f"{blocks_free} BLOCKS FREE.")
        
        return '\n'.join(lines)
    
    def write_file(self, filename: str, file_data: bytes) -> bool:
        """Write a file to the D64 disk image.
        
        This is a simplified implementation that saves files to a companion
        directory rather than modifying the D64 image itself (which would require
        complex BAM management and sector allocation).
        
        Args:
            filename: Filename (will be padded/truncated to 16 chars)
            file_data: File data (including load address for PRG files)
            
        Returns:
            True if successful, False otherwise
        """
        # For now, return False to indicate write not supported in D64
        # The drive layer can handle this by saving to filesystem
        return False
    
    def save_to_file(self, filename: str) -> None:
        """Save the D64 image to a file.
        
        Args:
            filename: Path to save the D64 file
        """
        with open(filename, 'wb') as f:
            f.write(self.data)


def load_d64(filename: str) -> D64Image:
    """Load a D64 disk image from file.
    
    Args:
        filename: Path to D64 file
        
    Returns:
        D64Image instance
    """
    with open(filename, 'rb') as f:
        data = f.read()
    return D64Image(data)
