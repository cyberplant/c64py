# Benchmark analysis — session `20260405-202206` (5M cycles, cProfile + VICE trace)

Source: `logs/benchmark-20260405-202206_*.pstats.txt` and `logs/benchmark-log.json` (NDJSON lines 3–6).

## Summary MHz (emulated CPU, from JSON)

| Mode | emulated_cpu_mhz |
|------|------------------|
| headless, fast VIC | ~0.161 |
| headless, accurate VIC | ~0.123 |
| graphics + ReSID, fast VIC | ~0.097 |
| graphics + ReSID, accurate VIC | ~0.064 |

Accurate VIC is ~1.3× slower than fast (headless); adding graphics+ReSID stacks another large cost on top.

## Critical caveat: do not combine cProfile with VICE trace in one process

Older runs did both at once; **`debug.py:log_instruction`** then dominated `.pstats` (I/O + formatting).

**`scripts/run_benchmark.sh`** now runs **two passes** when both `--cprofile` and `--vice-trace` are set: trace+wall first, then cProfile without trace. For raw MHz, omit both.

## Hot spots (structural, after discounting trace I/O)

### Headless + accurate VIC

- **`cpu.step`** — dominates.
- **`_vic_tick_one` / `ViciiCycleEngine.tick`** — one tick per emulated CPU cycle.
- **`MemoryMap.read`** and **`_cpu_port01_effective`** — very high call counts.
- **`_update_cia_timers`**, **`_vic_sync_engine_shadow_regs`**, **`_bus_cycle_phases`**, **`sid_tick_cpu_cycles`**.

Directions: RAM fast-path expansion (careful), CIA/SID tick batching where exactness allows, cheaper VIC shadow sync, reduce Python per-cycle overhead (native extension / PyPy).

### Headless + fast VIC

- **`cpu.step`**, **`_execute_opcode`**, **`_mr`**, **`_update_cia_timers`**, **`memory.read`**, **`_advance_raster`**.

### Graphics + ReSID

- **`resid.tick_cpu_cycles`** via **`memory.sid_tick_cpu_cycles`** — large when ReSID is on.
- **`graphics._plot_hires_text_cell`**, **`_fetch_glyph_rows`**, **`_render_text_mode`** — per-cell work at ~PAL frame rate × cells.
- **`pygame.transform.scale`**, **`Clock.tick`** — host presentation.
- **`emulator.shutdown` / `resid.close`** — can dominate wall time at process exit in profiles (not steady-state emulation).

Directions: throttle full-screen text redraws, cache glyphs / dirty rectangles, amortize ReSID buffer work, optional lower graphics scale.

## Suggested next measurements

1. Same four combos, **no** `--vice-trace`, optional `--cprofile` only on one short run.
2. Compare `headless_fast` vs `headless_accurate` pstats side-by-side (top 20 cumulative).
