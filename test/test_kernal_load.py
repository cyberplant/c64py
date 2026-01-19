#!/usr/bin/env python3
"""
Test KERNAL LOAD hook functionality
"""

import sys
import os

# Change to parent directory for proper package import
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from c64py.emulator import C64
from c64py.d64 import load_d64


def test_kernal_load_directory():
    """Test loading directory via KERNAL hook"""
    print("=" * 60)
    print("TEST: KERNAL LOAD Directory")
    print("=" * 60)
    
    # Create emulator
    emu = C64(interface_factory=lambda e: None)
    emu.interface = type('obj', (object,), {'add_debug_log': lambda *args: None})()
    emu._initialize_c64()
    
    # Attach disk
    disk_path = os.path.join(os.path.dirname(__file__), '..', 'programs', 'programs.d64')
    if not os.path.exists(disk_path):
        print(f"❌ Test disk not found: {disk_path}")
        return False
    
    emu.attach_disk(disk_path, device=8)
    
    # Set up LOAD parameters for LOAD"$",8
    emu.memory.write(0xBA, 8)  # Device 8
    emu.memory.write(0xB9, 1)  # Secondary address 1 (use file address)
    emu.memory.write(0xB7, 1)  # Filename length = 1 byte
    emu.memory.write(0xBB, 0x00)  # Filename pointer low = $1000
    emu.memory.write(0xBC, 0x10)  # Filename pointer high
    emu.memory.write(0x1000, ord('$'))  # Filename = "$"
    
    # Set CPU state for LOAD
    emu.cpu.state.pc = 0xFFD5  # KERNAL LOAD entry
    emu.cpu.state.a = 0  # LOAD (not VERIFY)
    emu.cpu.state.sp = 0xFD
    
    # Push fake return address
    emu.memory.write(0x01FE, 0x99)
    emu.memory.write(0x01FF, 0x99)
    
    # Call handler
    result = emu._handle_kernal_load()
    
    if not result:
        print("❌ LOAD not handled")
        return False
    
    # Check results
    end_addr = emu.cpu.state.x | (emu.cpu.state.y << 8)
    print(f"✅ Directory loaded at $0801-${end_addr:04X}")
    
    # Verify carry flag is clear (success)
    if emu.cpu.state.p & 0x01:
        print("❌ Carry flag set (error)")
        return False
    
    print("✅ Carry flag clear (success)")
    
    # Check that PC was updated
    if emu.cpu.state.pc != 0x999A:
        print(f"✅ PC updated to ${emu.cpu.state.pc:04X}")
    
    # Check that BASIC pointers were updated
    basic_end = emu.memory.read(0x002D) | (emu.memory.read(0x002E) << 8)
    print(f"✅ BASIC end pointer: ${basic_end:04X}")
    
    if basic_end != end_addr:
        print(f"❌ BASIC pointer mismatch")
        return False
    
    # Check first bytes (should be link to next line)
    link_low = emu.memory.read(0x0801)
    link_high = emu.memory.read(0x0802)
    link_addr = link_low | (link_high << 8)
    
    if link_addr != 0:
        print(f"✅ Directory format valid (link: ${link_addr:04X})")
    
    print("✅ KERNAL LOAD directory test PASSED\n")
    return True


def test_kernal_load_nonexistent():
    """Test loading non-existent file"""
    print("=" * 60)
    print("TEST: KERNAL LOAD Non-existent File")
    print("=" * 60)
    
    # Create emulator
    emu = C64(interface_factory=lambda e: None)
    emu.interface = type('obj', (object,), {'add_debug_log': lambda *args: None})()
    emu._initialize_c64()
    
    # Attach disk
    disk_path = os.path.join(os.path.dirname(__file__), '..', 'programs', 'programs.d64')
    emu.attach_disk(disk_path, device=8)
    
    # Set up LOAD parameters for non-existent file
    emu.memory.write(0xBA, 8)
    emu.memory.write(0xB9, 1)
    emu.memory.write(0xB7, 8)  # 8 bytes
    emu.memory.write(0xBB, 0x00)
    emu.memory.write(0xBC, 0x10)
    
    # Filename = "NOTFOUND"
    for i, ch in enumerate("NOTFOUND"):
        emu.memory.write(0x1000 + i, ord(ch))
    
    # Set CPU state
    emu.cpu.state.pc = 0xFFD5
    emu.cpu.state.a = 0
    emu.cpu.state.sp = 0xFD
    emu.memory.write(0x01FE, 0x99)
    emu.memory.write(0x01FF, 0x99)
    
    # Call handler
    result = emu._handle_kernal_load()
    
    if not result:
        print("❌ LOAD not handled")
        return False
    
    # Check that carry flag is SET (error)
    if not (emu.cpu.state.p & 0x01):
        print("❌ Carry flag not set (should indicate error)")
        return False
    
    print("✅ Carry flag set (error correctly indicated)")
    
    # Check status byte
    status = emu.memory.read(0x90)
    if status & 0x40:
        print(f"✅ Status byte indicates error (${status:02X})")
    
    print("✅ KERNAL LOAD error handling test PASSED\n")
    return True


def test_kernal_load_with_address():
    """Test loading with specific address"""
    print("=" * 60)
    print("TEST: KERNAL LOAD with Address")
    print("=" * 60)
    
    # Create emulator
    emu = C64(interface_factory=lambda e: None)
    emu.interface = type('obj', (object,), {'add_debug_log': lambda *args: None})()
    emu._initialize_c64()
    
    # Attach disk
    disk_path = os.path.join(os.path.dirname(__file__), '..', 'programs', 'programs.d64')
    emu.attach_disk(disk_path, device=8)
    
    # Set up LOAD parameters
    emu.memory.write(0xBA, 8)
    emu.memory.write(0xB9, 0)  # Secondary 0 = use X/Y address
    emu.memory.write(0xB7, 1)
    emu.memory.write(0xBB, 0x00)
    emu.memory.write(0xBC, 0x10)
    emu.memory.write(0x1000, ord('$'))
    
    # Set load address in X/Y
    emu.cpu.state.pc = 0xFFD5
    emu.cpu.state.a = 0
    emu.cpu.state.x = 0x00  # Load to $C000
    emu.cpu.state.y = 0xC0
    emu.cpu.state.sp = 0xFD
    emu.memory.write(0x01FE, 0x99)
    emu.memory.write(0x01FF, 0x99)
    
    # Call handler
    result = emu._handle_kernal_load()
    
    if not result:
        print("❌ LOAD not handled")
        return False
    
    # Check that data was loaded at $C000
    end_addr = emu.cpu.state.x | (emu.cpu.state.y << 8)
    
    if end_addr <= 0xC000:
        print("❌ No data loaded at $C000")
        return False
    
    print(f"✅ Data loaded at $C000-${end_addr:04X}")
    
    # Verify some data exists at $C000
    first_bytes = [emu.memory.read(0xC000 + i) for i in range(4)]
    if any(b != 0 for b in first_bytes):
        print(f"✅ Data present at $C000: {[f'${b:02X}' for b in first_bytes]}")
    
    print("✅ KERNAL LOAD with address test PASSED\n")
    return True


def main():
    """Run all tests"""
    print("\n" + "=" * 60)
    print("KERNAL LOAD HOOK TEST SUITE")
    print("=" * 60 + "\n")
    
    tests = [
        test_kernal_load_directory,
        test_kernal_load_nonexistent,
        test_kernal_load_with_address,
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
