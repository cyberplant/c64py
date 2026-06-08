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


def parse_commodore_filename_mode(filename: str) -> Tuple[str, Optional[int]]:
    """Split ``"NAME,S"`` / ``NAME,P`` style strings into stem and DOS file type.

    Returns ``(stem, filetype_nibble)`` where ``filetype_nibble`` is ``1`` SEQ,
    ``2`` PRG, ``3`` USR, ``4`` REL, or ``None`` if no trailing ``,P``/``,S``/``,R``/``,U``.

    The stem is what we compare against directory names (no quotes, stripped).
    Trailing ``,W`` / ``,R`` (OPEN read/write mode) is stripped first.
    """
    fn = filename.strip().strip('"').strip()
    # Strip OPEN channel mode ,W / ,R (may follow file type: "N,S,W")
    while "," in fn:
        _base, suf = fn.rsplit(",", 1)
        suf_u = suf.strip().upper()
        if suf_u in ("W", "R"):
            fn = _base
            continue
        break
    if "," not in fn:
        return fn, None
    base, suf = fn.rsplit(",", 1)
    suf = suf.strip().upper()
    if len(suf) != 1:
        return fn, None
    map_letter = {"P": 2, "S": 1, "U": 3, "R": 4}
    if suf not in map_letter:
        return fn, None
    return base.strip(), map_letter[suf]


