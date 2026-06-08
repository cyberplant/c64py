# Emulator performance (c64py)

This document summarizes **where time is spent** today and reasonable **directions for improvement**, to guide future work without giving up optional accuracy (accurate VIC, traces, etc.). See [emulation_modes.md](emulation_modes.md) for fast vs accurate VIC and audio lockstep behavior.

## Summary

- The main loop is **`C64.run()` → `cpu.step()`**, once per 6502 instruction.
- With **`accurate_vic`**, every **CPU cycle** also advances the VIC model (`ViciiCycleEngine.tick()`), the SID, and the CIAs: it is the most expensive mode but needed to match VICE on IRQ/badlines in difficult cases.
- Without accurate VIC, the raster advances in **bursts** (`_advance_raster`), much cheaper but less faithful.
- **`MemoryMap.read` / `write`** hold 6510 banking, I/O, optional debug hooks, and (on hot paths) many branches.

## Relevant components

| Area | Files / notes |
|------|---------------|
| CPU | `cpu.py`: `step()` with a long `if/elif` chain per opcode; each instruction calls `_mr`/`_mw` several times. |
| Memory | `memory.py`: `read`/`write` with range decoding; flags/env vars enable costly logging on hot paths. |
| VIC | `vicii_cycle.py` + integration in `cpu.py` (`_vic_tick_one` vs `_advance_raster`). |
| Host video | `graphics.py`: pygame presenter; pixels built in `presenter.RgbFrameBuffer` then uploaded (see `presenter.py`). Moderate cost vs core except at high scale / per-pixel paths. |
| Audio | `resid.py` / SID; can compete with CPU depending on buffer and host. |
| Disk / IEC | `drive1541.py`, `iec_bus.py` when the disk is active. |

## Graphics + ReSID + turbo regression canary

Use a fixed cycle cap and the same host to spot throughput regressions in the **integrated** path (pygame + ReSID + `--turbo`). Absolute **MHz** varies by machine; compare **before/after** or **git revisions** on the same hardware.

```bash
cd /path/to/c64py
python C64.py --graphics --enable-resid --max-cycles 8000000 --autoquit programs/swinth.prg --turbo
```

A historical fast-mode baseline on one developer machine was on the order of **~0.87 emulated MHz** at commit `6556a0c36b20d9c51c005df3c565b4b08955fd28` for this command (PAL nominal ~0.985 MHz). Re-run and grep the summary / `C64PY_BENCHMARK` line if your build prints them.

**Host note:** The pygame main thread must not **tight-poll** (e.g. `sleep(1 ms)` in a loop) while the CPU runs on another thread — on CPython that can steal the GIL **~1000×/s** and slow the emulator. Prefer sleeping until the next **present** deadline at `--graphics-fps`, and skip per-instruction ReSID CPU hooks when ReSID is decoupled (fast VIC).

**Graphics:** Standard **hires text** uses **pygame blit** only when the **2048-byte charset visible in RAM** exactly matches the **character ROM** (upper or lower 2K slice)—glyph surfaces are built **once**. Custom or animated charsets fall back to the **RGB buffer** glyph path (no per-frame `set_at` rebuild).

## Integrated path: throughput vs older Python (`faf640f`)

This section is for **merge / roadmap** context: the current Python tree (accurate VIC / badlines work, RGB presenter, richer IRQ path) is **accepted** as the correctness baseline even though the **integrated** benchmark (pygame + ReSID + turbo) is slower than an **older** revision on the same host.

### Commit where integrated performance was clearly better

For the **same** canary command as above (`programs/swinth.prg`, 8M cycles, `--graphics --enable-resid --turbo`), the last revision we repeatedly measured as **faster** on the same machine than post-badlines `main`-bound work is:

| | |
|--|--|
| **Git (full)** | `faf640fa469a2d5d7ab41242a6ad2bf3c5190f63` |
| **Short** | `faf640f` |

