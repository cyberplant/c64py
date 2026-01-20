# IEC Serial Bus Implementation Status

## Overview

The IEC (IEEE-488 derivative) serial bus implementation provides authentic hardware-level emulation of the Commodore 64's peripheral communication system. This replaces the temporary KERNAL hook approach with proper serial bus protocol.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ C64 Computer                                                │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐             │
│  │  KERNAL  │───→│   CIA2   │───→│ IEC Bus  │             │
│  │  $FFD5   │    │  $DD00   │    │Connector │             │
│  └──────────┘    └──────────┘    └────┬─────┘             │
└────────────────────────────────────────│───────────────────┘
                                         │
                    ┌────────────────────┴────────────────┐
                    │   IEC Serial Bus                     │
                    │   - ATN (Attention)                  │
                    │   - CLK (Clock)                      │
                    │   - DATA                             │
                    │   Open-collector wired-AND logic     │
                    └────────┬─────────────────────────────┘
                             │
        ┌────────────────────┴────────────────────┐
        │  1541 Disk Drive (Device 8)              │
        │   ┌──────────┐  ┌──────────┐  ┌───────┐ │
        │   │ 6502 CPU │  │ DOS ROM  │  │  VIA  │ │
        │   │  1MHz    │  │  16KB    │  │  I/O  │ │
        │   └──────────┘  └──────────┘  └───────┘ │
        │   ┌──────────┐  ┌──────────┐  ┌───────┐ │
        │   │ 2KB RAM  │  │Serial ROM│  │  D64  │ │
        │   │          │  │   8KB    │  │ Image │ │
        │   └──────────┘  └──────────┘  └───────┘ │
        └─────────────────────────────────────────┘
