#!/usr/bin/env python3
"""
Test script to demonstrate disk drive support.

This script shows:
1. D64 parsing
2. Disk attachment to emulator
3. Directory listing
4. Server commands (ATTACH-DISK, DETACH-DISKS)
"""

import sys
import os

# Change to parent directory for proper package import
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from c64py.d64 import load_d64
from c64py.drive import DiskDrive
from c64py.emulator import C64
from c64py.server import EmulatorServer

def test_d64_parsing():
    """Test D64 file parsing"""
    print("=" * 60)
    print("TEST 1: D64 Parsing")
    print("=" * 60)
    
    import tempfile
    d64_path = os.path.join(tempfile.gettempdir(), "test-disk.d64")
    if not os.path.exists(d64_path):
        print(f"❌ Test D64 not found at {d64_path}")
        print("   Run the create_test_d64.py script first")
        return False
    
    d64 = load_d64(d64_path)
    disk_name, disk_id = d64.read_bam()
    print(f"✅ Loaded D64: '{disk_name}' (ID: {disk_id})")
    
    entries = d64.read_directory()
    print(f"✅ Found {len(entries)} file(s):")
    for entry in entries:
        print(f"   - {entry.filename} ({entry.blocks} blocks, type {entry.filetype})")
    
    listing = d64.format_directory_listing()
    print("\n📂 Directory Listing:")
    print(listing)
    print()
    
    return True

def test_disk_drive():
    """Test disk drive module"""
    print("=" * 60)
    print("TEST 2: Disk Drive")
    print("=" * 60)
    
    import tempfile
    d64_path = os.path.join(tempfile.gettempdir(), "test-disk.d64")
    d64 = load_d64(d64_path)
    
    drive = DiskDrive(device_number=8)
    print(f"✅ Created drive {drive.device_number}")
    
    drive.attach_disk(d64, d64_path)
    print(f"✅ Disk attached")
    print(f"   Status: {drive.get_status()}")
    
    # Load directory
    dir_data = drive.load_file("$")
    print(f"✅ Loaded directory: {len(dir_data)} bytes")
    print(f"   Load address: ${dir_data[0] | (dir_data[1] << 8):04X}")
    
    drive.detach_disk()
    print(f"✅ Disk detached")
    print()
    
    return True

def test_emulator_integration():
    """Test emulator integration"""
    print("=" * 60)
    print("TEST 3: Emulator Integration")
    print("=" * 60)
    
    # Create emulator (no UI)
    emu = C64(interface_factory=lambda e: None)
    emu.interface = type('obj', (object,), {'add_debug_log': lambda *args: None})()
    print("✅ Created emulator")
    
    # Attach disk
    import tempfile
    d64_path = os.path.join(tempfile.gettempdir(), "test-disk.d64")
    emu.attach_disk(d64_path, device=8)
    print(f"✅ Attached disk to drive 8")
    
    # Check drive
    drive = emu.get_drive(8)
    if drive and drive.has_disk():
        print(f"✅ Drive 8 has disk attached")
        print(f"   Status: {drive.get_status()}")
        
        # Load directory
        dir_data = drive.load_file("$")
        print(f"✅ Can load directory: {len(dir_data)} bytes")
    else:
        print(f"❌ Drive 8 does not have disk")
        return False
    
    # Detach disks
    emu.detach_disks()
    print(f"✅ Detached all disks")
    print()
    
    return True

def test_server_commands():
    """Test server commands"""
    print("=" * 60)
    print("TEST 4: Server Commands")
    print("=" * 60)
    
    # Create emulator
    emu = C64(interface_factory=lambda e: None)
    emu.interface = type('obj', (object,), {'add_debug_log': lambda *args: None})()
    
    # Create server
    server = EmulatorServer(emu, tcp_port=None, udp_port=None)
    print("✅ Created server")
    
    # Test ATTACH-DISK command
    import tempfile
    d64_path = os.path.join(tempfile.gettempdir(), "test-disk.d64")
    response = server._handle_command(f"ATTACH-DISK {d64_path} 8")
    print(f"✅ ATTACH-DISK response: {response}")
    
    if 8 not in emu.drives:
        print("❌ Disk not attached")
        return False
    
    # Test DETACH-DISKS command
    response = server._handle_command("DETACH-DISKS")
    print(f"✅ DETACH-DISKS response: {response}")
    
    if len(emu.drives) != 0:
        print(f"❌ Disks not detached (have {len(emu.drives)})")
        return False
    
    print()
    return True

def main():
    """Run all tests"""
    print("\n" + "=" * 60)
    print("C64PY DISK DRIVE SUPPORT TEST SUITE")
    print("=" * 60 + "\n")
    
    tests = [
        test_d64_parsing,
        test_disk_drive,
        test_emulator_integration,
        test_server_commands,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"❌ Test failed with exception: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    
    print("=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("=" * 60)
    
    return failed == 0

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
