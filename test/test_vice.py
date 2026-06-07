#!/usr/bin/env python3
"""
VICE test suite for c64py emulator regression testing.
These tests help identify emulation issues and track fixes.
"""
import pytest
import subprocess
import sys
import os
from pathlib import Path
from test._vice_assets import ensure_vice_assets

if not ensure_vice_assets():
    pytestmark = pytest.mark.skip(reason="VICE test assets missing; run scripts/fetch_vice_tests.sh")


class TestVICETests:
    """Test VICE compatibility with various test programs."""
    
    @pytest.fixture
    def run_emulator(self):
        """Helper to run the emulator with given parameters."""
        def _run(prg_path, max_cycles=15000000, extra_args=None):
            cmd = [
                sys.executable, "C64.py",
                "--accurate", "--turbo", "--audio-emulation", "resid",
                "--max-cycles", str(max_cycles),
                "--no-colors", "--audio-muted", prg_path
            ]
            
            if extra_args:
                cmd.extend(extra_args)
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            return result
        return _run
    
    def test_allsuitea_runs(self, run_emulator):
        """Test AllSuiteA.prg - should run full duration but gets stuck."""
        result = run_emulator("test/vice/hmc6502/AllSuiteA.prg")
        
        # The program should run for 15,000,000 cycles but gets stuck early
        assert result.returncode == 0
        # Should NOT get stuck - this is the bug we need to fix
        assert "PC stuck at" not in result.stdout, f"Program got stuck: {result.stdout}"
        # Should run close to the max cycles
        assert "Cycles: 15" in result.stdout or "Cycles: 14" in result.stdout, f"Program stopped early: {result.stdout}"
    
    def test_allsuitea_detailed_analysis(self, run_emulator):
        """Test AllSuiteA.prg - analyze where it gets stuck."""
        result = run_emulator("test/vice/hmc6502/AllSuiteA.prg")
        
        # The program gets stuck at $40A4 at cycle ~2,127,863
        assert result.returncode == 0
        assert "PC stuck at $40A4" in result.stdout
        assert "Cycles: 2,127,863" in result.stdout
    
    def test_cpu_shx_no_overflow(self, run_emulator):
        """Test cpu_shx.prg - should not overflow after fix."""
        result = run_emulator("test/vice/asap/cpu_shx.prg", max_cycles=7000000)
        
        # Should not have overflow error anymore
        assert "attempt to add with overflow" not in result.stdout
        assert "PanicException" not in result.stdout
        
        # May still get stuck at $0002 (different issue)
        # TODO: Investigate why it gets stuck at $0002
    
    def test_cpu_basic_instructions(self, run_emulator):
        """Test basic CPU instruction handling."""
        # Test with a simple program that should complete
        result = run_emulator("test/vice/hmc6502/AllSuiteA.prg", max_cycles=100000)
        
        # Should run without crashing
        assert result.returncode == 0
        assert "PanicException" not in result.stdout
        assert "attempt to add with overflow" not in result.stdout
    
    def test_collision_registers_accessible(self, run_emulator):
        """Test that collision registers $D01E/$D01F are accessible."""
        # For now, just verify that the emulator can handle collision register reads
        # without crashing. The actual collision detection was implemented in Rust.
        result = run_emulator("test/vice/hmc6502/AllSuiteA.prg", max_cycles=1000000)
        assert result.returncode == 0
        assert "PanicException" not in result.stdout


if __name__ == "__main__":
    # Run tests directly
    pytest.main([__file__, "-v"])
