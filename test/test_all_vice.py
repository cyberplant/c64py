#!/usr/bin/env python3
"""
Comprehensive test suite for all VICE test programs.
Tests all PRG files in the test/vice directory with proper failure criteria.
"""
import pytest
import subprocess
import sys
import os
import time
from pathlib import Path
from datetime import datetime
import json
from test._vice_assets import ensure_vice_assets


# Find all PRG files at module level (needed for pytest parametrize)
def find_all_prg_files():
    """Find all PRG files in the test/vice directory."""
    ensure_vice_assets()
    vice_dir = Path("test/vice")
    prg_files = []
    
    for prg_file in vice_dir.rglob("*.prg"):
        # Skip some known problematic files for now
        if any(skip in str(prg_file) for skip in [
            "none",  # Skip "none" variants
            "timing_ntsc",  # Skip NTSC timing tests for now
        ]):
            continue
        prg_files.append(prg_file)
    
    return sorted(prg_files)

# Get all PRG files for parametrize
ALL_PRG_FILES = find_all_prg_files()


class TestAllVICE:
    """Test all VICE compatibility programs."""
    
    # Global configuration
    DEFAULT_MAX_CYCLES = 5_000_000
    TIMEOUT = 60  # seconds per test
    
    @pytest.fixture(scope="class")
    def test_results_log(self, request):
        """Create a log file to track test results."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Add worker ID for parallel execution
        worker_id = getattr(request.config, 'workerinput', {}).get('workerid', '')
        if worker_id:
            log_file = f"logs/vice_test_results_{timestamp}_worker{worker_id}.json"
        else:
            log_file = f"logs/vice_test_results_{timestamp}.json"
        
        # Ensure logs directory exists
        os.makedirs("logs", exist_ok=True)
        
        results = {
            "timestamp": timestamp,
            "max_cycles": self.DEFAULT_MAX_CYCLES,
            "worker_id": worker_id,
            "tests": {}
        }
        
        yield results, log_file
        
        # Save results at the end
        with open(log_file, 'w') as f:
            json.dump(results, f, indent=2)
        print(f"\nTest results saved to: {log_file}")
    
    @pytest.fixture
    def run_emulator(self):
        """Helper to run the emulator with given parameters."""
        def _run(prg_path, max_cycles=None, extra_args=None):
            if max_cycles is None:
                max_cycles = self.DEFAULT_MAX_CYCLES
                
            cmd = [
                sys.executable, "C64.py",
                "--accurate", "--turbo", "--audio-emulation", "resid",
                "--max-cycles", str(max_cycles),
                "--no-colors", "--interface", "headless",
                "--audio-muted",
                prg_path
            ]
            
            if extra_args:
                cmd.extend(extra_args)
            
            start_time = time.time()
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.TIMEOUT
            )
            elapsed = time.time() - start_time
            
            # Parse results
            cycles = None
            if "Cycles:" in result.stdout:
                for line in result.stdout.split('\n'):
                    if "Cycles:" in line:
                        try:
                            cycles_str = line.split("Cycles:")[1].strip()
                            cycles = int(cycles_str.replace(',', ''))
                        except:
                            pass
            
            return {
                "result": result,
                "cycles": cycles,
                "elapsed": elapsed,
                "got_stuck": "PC stuck at" in result.stdout,
                "cpu_stopped": "CPU stopped" in result.stdout,
                "overflow_error": "attempt to add with overflow" in result.stdout,
                "panic": "PanicException" in result.stdout
            }
        return _run
    
    @pytest.mark.parametrize("prg_file", ALL_PRG_FILES, ids=lambda x: os.path.relpath(x))
    def test_vice_program(self, run_emulator, prg_file, test_results_log):
        """Test a single VICE program."""
        results, log_file = test_results_log
        
        # Convert to relative path for display
        prg_rel_path = os.path.relpath(prg_file)
        print(f"\nTesting: {prg_rel_path}")
        
        # Run the test
        test_result = run_emulator(str(prg_file))
        
        # Store results for logging
        test_info = {
            "path": prg_rel_path,
            "cycles": test_result["cycles"],
            "elapsed": test_result["elapsed"],
            "got_stuck": test_result["got_stuck"],
            "cpu_stopped": test_result["cpu_stopped"],
            "overflow_error": test_result["overflow_error"],
            "panic": test_result["panic"],
            "success": False,
            "failure_reason": None
        }
        
        # Success criteria: should run close to max_cycles (within 10%)
        expected_min_cycles = self.DEFAULT_MAX_CYCLES * 0.9
        
        # Check for immediate failures
        if test_result["result"].returncode != 0:
            test_info["failure_reason"] = f"Process failed with return code {test_result['result'].returncode}"
            results["tests"][prg_rel_path] = test_info
            pytest.fail(f"Process failed: {test_info['failure_reason']}")
        
        # Check for overflow errors (should not happen)
        if test_result["overflow_error"]:
            test_info["failure_reason"] = "CPU overflow error occurred"
            results["tests"][prg_rel_path] = test_info
            pytest.fail("CPU overflow error detected")
        
        # Check for panic (should not happen)
        if test_result["panic"]:
            test_info["failure_reason"] = "Panic exception occurred"
            results["tests"][prg_rel_path] = test_info
            pytest.fail("Panic exception detected")
        
        # Check if got stuck (this is the main issue we're trying to fix)
        if test_result["got_stuck"] or test_result["cpu_stopped"]:
            test_info["failure_reason"] = f"Program stopped early at cycle {test_result['cycles']}"
            results["tests"][prg_rel_path] = test_info
            pytest.fail(f"Program got stuck at cycle {test_result['cycles']} (expected ~{self.DEFAULT_MAX_CYCLES})")
        
        # Check if ran for sufficient cycles
        if test_result["cycles"] is None:
            test_info["failure_reason"] = "Could not parse cycle count"
            results["tests"][prg_rel_path] = test_info
            pytest.fail("Could not determine cycle count from output")
        
        if test_result["cycles"] < expected_min_cycles:
            test_info["failure_reason"] = f"Program stopped early: {test_result['cycles']} < {expected_min_cycles}"
            results["tests"][prg_rel_path] = test_info
            pytest.fail(f"Program stopped early: {test_result['cycles']} cycles (expected > {expected_min_cycles})")
        
        # If we get here, the test passed
        test_info["success"] = True
        results["tests"][prg_rel_path] = test_info
        print(f"✅ PASS: Ran for {test_result['cycles']} cycles")
    


if __name__ == "__main__":
    # Run tests directly
    pytest.main([__file__, "-v", "--tb=short"])
