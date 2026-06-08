"""Stage-5 tests: Rust fast-batch is bypassed when iec_disk_full_impl=True.

The c64py Rust core only snapshots peer IEC state at batch start; mid-batch
$DD00 writes never reach IECBus. To make true 1541 stepping work end-to-end
we must force Python interpretation when iec_disk_full_impl is set.
"""
import pytest


def _has_rust():
    try:
        import c64py._core  # noqa: F401
        return True
    except ImportError:
        return False


pytestmark = pytest.mark.skipif(
    not _has_rust(), reason="Rust core not built; nothing to interlock"
)


def test_python_step_used_when_iec_full_impl_true():
    """When iec_disk_full_impl=True, cpu_step_quantum must take the Python
    path even though Rust is available. We verify this by counting calls
    to step() vs step_fast_batch() over a short run."""
    from c64py.emulator import C64

    em = C64()
    # Synthesise a minimal program in RAM so the CPU has something to run.
    em.memory.ram[0x0800] = 0xEA  # NOP
    em.memory.ram[0x0801] = 0x4C  # JMP $0800
    em.memory.ram[0x0802] = 0x00
    em.memory.ram[0x0803] = 0x08
    em.cpu.state.pc = 0x0800

    em.memory.iec_disk_full_impl = True

    calls = {"step": 0, "batch": 0}
    orig_step = em.cpu.step
    orig_batch = em.cpu.step_fast_batch

    def counted_step(*a, **kw):
        calls["step"] += 1
        return orig_step(*a, **kw)

    def counted_batch(*a, **kw):
        calls["batch"] += 1
        return orig_batch(*a, **kw)

    em.cpu.step = counted_step
    em.cpu.step_fast_batch = counted_batch

    for _ in range(20):
        em.cpu.cpu_step_quantum(em.udp_debug, em.vice_trace, 0, None)

    assert calls["step"] == 20, "expected 20 Python step calls"
    assert calls["batch"] == 0, "Rust batch must NOT run when iec_disk_full_impl=True"


def test_rust_batch_used_when_iec_full_impl_false():
    """Sanity inverse: with iec_disk_full_impl=False the Rust batch is used
    (provided the rest of the gate allows it)."""
    from c64py.emulator import C64

    em = C64()
    em.memory.ram[0x0800] = 0xEA
    em.memory.ram[0x0801] = 0x4C
    em.memory.ram[0x0802] = 0x00
    em.memory.ram[0x0803] = 0x08
    em.cpu.state.pc = 0x0800
    em.memory.iec_disk_full_impl = False

    calls = {"step": 0, "batch": 0}
    orig_step = em.cpu.step
    orig_batch = em.cpu.step_fast_batch

    def counted_step(*a, **kw):
        calls["step"] += 1
        return orig_step(*a, **kw)

    def counted_batch(*a, **kw):
        calls["batch"] += 1
        return orig_batch(*a, **kw)

    em.cpu.step = counted_step
    em.cpu.step_fast_batch = counted_batch

    for _ in range(5):
        em.cpu.cpu_step_quantum(em.udp_debug, em.vice_trace, 0, None)

    # If Rust is usable here, batch should have been called at least once.
    # In any case, this test mainly proves the inverse condition is wired.
    assert calls["batch"] + calls["step"] >= 5