def dos_filetype_byte_closed(nibble: int) -> int:
    """Closed-splat directory type byte (0x80 | nibble) for PRG/SEQ/USR/REL."""
    return 0x80 | (nibble & 0x0F)


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
        return bytes(self.data[offset:offset + 256])

    def write_sector(self, track: int, sector: int, payload: bytes) -> None:
        """Overwrite a 256-byte sector with ``payload`` (must be exactly 256 bytes)."""
        if len(payload) != 256:
            raise ValueError(f"sector payload must be 256 bytes, got {len(payload)}")
        offset = self._track_sector_to_offset(track, sector)
        self.data[offset:offset + 256] = payload
    
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

        PETSCII has two character sets (unshifted vs C= key pressed):
        - Unshifted: 0x41-0x5A = A-Z (uppercase), 0x61-0x7A = graphics chars
        - Shifted:   0x41-0x5A = graphics chars, 0x61-0x7A = a-z (lowercase)

        For directory display, filenames are typically stored in PETSCII uppercase
        (0x41-0x5A) but should display as ASCII lowercase (0x61-0x7A). We map:
        - PETSCII 0x41-0x5A → ASCII 0x61-0x7A (uppercase to lowercase)
        - PETSCII 0x61-0x7A → ASCII 0x61-0x7A (already lowercase, direct map)

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
            elif 0x41 <= byte <= 0x5A:  # A-Z (same in PETSCII and ASCII)
                result.append(chr(byte))
            elif 0x61 <= byte <= 0x7A:  # PETSCII lowercase → ASCII lowercase (same codes)
                result.append(chr(byte))
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
                
                # 32-byte entry layout (entry_data starts AFTER the 2-byte sector chain link):
                # [0]    = file type byte (bits 0-3 = type, bit 7 = closed)
                # [1]    = file start track
                # [2]    = file start sector
                # [3:19] = filename (16 bytes PETSCII, padded with 0xA0)
                # [28:30]= file size in blocks (little-endian)
                file_type_byte = entry_data[0]
                
                # Skip if entry is not used (type 0 = scratched/deleted)
                if file_type_byte == 0:
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

    def read_rel_file(self, entry: D64DirEntry) -> bytes:
        """Read a REL file via side-sector chain and 60 (track,sector) slots.

        Commodore REL files start at a *side sector* (not a linear sector chain).
        Each 256-byte side sector has up to 60 data pointers at offsets 4..123,
        then optional link to the next side sector at bytes 0-1.
        """
        out = bytearray()
        t, s = entry.track, entry.sector
        while t != 0:
            ss = self.read_sector(t, s)
            next_t, next_s = ss[0], ss[1]
            for i in range(60):
                dt = ss[4 + i * 2]
                ds = ss[5 + i * 2]
                if dt == 0:
                    break
                sec = self.read_sector(dt, ds)
                nt, ns = sec[0], sec[1]
                if nt == 0:
                    n = ns if ns > 0 else 1
                    out.extend(sec[2 : 2 + n])
                else:
                    out.extend(sec[2:256])
            if next_t == 0:
                break
            t, s = next_t, next_s
        return bytes(out)

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
    
    # ------------------------------------------------------------------
    # Write support
    # ------------------------------------------------------------------
    #
    # The D64 image is mutated in-place via ``self.data`` (a bytearray, see
    # ``__init__``). Writing a file involves three pieces:
    #   1. ``_alloc_sector`` walks the BAM (track 18 sector 0) using the
    #      classic 1541 allocation order (start near the directory and spiral
    #      outward, skipping track 18) and marks a free sector used.
    #   2. ``write_file`` chains the data sectors together using the
    #      next-track / next-sector link bytes at offset 0/1 of every data
    #      sector. The final sector's link is (0, last_byte_index) so
    #      ``read_file`` can recover the exact file length.
    #   3. ``_find_dir_slot`` returns an empty 32-byte directory slot,
    #      allocating and chaining a new directory sector if every slot in
    #      the existing dir chain is occupied.
    #
    # ``save_to_file`` simply persists ``self.data``, so all of the above
    # is automatically picked up.
    #
    # Reference: Immers/Neufeld "Inside Commodore DOS" / 1541 ROM listings.

    # Standard 1541 allocation order: prefer the directory track's
    # neighbours and spiral outward, skipping track 18 itself.
    _ALLOC_ORDER = (
        [t for t in (17, 19, 16, 20, 15, 21, 14, 22, 13, 23, 12, 24, 11, 25,
                     10, 26, 9, 27, 8, 28, 7, 29, 6, 30, 5, 31, 4, 32, 3, 33,
                     2, 34, 1, 35)]
    )

    def _bam_offset(self, track: int) -> int:
        """Return the byte offset of the 4-byte BAM entry for ``track``
        within track 18 sector 0."""
        return 4 + (track - 1) * 4

    def _is_sector_free(self, bam: bytes, track: int, sector: int) -> bool:
        base = self._bam_offset(track)
        byte_index = sector // 8
        bit_index = sector % 8
        return bool(bam[base + 1 + byte_index] & (1 << bit_index))

    def _set_sector_used(self, bam: bytearray, track: int, sector: int) -> None:
        base = self._bam_offset(track)
        byte_index = sector // 8
        bit_index = sector % 8
        if bam[base + 1 + byte_index] & (1 << bit_index):
            bam[base + 1 + byte_index] &= ~(1 << bit_index) & 0xFF
            if bam[base] > 0:
                bam[base] -= 1

    def _set_sector_free(self, bam: bytearray, track: int, sector: int) -> None:
        base = self._bam_offset(track)
        byte_index = sector // 8
        bit_index = sector % 8
        if not (bam[base + 1 + byte_index] & (1 << bit_index)):
            bam[base + 1 + byte_index] |= (1 << bit_index)
            max_sectors = self._get_sectors_per_track(track)
            if bam[base] < max_sectors:
                bam[base] += 1

    def _write_sector(self, track: int, sector: int, payload: bytes) -> None:
        """Write a 256-byte payload to ``track``/``sector``."""
        if len(payload) != 256:
            raise ValueError(
                f"_write_sector expects exactly 256 bytes, got {len(payload)}"
            )
        offset = self._track_sector_to_offset(track, sector)
        self.data[offset:offset + 256] = payload

    def _alloc_sector(self) -> Optional[Tuple[int, int]]:
        """Find a free sector via BAM, mark it used, and return (track, sector).

        Returns ``None`` if the disk is full.
        """
        bam_offset = self._track_sector_to_offset(18, 0)
        bam = bytearray(self.data[bam_offset:bam_offset + 256])

        for track in self._ALLOC_ORDER:
            if track == 18:
                continue
            base = self._bam_offset(track)
            if bam[base] == 0:
                continue
            max_sectors = self._get_sectors_per_track(track)
            for sector in range(max_sectors):
                if self._is_sector_free(bam, track, sector):
                    self._set_sector_used(bam, track, sector)
                    self.data[bam_offset:bam_offset + 256] = bam
                    return (track, sector)
        return None

    def _alloc_dir_sector(self) -> Optional[Tuple[int, int]]:
        """Allocate a free sector on track 18 (for new directory blocks)."""
        bam_offset = self._track_sector_to_offset(18, 0)
        bam = bytearray(self.data[bam_offset:bam_offset + 256])
        max_sectors = self._get_sectors_per_track(18)
        # Sectors 0 (BAM) and the rest are typically used by directory.
        # Walk the directory track in order so dir blocks stay contiguous.
        for sector in range(1, max_sectors):
            if self._is_sector_free(bam, 18, sector):
                self._set_sector_used(bam, 18, sector)
                self.data[bam_offset:bam_offset + 256] = bam
                return (18, sector)
        return None

    def _free_sector(self, track: int, sector: int) -> None:
        """Mark a sector as free in the BAM (used by future delete/rename paths)."""
        bam_offset = self._track_sector_to_offset(18, 0)
        bam = bytearray(self.data[bam_offset:bam_offset + 256])
        self._set_sector_free(bam, track, sector)
        self.data[bam_offset:bam_offset + 256] = bam

    def _ascii_to_petscii_filename(self, name: str) -> bytes:
        """Encode an ASCII filename as a 16-byte PETSCII field padded with 0xA0."""
        # Uppercase ASCII letters map directly to PETSCII codes.
        encoded = bytearray()
        for ch in name[:16]:
            b = ord(ch)
            if 0x61 <= b <= 0x7A:  # lowercase ASCII -> uppercase PETSCII
                b -= 0x20
            encoded.append(b & 0xFF)
        while len(encoded) < 16:
            encoded.append(0xA0)
        return bytes(encoded)

    def _find_dir_slot(self) -> Optional[Tuple[int, int, int]]:
        """Find a free 32-byte directory slot.

        Walks the directory chain starting at 18/1. If every slot in every
        existing dir sector is occupied, allocate a new dir sector on track
        18 and link it from the previous dir sector's T/S pointer.

        Returns (track, sector, slot_index) or None if no dir sector can be
        allocated (track 18 full).
        """
        track, sector = 18, 1
        while True:
            sector_data = bytearray(self.read_sector(track, sector))
            for i in range(8):
                offset = 2 + i * 32
                if sector_data[offset] == 0x00:
                    return (track, sector, i)
            next_track = sector_data[0]
            next_sector = sector_data[1]
            if next_track == 0:
                # Need to allocate a new dir sector and chain it in.
                new = self._alloc_dir_sector()
                if new is None:
                    return None
                new_t, new_s = new
                # Link previous sector to new one.
                sector_data[0] = new_t
                sector_data[1] = new_s
                self._write_sector(track, sector, bytes(sector_data))
                # Initialise the new sector: empty dir, end-of-chain.
                fresh = bytearray(256)
                fresh[0] = 0x00
                fresh[1] = 0xFF
                self._write_sector(new_t, new_s, bytes(fresh))
                return (new_t, new_s, 0)
            track, sector = next_track, next_sector

    def _file_exists(self, filename: str) -> bool:
        target = filename.upper().rstrip()
        for entry in self.read_directory():
            if entry.filename.upper().rstrip() == target:
                return True
        return False

    def write_file(self, filename: str, file_data: bytes,
                   filetype: int = 0x82) -> bool:
        """Write a PRG file to the D64 image, allocating sectors via the BAM.

        Args:
            filename: ASCII filename (will be uppercased / truncated to 16
                chars and PETSCII-encoded).
            file_data: Full file payload, including the 2-byte load address
                for PRG files (matches what ``read_file`` returns).
            filetype: Directory entry file-type byte. Defaults to 0x82
                (closed PRG).

        Returns:
            True on success, False if the file already exists or the disk
            is full.
        """
        clean_name = filename.strip().strip('"').upper()[:16]
        if not clean_name:
            return False
        if self._file_exists(clean_name):
            return False
        if not file_data:
            return False

        # Compute number of 254-byte data sectors needed.
        n = (len(file_data) + 253) // 254
        # Allocate them all up front so a "disk full" failure is atomic.
        allocations = []
        # We must rollback on failure; capture BAM beforehand.
        bam_offset = self._track_sector_to_offset(18, 0)
        bam_backup = bytes(self.data[bam_offset:bam_offset + 256])

        try:
            for _ in range(n):
                ts = self._alloc_sector()
                if ts is None:
                    raise RuntimeError("disk full")
                allocations.append(ts)
        except RuntimeError:
            # Rollback BAM (no data sectors written yet).
            self.data[bam_offset:bam_offset + 256] = bam_backup
            return False

        # Find a directory slot (may allocate a new dir sector on track 18).
        slot = self._find_dir_slot()
        if slot is None:
            # Roll back data-sector allocations.
            self.data[bam_offset:bam_offset + 256] = bam_backup
            return False

        # Write data sectors with chain links.
        for i, (track, sector) in enumerate(allocations):
            payload = bytearray(256)
            chunk_start = i * 254
            chunk = file_data[chunk_start:chunk_start + 254]
            if i + 1 < len(allocations):
                next_t, next_s = allocations[i + 1]
                payload[0] = next_t
                payload[1] = next_s
            else:
                # Last sector: link bytes = (0, bytes_used_in_this_sector).
                # ``read_file`` does ``data.extend(sector_data[2:2 + byte1])``
                # so we set byte1 to the exact chunk length to round-trip
                # the payload byte-for-byte. (Real CBM DOS uses the
                # 1-based offset of the last data byte, i.e. len+1; the
                # existing reader treats it as a count, so we match the
                # reader.)
                payload[0] = 0
                payload[1] = len(chunk) & 0xFF
            payload[2:2 + len(chunk)] = chunk
            self._write_sector(track, sector, bytes(payload))

        # Write directory entry. Note: the read side computes
        # ``offset = 2 + i*32``; the trailing slot (i=7) therefore overlaps
        # the next sector's link bytes if we wrote a full 32 bytes, so we
        # clamp wipe/write to the sector boundary (256 bytes total).
        dir_track, dir_sector, slot_idx = slot
        dir_data = bytearray(self.read_sector(dir_track, dir_sector))
        entry_off = 2 + slot_idx * 32
        wipe_end = min(entry_off + 32, 256)
        for j in range(entry_off, wipe_end):
            dir_data[j] = 0x00
        first_t, first_s = allocations[0]
        dir_data[entry_off + 0] = filetype & 0xFF
        dir_data[entry_off + 1] = first_t
        dir_data[entry_off + 2] = first_s
        dir_data[entry_off + 3:entry_off + 19] = self._ascii_to_petscii_filename(clean_name)
        # bytes 19..27 reserved (REL/etc.) — leave 0.
        # block count at 28/29 little-endian.
        dir_data[entry_off + 28] = len(allocations) & 0xFF
        dir_data[entry_off + 29] = (len(allocations) >> 8) & 0xFF
        self._write_sector(dir_track, dir_sector, bytes(dir_data))

        return True
    
    def save_to_file(self, filename: str) -> None:
        """Save the D64 image to a file.
        
        Args:
            filename: Path to save the D64 file
        """
        with open(filename, 'wb') as f:
            f.write(self.data)