```

## Implementation Phases

### ✅ Phase 1: Foundation (commit ce0d6a6)
**Module:** `iec_bus.py`, `drive1541.py`

- IEC bus signal management (ATN, CLK, DATA)
- Open-collector bus logic (wired-AND)
- 1541 drive structure with 6502 CPU
- Memory map (2KB RAM, 16KB DOS ROM, 8KB Serial ROM)
- VIA register emulation (simplified)

### ✅ Phase 2: ROM Loading (commit c9c6fd1)
**Module:** `roms.py`

- ROM discovery for 1541 drives
- Search paths include VICE DRIVES directories
- Support for multiple ROM filename variants
- Functions:
  - `find_drive_rom("dos1541")` - DOS ROM (16KB)
  - `find_drive_rom("serial1541")` - Serial ROM (8KB)

### ✅ Phase 3: CIA Integration (commit 91ac8a0)
**Module:** `memory.py`

- Connected CIA2 Port A to IEC bus
- Memory map includes IEC bus reference
- CIA2 register handlers:
  - $DD00 Port A: ATN OUT (bit 3), CLK OUT (bit 4), DATA OUT (bit 5), CLK IN (bit 6), DATA IN (bit 7)
  - $DD02 DDRA: Data direction control
- Real-time bus state synchronization

### ✅ Phase 4: Drive Initialization (commit 220c191)
**Module:** `emulator.py`

- `initialize_iec_bus()` method
- Automatic ROM loading
- Drive creation for devices 8-11
- IEC bus attachment
- Fallback to KERNAL hooks if ROMs not found

## Current Status

### Working Features
- ✅ IEC bus infrastructure complete
- ✅ CIA2-to-IEC connection functional
- ✅ 1541 drives created with ROMs loaded
- ✅ Open-collector bus signal handling
- ✅ Multi-device support (4 drives simultaneously)
- ✅ Fallback to KERNAL hooks (LOAD/SAVE still work)

### In Progress
- ⏳ Byte-level IEC protocol (send/receive)
- ⏳ Drive CPU execution loop
- ⏳ DOS ROM ↔ D64 integration
- ⏳ End-to-end LOAD testing

### Not Yet Implemented
- ⏳ IEC protocol timing (microsecond-level)
- ⏳ Drive ROM execution stepping
- ⏳ TALK/LISTEN byte transfers
- ⏳ EOI (End Of Indicator) handling
- ⏳ Drive command channel processing
- ⏳ Full SAVE through IEC

## IEC Bus Signals

### ATN (Attention)
- **Direction:** C64 → Devices (unidirectional)
- **Function:** Indicates command byte follows
- **Controlled by:** C64 CIA2 Port A bit 3 (inverted: 0=asserted, 1=released)
- **Implementation:** `IECBus.set_atn(state)`

### CLK (Clock)
- **Direction:** Bidirectional
- **Function:** Clock signal for data synchronization
- **Controlled by:** CIA2 Port A bit 4 (C64 side)
- **Read from:** CIA2 Port A bit 6 (C64 reads bus state)
- **Implementation:** `IECBus.set_clk(device_id, state)`, open-collector logic

### DATA
- **Direction:** Bidirectional
- **Function:** Data transmission line
- **Controlled by:** CIA2 Port A bit 5 (C64 side)
- **Read from:** CIA2 Port A bit 7 (C64 reads bus state)
- **Implementation:** `IECBus.set_data(device_id, state)`, open-collector logic

## IEC Protocol Commands

### Command Structure
Commands are sent with ATN asserted (low):

| Command Byte | Function                | Device      |
|--------------|-------------------------|-------------|
| 0x20-0x3E    | LISTEN                  | Bits 0-4    |
| 0x3F         | UNLISTEN                | All         |
| 0x40-0x5E    | TALK                    | Bits 0-4    |
| 0x5F         | UNTALK                  | All         |
| 0x60-0x6F    | Secondary (after LISTEN)| Channel 0-15|
| 0xE0-0xEF    | Secondary (after TALK)  | Channel 0-15|

### Example: LOAD"$",8
```
1. C64 asserts ATN (low)
2. C64 sends 0x28 (LISTEN device 8)
3. C64 sends 0x60 (Channel 0 - file data)
4. C64 releases ATN (high)
5. C64 sends filename bytes
6. C64 asserts ATN
7. C64 sends 0x5F (UNTALK)
8. C64 sends 0x48 (TALK device 8)
9. C64 sends 0xE0 (Channel 0)
10. C64 releases ATN
11. 1541 sends directory data
12. C64 asserts ATN, sends 0x5F (UNTALK)
```

## ROM Files Required

### DOS ROM (Required)
- **Size:** 16KB (16384 bytes)
- **Address:** $C000-$FFFF
- **Filenames:**
  - `dos1541` (VICE default)
  - `d1541-325302-01.bin`
  - `325302-01.bin`
  - `dos-1541.bin`
- **Contents:** DOS firmware, disk I/O, BAM management, directory handling

### Serial ROM (Optional)
- **Size:** 8KB (8192 bytes)
- **Address:** $8000-$9FFF
- **Filenames:**
  - `d1541II` (VICE default)
  - `901229-06.bin`
  - `serial-1541.bin`
- **Contents:** IEC protocol, GCR encoding/decoding

## ROM Search Paths

The emulator searches these directories for 1541 ROMs:

### macOS
- `/Applications/VICE.app/Contents/Resources/DRIVES`
- `/opt/homebrew/share/vice/DRIVES`
- `~/Library/Application Support/VICE/DRIVES`

### Linux
- `/usr/local/share/vice/DRIVES`
- `/usr/share/vice/DRIVES`
- `/usr/lib/vice/DRIVES`
- `~/.vice/DRIVES`

### User ROM Directory
- `~/.local/share/c64py/roms` (Linux/macOS)
- `~/Library/Application Support/c64py/roms` (macOS)
- `%APPDATA%\c64py\roms` (Windows)

### Environment Variable
- `$C64PY_ROM_DIR`

## Code Examples

### Initialize IEC Bus
```python
from c64py.emulator import C64

emu = C64()
emu.load_roms(rom_dir)
success = emu.initialize_iec_bus(rom_dir)

if success:
    print("IEC bus ready with 1541 ROM emulation")
else:
    print("Fallback to KERNAL hooks")
```

### Attach Disk to IEC Drive
```python
from c64py.d64 import D64Image

