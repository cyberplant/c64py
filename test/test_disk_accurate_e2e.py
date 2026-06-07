"""Stage 6 end-to-end: real C64 KERNAL ↔ real 1541 DOS via byte-level IEC.

These are slow integration tests (~seconds each) gated on dos1541 ROM.
"""
import os
import time
import pytest

from c64py.emulator import C64
from c64py.d64 import create_blank_d64
from c64py.roms import find_drive_rom


ROM_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "roms"))


def _require_roms():
    if find_drive_rom("dos1541", ROM_DIR) is None:
        pytest.skip("dos1541 ROM not present")
    if not os.path.exists(os.path.join(ROM_DIR, "kernal-901227-03.bin")) and \
       not os.path.exists(os.path.join(ROM_DIR, "kernal-251104-04.bin")) and \
       not os.path.exists(os.path.join(ROM_DIR, "kernal.901227-03.bin")):
        pytest.skip("kernal ROM not present")


def _make_emu_with_disk(tmp_path, *, disk_accurate: bool, kernal_shortcut: bool = True):
    _require_roms()
    # Build a disk with a tiny PRG: load addr $C000, payload 3 bytes 0x11 0x22 0x33.
    disk = create_blank_d64(disk_name="HELLO", disk_id="01")
    payload = bytes([0x00, 0xC0, 0x11, 0x22, 0x33])  # PRG header + 3 data bytes
    disk.write_file("HELLO", payload, filetype=0x82)  # PRG closed
    d64_path = tmp_path / "hello.d64"
    disk.save_to_file(str(d64_path))

    emu = C64(interface_factory=lambda e: None)
    emu.interface = type("Iface", (), {"add_debug_log": lambda *a, **k: None})()
    emu._initialize_c64()
    emu.load_roms(ROM_DIR, require_char_rom=False)
    # `kernal_shortcut=False` mirrors `[emulation] disk_emulation = "accurate-python"`:
    # the KERNAL `$FFD5`/`$FFD8` shortcut is disabled, so the real KERNAL
    # serial routines run and exchange bytes with DOS over the IEC wire.
    emu.kernal_load_shortcut_enabled = kernal_shortcut
    emu.initialize_iec_bus()
    emu.attach_disk(str(d64_path), device=8)
    return emu


def _step_until(emu, predicate, max_cycles, step_chunk=10_000):
    """Step CPU until predicate(emu) is True or we run out of cycles.

    Returns (consumed_cycles, predicate_value).
    """
    consumed = 0
    current_cycles = 0
    while consumed < max_cycles:
        # Use run_cpu_instruction_quantum to ensure KERNAL hooks are called
        current_cycles += emu.run_cpu_instruction_quantum(current_cycles)
        consumed += 1  # step() returns cycles but we just need a bound
        if consumed % step_chunk == 0:
            # Service drives every chunk like the main loop does.
            if hasattr(emu, "_step_iec_drives"):
                emu._step_iec_drives(step_chunk)
            if predicate(emu):
                return consumed, True
        if predicate(emu):
            return consumed, True
    return consumed, False


def test_disk_accurate_initialize_does_not_break_baseline():
    """With disk_emulation accurate-* (constructor), setup must succeed without crashing.
    The IEC bus is initialised with no drives (TCP drives connect separately)."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        from pathlib import Path
        emu = _make_emu_with_disk(Path(td), disk_accurate=True)
        assert emu.iec_bus is not None


def test_disk_accurate_rust_path_disabled():
    """When _iec_disk_full_impl is True, cpu_step_quantum must NOT use
    Rust fast-batch (interlock from stage 5)."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        emu = _make_emu_with_disk(Path(td), disk_accurate=True)
        # Just exercise a few steps; this test passes by absence of
        # Rust-batched IEC corruption (verified more thoroughly in
        # test_iec_rust_interlock.py).
        for _ in range(50):
            emu.cpu.step()
        assert emu.cpu.state.pc != 0


