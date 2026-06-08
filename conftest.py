"""Pytest configuration and hooks."""
import pytest

# Quick test subset - a small set of tests for fast feedback
# These are also included in the full suite (no duplication)
QUICK_TESTS = {
    # Tests that currently FAIL - track progress on fixing them
    "test/vice/hmc6502/AllSuiteA.prg",      # Known to get stuck at $40A4
    "test/vice/asap/cpu_shx.prg",            # Fixed overflow but may have other issues
    "test/vice/cpujam/cpujam32.prg",         # CPU jam test
    "test/vice/decimalmode/isc00.prg",       # Decimal mode test
    "test/vice/64doc/dadc.prg",              # 64doc test
    "test/vice/Acid800/cpu_flags.prg",       # Acid800 test
    # Tests that should PASS (witness tests)
    "test/vice/ane-lax/dumps/dump6510_4782-coldstart/alresult1.prg",
    "test/vice/ane-lax/dumps/dump6510_4782-coldstart/alresult2.prg",
}


def pytest_collection_modifyitems(config, items):
    """Add 'quick' marker to tests that are in the QUICK_TESTS set."""
    quick_mark = pytest.mark.quick
    for item in items:
        # Check if this test's prg_file parameter matches a quick test
        if hasattr(item, 'callspec') and 'prg_file' in item.callspec.params:
            prg_path = str(item.callspec.params['prg_file'])
            if prg_path in QUICK_TESTS:
                item.add_marker(quick_mark)