def create_blank_d64(disk_name: str = "BLANK", disk_id: str = "01") -> D64Image:
    """Create a freshly-formatted, empty 35-track D64 image.

    The image has all 683 sectors zeroed except for track 18:
      - Sector 0 (BAM): DOS version 'A', a full BAM bitmap with every
        sector marked free *except* 18/0 and 18/1 (which are the BAM and
        the first directory sector), and a disk name/ID block.
      - Sector 1 (first directory sector): empty, end-of-chain link
        (00 FF) at offsets 0/1.

    Returns a writable D64Image. Useful for tests and as a "format" stub.
    """
    data = bytearray(D64_SIZE_STANDARD)
    img = D64Image.__new__(D64Image)
    img.data = data

    # Build BAM in track 18 sector 0.
    bam_off = img._track_sector_to_offset(18, 0)
    bam = bytearray(256)
    bam[0] = 18  # next dir T/S = 18/1
    bam[1] = 1
    bam[2] = ord('A')  # DOS version
    bam[3] = 0x00      # unused
    # Per-track BAM entries: free count + 3-byte bitmap.
    for track in range(1, 36):
        max_sectors = img._get_sectors_per_track(track)
        base = 4 + (track - 1) * 4
        bam[base] = max_sectors  # free count (will adjust track 18 below)
        # Mark all sectors free.
        bits = (1 << max_sectors) - 1
        bam[base + 1] = bits & 0xFF
        bam[base + 2] = (bits >> 8) & 0xFF
        bam[base + 3] = (bits >> 16) & 0xFF
    # Track 18: mark sectors 0 and 1 used.
    t18 = 4 + 17 * 4
    bam[t18 + 1] &= ~0x03 & 0xFF  # clear bits 0 and 1
    bam[t18] = img._get_sectors_per_track(18) - 2

    # Disk name (16 bytes PETSCII, padded with 0xA0) at 0x90.
    name_bytes = bytearray()
    for ch in disk_name.upper()[:16]:
        name_bytes.append(ord(ch) & 0xFF)
    while len(name_bytes) < 16:
        name_bytes.append(0xA0)
    bam[0x90:0xA0] = name_bytes
    # Filler 0xA0 at 0xA0/0xA1.
    bam[0xA0] = 0xA0
    bam[0xA1] = 0xA0
    # Disk ID at 0xA2-0xA3.
    id_bytes = (disk_id.upper() + "  ")[:2]
    bam[0xA2] = ord(id_bytes[0]) & 0xFF
    bam[0xA3] = ord(id_bytes[1]) & 0xFF
    bam[0xA4] = 0xA0  # shifted-space filler
    bam[0xA5] = ord('2')  # DOS type "2A"
    bam[0xA6] = ord('A')
    bam[0xA7] = 0xA0
    bam[0xA8] = 0xA0
    bam[0xA9] = 0xA0
    bam[0xAA] = 0xA0
    img.data[bam_off:bam_off + 256] = bam

    # First directory sector (18/1): end-of-chain.
    dir_off = img._track_sector_to_offset(18, 1)
    dir_sector = bytearray(256)
    dir_sector[0] = 0x00
    dir_sector[1] = 0xFF
    img.data[dir_off:dir_off + 256] = dir_sector

    return img


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
