# Disk Drive Support Implementation

This implementation adds basic D64 disk image support to the C64 emulator.

## What's Implemented

### D64 File Format Parser (`d64.py`)
- Parses D64 disk image format (35-track 1541 disk images)
- Reads Block Availability Map (BAM) for disk name and ID
- Parses directory entries
- Reads file data from disk
- Formats directory listings as C64 would display them

### Disk Drive Emulation (`drive.py`)
- Emulates Commodore 1541 disk drive
- Supports attaching/detaching D64 disk images
- Can load directory as a BASIC program (LOAD"$",8 format)
- Provides drive status information
- Supports multiple drives (devices 8-11)

### CLI Integration (`C64.py`)
- New `--disk <file.d64>` option to attach a disk on startup
- Automatically injects `LOAD"$",8` into keyboard buffer after BASIC boots
- Shows disk attachment status in debug logs

### Server Commands (`server.py`)
- `ATTACH-DISK <file.d64> [device]` - Attach a D64 image (device 8-11, default 8)
- `DETACH-DISKS` - Detach all disk images
- Updated HELP command with new disk commands

### Emulator Integration (`emulator.py`)
- Disk drive instance management (supports devices 8-11)
- Methods to attach/detach disks programmatically
- Auto-injection of LOAD"$",8 command when disk is attached via CLI

## What's NOT Implemented (Future Work)

### IEC Serial Bus Emulation
The current implementation does **not** emulate the IEC serial bus that the C64 uses to communicate with the 1541 drive. This means:

- `LOAD"$",8` command will be injected into the keyboard buffer
- BASIC will try to execute it
- However, the actual I/O operations will not work without serial bus emulation
- The directory data is prepared but cannot be transferred via the normal KERNAL routines

### To Make It Fully Functional

To make disk operations work end-to-end, one of these approaches would be needed:

1. **Full IEC Bus Emulation**
   - Implement CIA2 port manipulation for serial bus
   - Emulate 1541 drive protocol (ATN, DATA, CLOCK lines)
   - Handle TALK/LISTEN/UNLISTEN commands
   - This is the most accurate but most complex approach

2. **KERNAL Hook Interception**
   - Intercept calls to LOAD ($FFD5) in the KERNAL
   - Check if device is 8-11 (disk drive)
   - Bypass normal I/O and load directly from virtual disk
   - Simpler but less accurate to real hardware

3. **Fast Load Cartridge Simulation**
   - Many C64 fast loaders bypass KERNAL I/O
   - Could implement a simplified fast loader
   - Would require custom ROM or memory hooks

## Usage Examples

### Command Line
```bash
# Attach a disk on startup
python3 C64.py --disk test-disk.d64

# The emulator will:
# 1. Boot BASIC
# 2. Attach the disk to drive 8
# 3. Inject LOAD"$",8 into keyboard buffer
```

### TCP Server
```bash
# Start emulator with TCP server
python3 C64.py --tcp-port 6400

# In another terminal, use netcat to send commands:
echo "ATTACH-DISK test-disk.d64 8" | nc localhost 6400
echo "DETACH-DISKS" | nc localhost 6400
```

### Programmatic
```python
from c64py.emulator import C64
from c64py.d64 import load_d64

# Create emulator
emu = C64()

# Attach disk
emu.attach_disk("test-disk.d64", device=8)

# Get drive and check status
drive = emu.get_drive(8)
if drive.has_disk():
    # Load directory
    dir_data = drive.load_file("$")
    print(f"Directory: {len(dir_data)} bytes")

# Detach disks
emu.detach_disks()
```

## Creating Test D64 Images

See `test/create_test_d64.py` (if exists) or use VICE's `c1541` tool:

```bash
# Create empty disk
c1541 -format "disk name,id" d64 test.d64

# Add files
c1541 test.d64 -write program.prg "PROGRAM NAME"

# List directory
c1541 test.d64 -list
```

## Testing

Run the integration test suite:
```bash
python3 test_disk_integration.py
```

This tests:
- D64 parsing
- Disk drive operations
- Emulator integration
- Server commands

## Limitations

- No actual serial bus emulation (LOAD won't transfer data to C64 memory)
- No SAVE support (would require write operations to D64)
- No error channel handling
- No support for other file types (SEQ, REL, etc.)
- Directory is formatted but cannot be displayed via BASIC LIST

## Future Enhancements

1. Implement IEC serial bus emulation
2. Add SAVE support (write to D64)
3. Support for other disk formats (G64, D71, D81)
4. Fast loader implementation
5. Multiple concurrent drives
6. Disk change detection
7. Error channel emulation
8. Support for REL files
