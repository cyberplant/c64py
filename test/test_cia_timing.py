#!/usr/bin/env python3
"""
Test CIA timer timing to debug the AllSuiteA stuck issue.
"""

import subprocess
import sys

def test_cia_timer():
    """Test CIA timer behavior with AllSuiteA"""
    cmd = [
        sys.executable, "C64.py",
        "--accurate", "--turbo", "--audio-emulation", "resid",
        "--max-cycles", "2128864",  # Stop just before the stuck point
        "--vice-trace", "logs/cia_timing_test.trace",
        "--interface", "headless",
        "test/vice/hmc6502/AllSuiteA.prg"
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    print("=== CIA Timer Test ===")
    print(f"Return code: {result.returncode}")
    print(f"Cycles: {result.stdout.split('Cycles:')[1].split()[0] if 'Cycles:' in result.stdout else 'N/A'}")
    
    # Check if it got stuck
    if "PC stuck at" in result.stdout:
        print("❌ Got stuck")
    else:
        print("✅ Did not get stuck")
    
    return result

if __name__ == "__main__":
    test_cia_timer()
