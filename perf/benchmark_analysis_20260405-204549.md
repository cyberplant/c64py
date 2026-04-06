# Benchmark analysis — session `20260405-204549` (5M cycles)

Source: `logs/benchmark-20260405-204549_*_walltrace.log` and matching `*_cprofile.pstats.txt`.

## Emulated CPU MHz (from `C64PY_BENCHMARK` JSON)

Wall-trace runs use **`--turbo`** (no real-time throttle). `max_cycles` = 5,000,000, `prg` = `benchmark.prg`.

| Mode | accurate_vic | emulated_cpu_mhz | wall_seconds |
|------|--------------|------------------|--------------|
| headless | false | 0.3665 | 13.64 |
| headless | true | 0.2678 | 18.67 |
| graphics + `--enable-resid` | false | 0.296 | 16.89 |
| graphics + `--enable-resid` | true | 0.2262 | 22.11 |

Ratios (same column):

- Accurate vs fast VIC, headless: **~0.73×** MHz (about **1.37×** wall time for the same cycles).
- Graphics vs headless, fast VIC: **~0.81×** MHz.
- Graphics vs headless, accurate VIC: **~0.84×** MHz.

## ReSID / pygame note

The wall-trace logs for this session include a **“reSID library not found”** warning, so **`enable_resid` in JSON may be true (flag set) while the run fell back to no native ReSID**. Treat these rows as **“graphics window + pygame stack”** unless you confirm `resid_c` was loaded. For a clean **graphics + ReSID** profile, rebuild/copy the library per `src/resid_wrapper/README.md` and re-run.

## Hot spots (cProfile, directional)

**Multi-threading:** With `--graphics`, cProfile merges samples across the **main (pygame) thread** and the **emulator thread**. Cumulative times can **exceed wall time** or blend SDL wait with Python work—use `.pstats.txt` as **qualitative** only for graphics runs.

### Headless + fast VIC (`headless_fast-vic_cprofile.pstats.txt`)

- Dominated by `cpu.step` → `_execute_opcode`, `_mr`, `memory.read`, `_update_cia_timers`, `_advance_raster`.
- This benchmark path also spends significant time in `emulator._update_text_screen` / `time.sleep` via the **60 Hz screen worker** while the CPU thread runs—profile shape depends on how much wall time is sleep vs CPU.

### Headless + accurate VIC (`headless_accurate-vic_cprofile.pstats.txt`)

- Adds **`_vic_tick_one`** / **`ViciiCycleEngine.tick`** on the per-cycle path alongside `memory.read`, CIA ticks, **`_cpu_port01_effective`**, and **`recompute_pending_irq`**.

### Graphics + accurate VIC (`graphics-resid_accurate-vic_cprofile.pstats.txt`)

Extra top entries vs headless accurate:

- **`pygame.display.flip`**, **`graphics._render_frame`**, **`pygame.transform.scale`**, **`graphics._render_text_mode`**
- **`graphics._plot_hires_text_cell`** and **`_fetch_glyph_rows`** — per-cell / per-pixel work on the Python side
- Core entries (`cpu.step`, `_vic_tick_one`, `memory.read`) remain large because the **emulator thread** is still profiled.

## Threading reminder

In `graphics.PygameInterface`, **pygame runs on the main thread**; **`cpu.step` runs on a background thread**. They share the **GIL** and CPU time, so a heavy presenter path still reduces achievable MHz vs headless.

## Follow-ups implemented in tree

- **Presenter layer:** [`presenter.py`](../presenter.py) — RGB framebuffer + upload to pygame; documented as the boundary for future line-buffer / multi-band compositing.
- **Host refresh:** default **`--graphics-fps` 30** and optional decoupling of event pump vs present (see `graphics.py`).
- **Memory:** cached **6510 port $01 effective** value invalidated on writes to `$00`/`$01` to cut `read()` hot-path cost.

See also [`docs/performance.md`](../docs/performance.md).