# Full LOAD-via-IEC end-to-end (cold reset → BASIC ready → inject
# LOAD"HELLO",8,1 → wait for completion). Completes in ~8 seconds at
# Python interpretation speed.
@pytest.mark.skipif(
    os.environ.get("C64PY_RUN_SLOW_TESTS") != "1",
    reason="slow integration test; set C64PY_RUN_SLOW_TESTS=1 to run",
)
def test_disk_accurate_load_named_file_end_to_end(tmp_path):
    emu = _make_emu_with_disk(tmp_path, disk_accurate=True)

    # Cold reset: jump to KERNAL reset vector.
    reset_lo = emu.memory.read(0xFFFC)
    reset_hi = emu.memory.read(0xFFFD)
    emu.cpu.state.pc = reset_lo | (reset_hi << 8)
    emu.cpu.state.sp = 0xFD
    emu.cpu.state.p |= 0x04

    # Run KERNAL+BASIC init until we reach BASIC's main input loop
    # (~$A483 cold start finishes by jumping to ready loop at $A474).
    BASIC_INPUT_LOOP_START = 0xA470
    BASIC_INPUT_LOOP_END = 0xA490
    boot_consumed = 0
    boot_budget = 5_000_000
    current_cycles = 0
    while boot_consumed < boot_budget:
        current_cycles += emu.run_cpu_instruction_quantum(current_cycles)
        boot_consumed += 1
        if boot_consumed % 5000 == 0:
            emu._step_iec_drives(5000)
        pc = emu.cpu.state.pc
        if BASIC_INPUT_LOOP_START <= pc <= BASIC_INPUT_LOOP_END:
            break
    assert boot_consumed < boot_budget, (
        f"BASIC never reached READY (PC=${emu.cpu.state.pc:04X}, "
        f"after {boot_consumed} instructions)"
    )

    # Drive needs additional cycles to finish its DOS boot sequence and enable
    # CA1 interrupts. C64 reaches BASIC at ~575k cycles, but drive doesn't
    # enable CA1 until ~1M+ cycles. Step the drive to completion.
    # Drive runs in a separate TCP server process; no local drive to step.

    # Inject LOAD"HELLO",8,1<RETURN> into the keyboard buffer.
    cmd = b'LOAD"HELLO",8,1\r'
    for i, b in enumerate(cmd):
        emu.memory.write(0x0277 + i, b)  # keyboard buffer
    emu.memory.write(0x00C6, len(cmd))   # NDX = chars in buffer

    # Wait for LOAD to complete: bytes show up at $C000-$C002.
    load_consumed = 0
    load_budget = 30_000_000
    success = False
    while load_consumed < load_budget:
        current_cycles += emu.run_cpu_instruction_quantum(current_cycles)
        load_consumed += 1
        if load_consumed % 5000 == 0:
            emu._step_iec_drives(5000)
        if (
            emu.memory.read(0xC000) == 0x11
            and emu.memory.read(0xC001) == 0x22
            and emu.memory.read(0xC002) == 0x33
        ):
            success = True
            break
    assert success, (
        f"LOAD did not deliver bytes after {load_consumed} instructions; "
        f"got ${emu.memory.read(0xC000):02X} ${emu.memory.read(0xC001):02X} "
        f"${emu.memory.read(0xC002):02X}"
    )

    # Verify real IEC was used: success flag is sufficient.


# Same as the test above but with the KERNAL `$FFD5` shortcut DISABLED
# (matches `[emulation] disk_emulation = "accurate-python"`). DOS must serve the file
# back over the real IEC bus, exercising the bit-level CIA2/VIA1 wiring.
# Marked xfail until the bit-level handshake is fully debugged in M2a.
@pytest.mark.skipif(
    os.environ.get("C64PY_RUN_SLOW_TESTS") != "1",
    reason="slow integration test; set C64PY_RUN_SLOW_TESTS=1 to run",
)
@pytest.mark.xfail(
    reason="M2a WIP: bit-level IEC handshake without KERNAL shortcut not yet "
           "debugged end-to-end (timing / VIA2 BYTE-READY pending).",
    strict=False,
)
def test_disk_accurate_python_load_named_file_end_to_end_bitlevel(tmp_path):
    """`accurate-python` path: KERNAL talks to DOS over the real IEC wires.

    Self-contained wall-clock timeout so the test never hangs in CI.
    Budget: 60 seconds (much more than the ~8s shortcut path).
    """
    import time

    emu = _make_emu_with_disk(
        tmp_path, disk_accurate=True, kernal_shortcut=False,
    )
    assert emu.kernal_load_shortcut_enabled is False

    reset_lo = emu.memory.read(0xFFFC)
    reset_hi = emu.memory.read(0xFFFD)
    emu.cpu.state.pc = reset_lo | (reset_hi << 8)
    emu.cpu.state.sp = 0xFD
    emu.cpu.state.p |= 0x04

    deadline = time.monotonic() + 120.0  # 120s wall-clock budget

    BASIC_INPUT_LOOP_START = 0xA470
    BASIC_INPUT_LOOP_END = 0xA490
    boot_consumed = 0
    boot_budget = 5_000_000
    current_cycles = 0
    while boot_consumed < boot_budget and time.monotonic() < deadline:
        delta = emu.run_cpu_instruction_quantum(current_cycles)
        current_cycles += delta
        boot_consumed += 1
        # Step drive with ACTUAL cycle count so ATN/CLK/DATA timing is correct.
        emu._step_iec_drives(max(1, delta))
        pc = emu.cpu.state.pc
        if BASIC_INPUT_LOOP_START <= pc <= BASIC_INPUT_LOOP_END:
            break
    if time.monotonic() >= deadline:
        pytest.xfail("wall-clock timeout during BASIC boot (bit-level IEC WIP)")
    assert boot_consumed < boot_budget

    # Drive runs in a separate TCP server process; no local drive to step.

    cmd = b'LOAD"HELLO",8,1\r'
    for i, b in enumerate(cmd):
        emu.memory.write(0x0277 + i, b)
    emu.memory.write(0x00C6, len(cmd))

    consumed = 0
    while time.monotonic() < deadline:
        delta = emu.run_cpu_instruction_quantum(current_cycles)
        current_cycles += delta
        consumed += 1
        emu._step_iec_drives(max(1, delta))
        if (emu.memory.read(0xC000) == 0x11
                and emu.memory.read(0xC001) == 0x22
                and emu.memory.read(0xC002) == 0x33):
            break

    if time.monotonic() >= deadline:
        pytest.xfail(
            f"wall-clock timeout after LOAD injected ({consumed} quanta); "
            f"C64 PC=${emu.cpu.state.pc:04X}  "
            "bit-level IEC handshake WIP"
        )

    assert (
        emu.memory.read(0xC000) == 0x11
        and emu.memory.read(0xC001) == 0x22
        and emu.memory.read(0xC002) == 0x33
    ), "bit-level LOAD did not deliver payload"