Use that commit as the **A/B “good Python throughput” anchor** when profiling on your hardware (`git checkout faf640f` vs current `HEAD`, same ROMs, same Python).

**Note:** An **earlier** published headless-style anchor is still `6556a0c36b20d9c51c005df3c565b4b08955fd28` (~0.87 MHz on one machine for a related benchmark); it is **not** the same binary path as the swinth + pygame + ReSID canary above.

### How large is the gap?

On representative captures (same Mac, same flags), **wall time** for the CPU thread to finish 8M cycles was on the order of **~22% higher** (~22.3 s vs ~27 s) and **emulated MHz** correspondingly lower (~0.36 vs ~0.30) when comparing **`faf640f`** to current work. **Your absolute MHz will differ**; the important part is **relative** before/after on one PC.

### What we think changed (high level)

Not one line of code — mostly **architecture** accumulated after `faf640f`:

1. **Host rendering** — Default compositing uses **`presenter.RgbFrameBuffer`** (`fill_rect` / glyph / pixel paths), especially heavy in **multicolor bitmap / MCM / wide sprites**, vs more **`pygame.Surface.fill`** on the older tree’s hot path.
2. **CPU “envelope”** — Profiles show **more exclusive time in `step()`** (raster batching, `recompute_pending_irq`, memory path, throttling) while **`_execute_opcode`** is often **similar or cheaper** than on the old tree: the extra cost is **around** the opcode, not inside the big dispatch table.

Mitigations and tooling live in-tree: **`C64PY_PROFILE_CPU_THREAD`**, **`scripts/profile_swinth_graphics_compare.py`**, and fast-VIC **VIC latch cadence** (`vic_snapshot_each_emulated_frame` — see [emulation_modes.md](emulation_modes.md)).

### Closing the gap

- **Python:** Batching presenter updates (fewer tiny `fill_rect` / pixel calls) is the most realistic win for **`--graphics`** without dropping features.
- **Native core:** An **optional** Rust extension (`c64py_rust_core` / `CPU6502.step_fast_batch`) exists for the fast-VIC path; see [rust_core.md](rust_core.md).

Merging **`main`** with the current branch is **compatible** with accepting this throughput trade-off while accuracy and presenter behavior are locked; treat **`faf640f`** as the **performance regression anchor** for future native-core or batching work.

## How to measure (today)

1. **cProfile** (time per function):

   ```bash
   cd /path/to/c64py
   python -m cProfile -o /tmp/c64py.prof C64.py --max-cycles 2000000 /path/to/test.prg
   python -c "import pstats; pstats.Stats('/tmp/c64py.prof').sort_stats('cumulative').print_stats(40)"
   ```

   With **graphics and ReSID** (same idea, more host load): adjust flags per `python C64.py --help`. For **core only**, use headless (default with `--benchmark`). For **pygame + ReSID** (more realistic when the game depends on SID and rendering), add `--graphics --enable-resid` to `C64.py` (needs a display/window and the reSID library; see `--enable-resid` in `--help`).

2. **`cpu.step` statistics** — Count invocations and emulated cycles per second in a fixed mode (`accurate_vic` on/off) for A/B comparison.

3. **`scripts/profile_hotpath.py`** (isolated, no UI):  
   `PYTHONPATH=… python3 scripts/profile_hotpath.py [--accurate-vic] [steps]` — prints cProfile top for `CPU6502.step`. For the full binary, keep using `python -m cProfile … C64.py …`.

4. **`py-spy` / `scalene`** (optional) — Useful on macOS/Linux for live sampling with less cProfile bias on C-extension code (ReSID).

## Reproducible benchmark (c64py)

Goal: **same workload** and **same parameters** across runs, plus one **parseable** line for scripts or notebooks.

### Reference program

- Source: `src/BENCHMARK.BAS` (screen fill, border/background changes, BASIC math, `POKE` to screen and color).
- Binary: `programs/benchmark.prg` — build with `./tools/compile.sh` (requires VICE **petcat**).

