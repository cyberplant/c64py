# Emulator snapshots

`c64py` can save and restore a full-state snapshot of the emulator so that a
game (or debug session) can be resumed without re-running BASIC boot, PRG
autoload and any in-game loader. Typical use case: play or trace from the
post-title state of a tape/disk game without waiting for the loader every time.

The round-trip is **byte-exact** through the full Rust accurate-VIC path:
loading a snapshot taken at cycle `N` and running to cycle `M` produces the
same CPU state and the same 64 KiB RAM as running continuously from reset to
cycle `M`. See `test/test_snapshot.py::test_resume_parity_matches_continuous_run`.

## Command-line flags

```text
--save-snapshot-at-cycle CYCLE[:PATH]    save once cumulative cpu_cycles >= CYCLE
--save-snapshot-at-exit [PATH]           save on graceful exit (max-cycles/KIL/stuck)
--load-snapshot PATH                     resume from PATH; skips BASIC boot and autoload
```

When `PATH` is omitted, the emulator writes to
`snapshots/<basename>_<kind>_<cycle>.snap` where `<basename>` is derived from
the `--media-file` / positional PRG / D64 argument.

### Examples

Save after the KERNAL finishes booting and use that as the baseline for faster
iteration:

```bash
# First run: boot + record snapshot at cycle 5_000_000.
python -m c64py.C64 --headless --turbo \
    --max-cycles 5000000 \
    --save-snapshot-at-exit snapshots/boot_ready.snap

# Subsequent runs start from the saved state (no 5M-cycle warm-up).
python -m c64py.C64 --graphics \
    --load-snapshot snapshots/boot_ready.snap \
    programs/your_game.prg
```

Skip a game's own loader by stopping at a post-load cycle and snapshotting:

```bash
python -m c64py.C64 --headless --turbo programs/your_game.prg \
    --max-cycles 70000000 \
    --save-snapshot-at-cycle 68000000:snapshots/post_loader.snap
```

Then iterate on the emulator fix and re-test from the post-loader state:

```bash
python -m c64py.C64 --graphics \
    --load-snapshot snapshots/post_loader.snap
```

## Interactive keybinding (pygame)

When running with `--graphics`, press **Alt + S** to write a snapshot of the
current state to `snapshots/manual_cycle_<N>.snap` where `<N>` is the cycle
count at the time the key is pressed. The snapshot is written by the CPU
thread between instructions to avoid races with the shared RAM / VIC register
bytearrays that the Rust core holds references to.

## What is captured

| Area | Captured | Notes |
| --- | :---: | --- |
| CPU registers (PC/A/X/Y/SP/P, cycles, stopped, jiffy) | yes | Full `CPUState`. |
| Full 64 KiB RAM (`$0000–$FFFF`) | yes | Includes color RAM, I/O shadow, PRG image. |
| VIC-II register shadow (`$D000–$D03F`) | yes | 64 bytes. |
| Raster / badline / DEN-latched / YSCROLL latched | yes | From `MemoryMap`. |
| VIC-II cycle engine (PAL/NTSC table state, sprite mask, IRQ latch, collisions) | yes | All 16 fields that Rust `run_fast_batch` reads and writes. |
| CIA1 timers A/B (latch, counter, running, IRQ enable, one-shot, input mode) + ICR | yes |  |
| CIA2 port A value + DDR | yes | Sufficient for VIC bank selection on resume. |
| IEC bus / 1541 disk state | **no** | Resume is intended for post-LOAD play, not mid-I/O. |
| BASIC / KERNAL / CHAR ROMs | **no** | Loaded from `--rom-dir` as usual at startup. |
| SID internal pipeline (reSID) | **no** | Audio may glitch briefly; register shadow is restored via RAM. |
| Beam render buffers, pygame window, UDP/TCP sockets, trace files | **no** | UI/IO attachments; re-attach on demand. |

The Rust fast-batch core keeps no state of its own between calls — each
invocation reads Python state in and writes it back — so restoring the Python
side is enough to resume execution on the Rust hot path too.

## Format

Pickle (`pickle.HIGHEST_PROTOCOL`) of a flat `dict` with a magic marker and a
version integer. See [`snapshot.py`](../snapshot.py):

```python
{
    "magic":   "C64PY-SNAP",
    "version": 1,
    "note":    "<free-form message>",
    "emulator": {"current_cycles": ..., "vic_emulation": "fast|accurate-python|accurate-rust"},
    "cpu":      {"pc": ..., "a": ..., "x": ..., "y": ..., "sp": ..., "p": ...,
                 "cycles": ..., "stopped": ..., "jiffy_clock": ...},
    "memory":   {"ram": <65536 bytes>, "vic_regs": <64 bytes>, ...},
    "vic_engine": {"raster_line": ..., "cycles_per_line": ..., ... 16 fields},
}
```

This is an internal debug/dev tool, not a stable wire format: do not load
snapshots written by a different c64py version, and never load snapshots from
untrusted sources (pickle is code execution). The loader validates the magic
string and version integer and raises `SnapshotError` on mismatch.

## Python API

```python
from c64py.snapshot import save_snapshot, load_snapshot

# Save at an arbitrary moment from your own code.
save_snapshot(emu, "snapshots/mid.snap", note="right after loader RTS")

# Resume into an existing Emulator instance (same ROM set must already be loaded).
load_snapshot(emu, "snapshots/mid.snap")
emu.run(max_cycles=emu.current_cycles + 5_000_000)  # absolute now
```

`Emulator.save_snapshot` / `Emulator.load_snapshot` are thin wrappers that also
log through the Textual / pygame debug panel. For asynchronous requests from
a UI thread, use `Emulator.request_runtime_snapshot(path)`; the CPU thread will
service the write between instructions inside `run()`.