# Load D64 image
d64 = D64Image("game.d64")

# Attach to drive 8 (IEC emulation)
if emu.use_iec_bus:
    drive = emu.iec_drives[8]
    drive.attach_disk(d64, "game.d64")
else:
    # Fallback to simple drive
    emu.attach_disk("game.d64", device=8)
```

### Read IEC Bus State
```python
# C64 side: Read CIA2 Port A
port_a = emu.memory.read(0xDD00)
atn_state = (port_a & 0x08) != 0  # Bit 3
clk_in = (port_a & 0x40) != 0     # Bit 6
data_in = (port_a & 0x80) != 0    # Bit 7

print(f"ATN: {atn_state}, CLK: {clk_in}, DATA: {data_in}")
```

### Control IEC Bus
```python
# C64 side: Write CIA2 Port A
# Assert ATN (bit 3 = 0)
port_a = emu.memory.read(0xDD00)
port_a &= ~0x08  # Clear bit 3
emu.memory.write(0xDD00, port_a)

# This automatically updates IEC bus via CIA2 handler
```

## Next Steps

### 1. Implement Byte-Level Protocol
Add C64-side protocol functions:
```python
def iec_send_byte(bus, byte, eoi=False):
    """Send byte under attention"""
    # Set DATA=1, CLK=0 (ready to send)
    # Wait for device to set DATA=0 (ready to receive)
    # For each bit 7..0:
    #   Set DATA to bit value
    #   Set CLK=1 (clock high)
    #   Wait
    #   Set CLK=0 (clock low)
    # If EOI: Hold CLK high longer
    # Return ACK from device
```

### 2. Connect Drive ROM to D64
Add sector read/write handlers in Drive1541:
```python
def read_sector(self, track, sector):
    """Read sector from D64 image"""
    if self.disk:
        return self.disk.read_sector(track, sector)
    return None

def write_sector(self, track, sector, data):
    """Write sector to D64 image"""
    if self.disk:
        self.disk.write_sector(track, sector, data)
```

### 3. Execute Drive CPU
Add to emulator main loop:
```python
def run(self):
    while running:
        # Execute C64 CPU
        cycles = self.cpu.step()
        
        # Execute drive CPUs (at 1MHz vs C64's ~1MHz)
        if self.use_iec_bus:
            for drive in self.iec_drives.values():
                drive.step(cycles)
```

### 4. Test & Verify
Compare IEC results with KERNAL hook results:
- LOAD"$",8 directory listing
- LOAD"PROGRAM",8 file loading
- Timing accuracy
- Error handling

### 5. Remove KERNAL Hooks
Once IEC is verified, deprecate temporary hooks:
- `_handle_kernal_load()`
- `_handle_kernal_save()`

## Benefits of IEC Emulation

### Authenticity
- Real 1541 DOS ROM behavior
- Authentic error messages
- Proper timing (when implemented)
- Works like actual hardware

### Extensibility
- Easy to add other devices (printers, modems)
- Support for 1571, 1581 drives
- Custom device development

### Accuracy
- No custom BAM code to debug
- ROM handles all DOS operations
- Proven, tested firmware

### Educational
- Learn how IEC protocol works
- Understand drive internals
- See ROM execution in action

## References

- [IEC Protocol Documentation](http://www.zimmers.net/anonftp/pub/cbm/programming/serial-bus.pdf)
- [1541 Hardware Documentation](http://www.zimmers.net/anonftp/pub/cbm/schematics/drives/old/1541/)
- [VICE Emulator Source](https://github.com/VICE-Team/svn-mirror)
- [Commodore Service Manual](http://www.zimmers.net/anonftp/pub/cbm/schematics/drives/old/1541/1541-II_Service_Manual.pdf)

## Conclusion

The IEC serial bus foundation is complete and ready for protocol implementation. The infrastructure supports:
- Multiple drives (8-11)
- ROM-based emulation
- D64 disk images
- Fallback to KERNAL hooks

Next phase: Implement byte-level protocol and test end-to-end LOAD operations.