### Invoking c64py

`--benchmark` implies `--turbo`, `--autoquit`, `--no-colors` and, if you do not pass another `.prg`, loads `programs/benchmark.prg`. It also forces **`--headless`** unless you use `--graphics`.

Examples (set `--rom-dir` to where you keep `kernal`, `basic`, etc.):

```bash
cd /path/to/c64py

# Fast VIC, 20M cycles
python C64.py --benchmark --max-cycles 20000000 --rom-dir ./roms

# Same cap with accurate VIC (slower, more faithful)
python C64.py --benchmark --max-cycles 20000000 --rom-dir ./roms --accurate-vic

# Same workload with pygame window + ReSID (music-heavy demos; “full picture”)
python C64.py --benchmark --max-cycles 20000000 --rom-dir ./roms --graphics --enable-resid
python C64.py --benchmark --max-cycles 20000000 --rom-dir ./roms --graphics --enable-resid --accurate-vic
```

With `--graphics`, `--benchmark` does **not** force headless. You need a graphical environment; on servers without X11 it usually fails unless you use a framebuffer/SDL setup (`SDL_VIDEODRIVER` depends on platform).

When the run finishes you get the human summary (`=== Emulation Speed ===`) and one **JSON line** prefixed with `C64PY_BENCHMARK` (handy for `grep` or pipelines):

```bash
python C64.py --benchmark --max-cycles 20000000 --rom-dir ./roms 2>/dev/null | grep '^C64PY_BENCHMARK '
```

Relevant JSON fields:

| Field | Meaning |
|-------|---------|
| `cycles` | Emulated CPU cycles when stopped (often matches `--max-cycles` if the cap is hit). |
| `wall_seconds` | Host time (from emulator CPU loop start). |
| `emulated_cpu_mhz` | `cycles / wall_seconds` (raw throughput on this machine). |
| `accurate_vic` | `true` / `false`. |
| `enable_resid` | `true` if `--enable-resid` (ReSID via C extension). |
| `enable_sid` | `true` if `--enable-sid` (pygame SID). |
| `max_cycles_arg` | Value passed to `--max-cycles`. |
| `prg` | Program basename (e.g. `benchmark.prg`). |

**Note:** If `max_cycles` is low, the PRG may not print `benchmark complete`. Raise the cap (e.g. `50000000` or more) until a screen dump shows the end of the test, or compare **MHz only** with the same `max_cycles` between flags (`--accurate-vic` on/off).

### Script `run_benchmark.sh` (combinations + tee + NDJSON)

By default it runs **four combinations**: headless × (fast / accurate VIC) and **pygame + ReSID** × (fast / accurate). Each run:

- Writes output with **`tee`** to `logs/benchmark-<date>_<mode>.log` (slug encodes stack and VIC).
- Appends one **JSON line** (NDJSON) to **`logs/benchmark-log.json`**, with `git_commit`, `git_dirty`, `git_describe`, `benchmark_type`, `argv`, `exit_code`, `host_wall_seconds`, `log_file`, `python_version`, `platform`, flat metrics (`cycles`, `emulated_cpu_mhz`, …), the `c64py_benchmark` object, and when applicable `cprofile_prof`, `cprofile_pstats`, `vice_trace_file`, `vice_trace_wall`.

Optional (heavy disk I/O; lower `--cycles` for traces):

- `--cprofile` — runs under `python -m cProfile`, writes `logs/benchmark-<ts>_<slug>.prof` and a `*.pstats.txt` summary (top 40 by cumulative time).
- `--vice-trace` / `--vice-trace-wall` — VICE-format trace in `logs/…vice.log`.
- **If you use `--cprofile` and tracing together**, the script runs **two passes per combo**: first trace **with** `--vice-trace-wall` and **without** cProfile; then cProfile **without** trace (so `.pstats` is not filled with trace I/O).

