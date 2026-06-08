"""
Commodore 1541 disk drive emulation.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..d64 import D64Image

from ..d64 import (
    TOTAL_DISK_BLOCKS,
    dos_filetype_byte_closed,
    normalize_commodore_disk_catalog_name,
    parse_commodore_filename_mode,
)


class DiskDrive:
    """Emulates a Commodore 1541 disk drive.
    
    This is a simplified emulation that provides basic disk operations:
    - Loading files
    - Directory listing
    - File operations via command channel
    """

    # Canonical 1541 DOS error code -> message map.
    _DOS_ERRORS = {
        0: "OK",
        1: "FILES SCRATCHED",
        20: "READ ERROR",
        21: "READ ERROR",
        26: "WRITE PROTECT ON",
        30: "SYNTAX ERROR",
        31: "SYNTAX ERROR",
        32: "SYNTAX ERROR",
        33: "SYNTAX ERROR",
        50: "RECORD NOT PRESENT",
        60: "WRITE FILE OPEN",
        61: "FILE NOT OPEN",
        62: "FILE NOT FOUND",
        63: "FILE EXISTS",
        64: "FILE TYPE MISMATCH",
        67: "ILLEGAL TRACK OR SECTOR",
        72: "DISK FULL",
        73: "CBM DOS V2.6 1541",
        74: "DRIVE NOT READY",
    }

    def __init__(self, device_number: int = 8):
        """Initialize disk drive.
        
        Args:
            device_number: Drive device number (typically 8-11)
        """
        self.device_number = device_number
        self.disk: Optional[D64Image] = None
        self.disk_filename: Optional[str] = None
        # (code, message, track, sector) — Commodore-DOS-style error channel state.
        self.last_error: tuple = (0, "OK", 0, 0)
        # Set by :meth:`load_file` on success: DOS file type nibble (1–4) for KERNAL.
        self.last_loaded_dos_filetype: Optional[int] = None
    
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
            filename: File to load (use "$" for directory, "*" for first program)
            secondary_address: Secondary address (0 for load, 1 for verify)
            
        Returns:
            File data as bytes, or None if file not found
        """
        if not self.has_disk():
            return None

        self.last_loaded_dos_filetype = None

        # Special case: "$" loads directory
        if filename == "$":
            data = self._load_directory()
            self.last_loaded_dos_filetype = 2
            return data

        # Special case: "*" loads first program file
        if filename == "*":
            entries = self.disk.read_directory()
            # Find first PRG file
            for entry in entries:
                if entry.filetype == 2:  # PRG
                    file_data = self.disk.read_file(entry)
                    self.last_loaded_dos_filetype = 2
                    return file_data
            # No PRG files found
            return None

        stem, want_type = parse_commodore_filename_mode(filename)

        # Find file in directory
        entries = self.disk.read_directory()

        cat_name, _replace = normalize_commodore_disk_catalog_name(stem)
        clean_filename = cat_name

        for entry in entries:
            if entry.filename.upper().rstrip() != clean_filename:
                continue
            if want_type is not None and entry.filetype != want_type:
                self.last_error = (64, "FILE TYPE MISMATCH", 0, 0)
                return None

            if entry.filetype == 4:
                raw = self.disk.read_rel_file(entry)
            else:
                raw = self.disk.read_file(entry)

            self.last_loaded_dos_filetype = entry.filetype

            if entry.filetype == 2:
                # PRG: first two bytes are load address on disk
                return raw
            # SEQ / USR / REL — no load address bytes on disk for SEQ/USR chain;
            # REL returns raw concatenated record data.
            if entry.filetype == 4:
                return raw
            return bytes([0x01, 0x08]) + raw

        return None
    
    def save_file(self, filename: str, file_data: bytes) -> bool:
        """Save a file from the C64 into the attached D64 disk image.

        The bytes the C64 KERNAL sends include the 2-byte load address at the
        front of ``file_data`` for PRG saves. We write that payload verbatim
        into the D64 so that ``D64Image.read_file`` (which is what
        ``load_file`` uses) returns the exact same bytes back, preserving
        the load address.

        On success and if a backing ``disk_filename`` is known, the mutated
        D64 image is persisted to disk. Otherwise the change is in-memory
        only (still considered a successful save).

        On failure, ``self.last_error`` is set to a (code, message, track,
        sector) tuple using standard Commodore DOS codes:
            26  WRITE PROTECT ON
            63  FILE EXISTS
            67  ILLEGAL TRACK OR SECTOR
            72  DISK FULL
            74  DRIVE NOT READY

        Args:
            filename: File to save (quotes/whitespace stripped). A leading ``@``
                requests replace-on-write (scratch same catalog name first). After
                ``@``, optional ``S:`` is stripped for the 16-character catalog key
                only (see ``normalize_commodore_disk_catalog_name``). Trailing
                ``,P``/``,S``/… select the DOS file type.
            file_data: File data including the 2-byte load address at the
                front for PRG files.

        Returns:
            True if the save succeeded, False otherwise.
        """
        if not self.has_disk():
            self.last_error = (74, "DRIVE NOT READY", 0, 0)
            return False

        clean_stem, want_nibble = parse_commodore_filename_mode(filename)
        clean_filename, replace = normalize_commodore_disk_catalog_name(clean_stem)
        if not clean_filename:
            self.last_error = (34, "SYNTAX ERROR", 0, 0)
            return False

        if want_nibble == 4:  # REL — not supported for SAVE via fast path yet
            self.last_error = (34, "SYNTAX ERROR", 0, 0)
            return False

        type_nibble = want_nibble if want_nibble is not None else 2
        ft_byte = dos_filetype_byte_closed(type_nibble)

        # Reject empty payloads (no bytes to save).
        if not file_data:
            self.last_error = (34, "SYNTAX ERROR", 0, 0)
            return False

        # Detect "file exists" up-front for a clearer error than what
        # write_file's bool return conveys.
        try:
            existing = {
                e.filename.upper().rstrip()
                for e in self.disk.read_directory()
            }
        except Exception:
            existing = set()
        if clean_filename in existing:
            if replace:
                self._scratch_file(clean_filename)
            else:
                self.last_error = (63, "FILE EXISTS", 0, 0)
                return False

        try:
            ok = self.disk.write_file(clean_filename, file_data, filetype=ft_byte)
        except Exception as exc:
            self.last_error = (67, f"ILLEGAL TRACK OR SECTOR ({exc})", 0, 0)
            return False

        if not ok:
            # write_file returned False without raising — most likely the
            # disk was full (file-exists is handled above).
            self.last_error = (72, "DISK FULL", 0, 0)
            return False

        # Persist to backing file if we know one.
        if self.disk_filename:
            try:
                self.disk.save_to_file(self.disk_filename)
            except Exception as exc:
                self.last_error = (26, f"WRITE PROTECT ON ({exc})", 0, 0)
                return False

        self.last_error = (0, "OK", 0, 0)
        return True

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
        
        # Line 0: 1541 DOS header — $12 RVS ON, 16-char padded title, ID, "2A".
        # Trailing "2A" is the fixed disk-type suffix on every 1541 listing (not the
        # BAM id). No $92; KERNAL clears reverse at end of line (see cpu CHROUT).
        name_field = disk_name[:16].ljust(16)
        id_field = (disk_id[:2] if disk_id else "  ").ljust(2)[:2]
        header_bytes = (
            bytes([0x12, ord('"')])
            + name_field.encode("ascii")
            + bytes([ord('"'), ord(" ")])
            + id_field.encode("ascii")
            + b" 2A"
        )
        current_addr = self._add_basic_line_bytes(prg_data, current_addr, 0, header_bytes)

        # File lines: line number = block count; text aligns quotes per 1541 rules.
        for entry in entries:
            type_name = type_names.get(entry.filetype, "???")
            blocks = entry.blocks
            prefix = b" " * max(1, 4 - len(str(blocks)))
            fname = entry.filename.rstrip()[:16].ljust(16)
            file_bytes = prefix + b'"' + fname.encode("ascii") + b'" ' + type_name.encode("ascii")
            current_addr = self._add_basic_line_bytes(
                prg_data, current_addr, blocks, file_bytes
            )

        # Last line: blocks free (line number = free count).
        total_blocks = sum(e.blocks for e in entries)
        blocks_free = max(0, TOTAL_DISK_BLOCKS - total_blocks)
        free_bytes = b"BLOCKS FREE." + b" " * 18
        current_addr = self._add_basic_line_bytes(prg_data, current_addr, blocks_free, free_bytes)
        
        # End of program marker
        prg_data.extend([0x00, 0x00])
        
        return bytes(prg_data)
    
    def _add_basic_line_bytes(
        self, prg_data: bytearray, current_addr: int, line_number: int, text: bytes
    ) -> int:
        """Append one BASIC line with raw PETSCII line text (no trailing $00 in *text*)."""
        line_length = 2 + 2 + len(text) + 1
        next_addr = current_addr + line_length
        prg_data.extend([next_addr & 0xFF, (next_addr >> 8) & 0xFF])
        prg_data.extend([line_number & 0xFF, (line_number >> 8) & 0xFF])
        prg_data.extend(text)
        prg_data.append(0x00)
        return next_addr

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
        """Get drive status string in 1541 DOS error-channel format.

        Returns ``"NN,MESSAGE,TT,SS"``. The OK-status response keeps a leading
        space (``"00, OK,00,00"``) to match the historical 1541 DOS output.
        """
        if not self.has_disk():
            return "74,DRIVE NOT READY,00,00"
        code, message, track, sector = self.last_error
        if code == 0:
            return f"00, {message},{track:02d},{sector:02d}"
        return f"{code:02d},{message},{track:02d},{sector:02d}"

    def set_error(self, code: int, *, track: int = 0, sector: int = 0) -> None:
        """Record a DOS error on the status channel."""
        message = self._DOS_ERRORS.get(code, "SYNTAX ERROR")
        self.last_error = (int(code), message, int(track), int(sector))

    def clear_error(self) -> None:
        """Reset the status channel to OK."""
        self.last_error = (0, "OK", 0, 0)

    def command_channel_write(self, line: str) -> None:
        """Process a command-channel write (channel 15)."""
        if not isinstance(line, str):
            self.set_error(31)
            return
        cmd = line.strip()
        if not cmd:
            self.set_error(31)
            return

        upper = cmd.upper()

        if upper in ("I", "I0", "I1"):
            self.set_error(0)
            return

        if upper in ("V", "V0"):
            self.clear_error()
            return

        if upper.startswith("S0:") or upper.startswith("S:"):
            arg = cmd.split(":", 1)[1]
            names = [n.strip() for n in arg.split(",") if n.strip()]
            if not self.has_disk() or not names:
                self.set_error(62)
                return
            count = 0
            for name in names:
                if self._scratch_file(name):
                    count += 1
            if count == 0:
                self.set_error(62)
            else:
                self.last_error = (1, "FILES SCRATCHED", count, 0)
            return

        if upper.startswith("R0:") or upper.startswith("R:"):
            arg = cmd.split(":", 1)[1]
            if "=" not in arg:
                self.set_error(31)
                return
            new_name, old_name = arg.split("=", 1)
            new_name = new_name.strip()
            old_name = old_name.strip()
            if not new_name or not old_name or not self.has_disk():
                self.set_error(62)
                return
            if self._rename_file(old_name, new_name):
                self.clear_error()
            else:
                self.set_error(62)
            return

        if upper.startswith("N0:") or upper.startswith("N:"):
            self.clear_error()
            return

        self.set_error(31)

    def _ensure_mutable(self) -> None:
        if self.disk is None:
            return
        if not isinstance(self.disk.data, bytearray):
            self.disk.data = bytearray(self.disk.data)

    def _iter_dir_slots(self):
        if self.disk is None:
            return
        track, sector = 18, 1
        visited = set()
        while track != 0 and (track, sector) not in visited:
            visited.add((track, sector))
            try:
                sector_offset = self.disk._track_sector_to_offset(track, sector)
            except ValueError:
                return
            next_track = self.disk.data[sector_offset]
            next_sector = self.disk.data[sector_offset + 1]
            for i in range(8):
                entry_off = sector_offset + 2 + i * 32
                if self.disk.data[entry_off] == 0:
                    continue
                yield entry_off
            track = next_track
            if track != 0:
                sector = next_sector

    def _slot_filename(self, entry_off: int) -> str:
        if self.disk is None:
            return ""
        raw = bytes(self.disk.data[entry_off + 3:entry_off + 19])
        return self.disk._petscii_to_ascii(raw).rstrip().upper()

    def _bam_free_sector(self, track: int, sector: int) -> None:
        if self.disk is None:
            return
        if track < 1 or track > 35:
            return
        try:
            max_sec = self.disk._get_sectors_per_track(track)
        except ValueError:
            return
        if sector < 0 or sector >= max_sec:
            return
        bam_off = self.disk._track_sector_to_offset(18, 0)
        entry_off = bam_off + 4 + (track - 1) * 4
        byte_idx = 1 + (sector // 8)
        bit = 1 << (sector % 8)
        if not (self.disk.data[entry_off + byte_idx] & bit):
            self.disk.data[entry_off + byte_idx] |= bit
            self.disk.data[entry_off] = (self.disk.data[entry_off] + 1) & 0xFF

    def _free_sector_chain(self, track: int, sector: int) -> None:
        if self.disk is None:
            return
        visited = set()
        while track != 0 and (track, sector) not in visited:
            visited.add((track, sector))
            try:
                sec = self.disk.read_sector(track, sector)
            except ValueError:
                break
            next_track = sec[0]
            next_sector = sec[1]
            self._bam_free_sector(track, sector)
            track, sector = next_track, next_sector

    def _scratch_file(self, name: str) -> bool:
        if self.disk is None:
            return False
        self._ensure_mutable()
        target = name.strip().upper().strip('"')
        removed = False
        for entry_off in list(self._iter_dir_slots()):
            if self._slot_filename(entry_off) == target:
                start_track = self.disk.data[entry_off + 1]
                start_sector = self.disk.data[entry_off + 2]
                if start_track != 0:
                    self._free_sector_chain(start_track, start_sector)
                self.disk.data[entry_off] = 0
                removed = True
        return removed

    def _rename_file(self, old_name: str, new_name: str) -> bool:
        if self.disk is None:
            return False
        self._ensure_mutable()
        old_target = old_name.strip().upper().strip('"')
        new_target = new_name.strip().upper().strip('"')[:16]
        for entry_off in self._iter_dir_slots():
            if self._slot_filename(entry_off) == old_target:
                buf = bytearray(16)
                for i, ch in enumerate(new_target):
                    buf[i] = ord(ch) & 0xFF
                for i in range(len(new_target), 16):
                    buf[i] = 0xA0
                self.disk.data[entry_off + 3:entry_off + 19] = bytes(buf)
                return True
        return False