In `C64.py`, `--vice-trace-inline-wall` puts `; w` on the **same line** as the instruction (alternative to a separate `; w` comment line).

Filter combinations:

```bash
chmod +x scripts/run_benchmark.sh
./scripts/run_benchmark.sh --help

# Headless only (2 runs: fast + accurate VIC)
./scripts/run_benchmark.sh --headless-only /path/to/roms

# Window + ReSID only
./scripts/run_benchmark.sh --graphics-resid-only /path/to/roms

# Single run
./scripts/run_benchmark.sh --headless-only --vic-fast-only /path/to/roms

# Cycle count (also: `BENCHMARK_CYCLES` env)
BENCHMARK_CYCLES=5000000 ./scripts/run_benchmark.sh /path/to/roms

# Profile + trace: script splits walltrace vs cProfile automatically (keep cycles low)
BENCHMARK_CYCLES=500000 ./scripts/run_benchmark.sh --headless-only --cprofile --vice-trace /path/to/roms
```

Reading results and hotspots (example): [perf/benchmark_analysis_20260405.md](../perf/benchmark_analysis_20260405.md) (session `20260405-202206`; mind cProfile skew when trace was combined in older runs).

Read the aggregated log (one line = one JSON object):

```bash
while IFS= read -r line; do echo "$line" | python -m json.tool; done < logs/benchmark-log.json
```

### Comparing with VICE (qualitative)

VICE does not offer a portable CLI “run exactly N CPU cycles and exit” across all versions, so comparison is usually:

1. **Same workload**: autostart `programs/benchmark.prg` in VICE with **warp** and measure **wall time** until `benchmark complete` appears (stopwatch or recording).
2. **c64py**: use a high enough `--max-cycles` for the message to appear (or compare MHz only with a fixed cycle count, which is 100% reproducible in c64py).

Example (adjust paths and `x64sc` / `x64` binary):

```bash
x64sc -warp -sounddev dummy -autostartprgpath programs/benchmark.prg +confirmexit
```

BASIC **jiffies** (`TI` / `TI$`) follow the **emulated** clock (PAL/NTSC), not the host: they help compare **accuracy** between VICE and c64py if both finish the program, while `emulated_cpu_mhz` measures **how much host time** you need to advance N cycles.

## Likely bottlenecks (ordered hypotheses)

1. **Python per opcode** — A jump table or tighter `match`/dispatch can cut interpreter overhead; cycle/trace logic must stay correct.
2. **`MemoryMap.read`** — Many sequential checks; a **fast path** for “plain RAM” when `$01` and the map allow (watch CHAREN / I/O).
3. **Accurate VIC = one `tick()` per CPU cycle** — Micro-optimize `ViciiCycleEngine.tick()` (local attributes, fewer tuples) or move the core to **C/Rust** for another order of magnitude.
4. **ReSID lockstep** — With `--enable-resid`, **fast VIC** (default) runs the SID in **decoupled** chunks on the audio thread; **`--accurate-vic`** keeps SID **cycle-locked** to the CPU (slower, better for SID readback timing). See [emulation_modes.md](emulation_modes.md).
5. **Traces and UDP** — With `trace_enabled` or UDP on, cost dominates; turn them off for “max speed” benchmarks.

## Improvement roadmap (light)

- **Near term**: profile a fixed game/demo with `--accurate-vic` on/off; record emulated MHz in README or here.
- **Medium term**: dispatch table for “common” opcodes (LDA/STA/branches) without touching rare ones.
- **Long term**: native extension for CPU+memory+VIC in the hot loop, or PyPy (install the same deps as CPython; try `PYTHONPATH=… pypy3 scripts/profile_hotpath.py --accurate-vic 50000` vs `python3`; validate pygame/resid).

## Internal references

- Speed / throttling: `emulator.py` — `throttle_emulation_if_needed`, `turbo`.
- VIC mode: `accurate_vic` when constructing `CPU` (see `C64.py` / emulator startup).
