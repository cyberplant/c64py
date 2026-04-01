# Bruce Lee loader: trace divergence investigation

Persistent notes for the c64py vs VICE mismatch (wrong byte at `$E5F0`, source pointer `$2F/$30` two bytes ahead in c64py). Use this file to resume work without relying on chat context.

**Planning / roadmap (short):** [LOADER_DEBUG_PLAN.md](LOADER_DEBUG_PLAN.md) — status, completed work, and prioritized next actions.

## Symptom

- Game: **Bruce Lee** (`programs/BruceLee.prg`).
- Failure: bad data under **`$E5xx`**, **`JMP $C200` → BRK** after load.
- Loader uses **`STA ($2D),Y`** at **`$00FA`** and **`LDA ($2F),Y`** at **`$0113`** (and **`JSR $0103`** path with **`INC $2F`** at **`$010D`**).
- At the first **`STA $00FA`** that stores to **`$E5F0`**, VICE has **`$2F=$53`** (read from **`$E753`**, **`A=$20`**); c64py had **`$2F=$55`** (`$E755`, **`A=$C0`**): source pointer **+2 bytes** vs VICE for the same destination.

## Numeric facts (reference)

| Item | Value / note |
|------|----------------|
| Source span VICE | `$E753 - $4F54` = **`$97FF`** = **38911** byte steps |
| Source span c64py | `$E755 - $4F54` = **`$9801`** = **38913** (two extra **`INC $2F`** in the same logical window) |
| c64py `INC $2F` @ `$010D` in window 4CF5→E5F0 | **38913**; **`INC $30` @ `$0111`**: **152**; IRQs in window: **0** (fast + accurate VIC runs checked) |
| VICE trace experiment | After **38911**-th **`INC $2F` @ `$010D`** from first anchor `STA $00FA` **A:CC X:4F**, next **`STA ($2D),Y`** shows **`A=$20`**; after **38913**-th, next shows **`A=$C0`** (same trace file) |
| `compare_loader_branches.py` | **105 706** matching **(pc, take, z)** vs VICE after anchor (fresh log); first mismatch **idx=105706**: c64py **`$088A`** cyc **12794852** vs VICE **`$010F`** cyc **90487723** (same take/z — phase slip) |
| c64py milestone cycles (14.5M run) | `first_4cf5` **9804470**; `first_e5f0` **13067075**; delta **3262605** (not portable to VICE absolute cycles) |
| **JSR outer driver** (`$087E`→`$00FA`, `$0881`→`$0103`) in loader window | See § [JSR counts](#jsr-counts-outer-driver) below |

**38911 vs “free memory”:** **`0x97FF`** is the 16-bit source pointer delta, not a KERNAL “bytes free” table entry.

### JSR counts (outer driver)

c64py can count **`JSR` from `$087E`–`$0884` to `$0103` or `$00FA`** in the **same** window as the `$2F`/`$30` histogram (`C64PY_LOADER_PTR_SRC_COUNT` milestones). VICE can be compared with [`scripts/vice_trace_loader_jsr_counts.py`](../scripts/vice_trace_loader_jsr_counts.py).

**VICE** (`vice_full_trace.log`, anchor = first `STA ($2D),Y` @ `$00FA` with **A:CC X:4F**):

| End bound | `$087E`→`$00FA` | `$0881`→`$0103` | total |
|-----------|-----------------|-----------------|-------|
| `--nth-inc 38911` (cycle of Nth `INC $2F` @ `$010D`) | 37434 | 37520 | 74954 |
| `--nth-inc 38913` | 37436 | 37522 | 74958 |
| **`--nth-inc 38911 --end-at-next-sta00fa-after-nth-inc`** (exclude JSRs from Nth `INC` up to **next** `STA @ $00FA`, closer to c64py “stop before E5F0 store”) | 37435 | 37520 | 74955 |
| **`--nth-inc 38913` + same end flag** | 37437 | 37522 | 74959 |

So in VICE, moving the bracket by **two** inner bumps (**38911→38913**) adds **+2** to each JSR column when the end is defined the same way — consistent with “two extra trips through the copy helper”.

**c64py** (one run, `--rom-dir` set, window ends at **first `eff=$E5F0`** milestone):

- `JSR $087E`→`$00FA`: **37459**; `JSR $0881`→`$0103`: **37542**; **total 75001**.

**Resolution (Mar 2026):** With `C64PY_LOADER_PTR_SRC_COUNT=1`, the flush line now includes **`sta00fa_zp2d_before_e5f0=`** — the number of **`STA ($2D),Y` @ `$00FA`** (zp `2D`) with **`eff ≠ $E5F0`** after the window opens. On Bruce Lee this is **39162**, which matches a VICE count of **`.C:00fa` lines with `91 2D` strictly between the same anchor cycle and the first “next STA @ `$00FA` after the 38911th `INC $2F` @ `$010D`”** (same script as for JSR bounds).

So **destination progression and store count to the first `$E5F0` write are aligned** between c64py and VICE; the **~46** extra JSRs in c64py vs the VICE **74955** bracket are from **cycle-stamp semantics** in the trace (`cyc < end` on VICE lines) vs **instruction-boundary** activation of `_loader_src_count_active` in c64py, not from “hitting `$E5F0` later” in terms of stores.

Use the VICE **38911→38913** **+2** JSR step as the clean A/B for inner bumps; use **`sta00fa_zp2d_before_e5f0`** when you need a **store-count** anchor that matches both sides.

## Why traces are hard to align

1. **Different absolute cycle bases** between VICE and c64py; compare **semantically** (anchors), not raw cycle numbers.
2. **Multi‑GB traces** need streaming, **`rg`**, and the scripts below.
3. Divergence is often **one extra/missing step** in a tight loop (**phase slip**), not an obvious wrong flag on the first branch.
4. **Hot path is not `$0120`**: Bruce Lee’s copy loop uses **`$010F`** (BNE), **`$00FE`**, **`$088A`**; **`BRANCH_TRACE`** in `cpu.py` includes those PCs.

## Inject semantics and capture coherence (Apr 2026)

### When `--debug-inject-at-cycle` fires

Injection runs once at the **beginning** of **`CPU6502.step()`** when **`state.cycles >= N`**, **before** the instruction at **`PC`** is fetched (see [`cpu.py`](../cpu.py) `_maybe_apply_debug_inject`). So **`N`** is an **emulator cycle count**, not “VICE line cycle pasted from a trace row” in a 1:1 sense.

**Practical consequence:** The **`--inject-hint`** pair **`c64py_cyc=12794852`** / **`vice_cyc=90487723`** comes from **`compare_loader_branches`**: the **first differing event** in the **(pc, take, z)** stream. On a recorded run, at **12794852** c64py logged **`pc=$088A`** while the archived VICE line at the same **index** shows **`pc=$010F`**. Registers and RAM injected at **12794852** therefore land at a **different opcode boundary** than “stopped on **`$010F`** in VICE”. For experiments aimed at **`$010F`**, derive **`N`** from a **c64py log line** with **`BRANCH_TRACE pc=$010F`** near the mismatch (or add tooling to emit that cycle automatically).

### Live `x64sc` capture vs multi‑GB archive

**`compare_loader_branches`** is anchored to a **specific** file (**`vice_full_trace.log`**) and a **fixed** VICE cycle on a **`.C:010f`** row (~**90487723**). A **new** autostart capture (`vice_monitor_client.py --preset mismatch_90487723`) uses the **current** STOPWATCH; the monitor may report a **different** absolute cycle on the **`.C:010f`** line (e.g. **90627944**) even when phase‑2 logic targets “≥ 90487723”. That is **expected** when the reference trace was produced in **another run** or the monitor samples **slightly after** the trace printer. For a **self‑consistent** inject file + map, take **all** bytes and registers from **one** stop (one JSONL / one log), not regs from the archive and stack from a new run.

### `C64.py` **`--debug-inject-map`** parser

Earlier versions referenced an undefined name in **`_parse_debug_inject_map_string`** and crashed with **`NameError`** when using **`--debug-inject-map`**. Fixed: fragments are attributed to **`--debug-inject-map`** in error messages.

### Experiment: full stack page + ZP + regs from one JSONL

Pipeline verified end‑to‑end:

1. Capture with **`vice_monitor_client.py`** (preset **`mismatch_90487723`** or equivalent) → JSONL includes **`mem_dump`** for **`0100 01ff`**.
2. **`vice_mem_dump_to_inject.py --jsonl --match-command "0100 01ff" …`** → **`--debug-inject-file`** (256 lines).
3. **`--debug-inject-map`** with **A/X/Y/P** and **`$2D`–`$30`** from the **same** monitor stop as the dump.
4. **`compare_loader_branches`** vs **`vice_full_trace.log`**: first mismatch **index** remained **`idx=105706`**; **take/z** still agreed on that row but **PC / register columns** vs the **archive** reflect **mixed anchors** (live map vs old trace). Conclusion: **phase slip** is not corrected by this single inject at the **hint** cycle alone; next step is aligning **inject time** with **`$010F`** on the c64py side (see [LOADER_DEBUG_PLAN.md](LOADER_DEBUG_PLAN.md)).

## Commands (copy-paste)

Paths: adjust `--rom-dir` and trace/log paths to your machine.

### Autonomous VICE (x64sc) remote monitor capture (no human)

This repo includes [`scripts/vice_monitor_client.py`](../scripts/vice_monitor_client.py) to automate VICE monitor dumps. You can run `x64sc` with a TCP remote monitor, then let the script stop on the **second** store to `$E5F0` (the one that hits `STA ($2D),Y @ $00FA`) and dump regs + key memory ranges.

**Recommended (single command):**

```bash
python3 scripts/vice_monitor_client.py --launch-x64sc --kill-x64sc \
  --preset e5f0_second_00fa \
  --output vice_capture_autolaunch.log \
  --json-output vice_capture_autolaunch.jsonl \
  --quiet
```

Optional: capture VICE stdout/stderr for troubleshooting:

```bash
python3 scripts/vice_monitor_client.py --launch-x64sc --kill-x64sc \
  --x64sc-log x64sc_autolaunch.log \
  --preset e5f0_second_00fa \
  --output vice_capture_autolaunch.log \
  --json-output vice_capture_autolaunch.jsonl
```

### Mismatch-cycle preset (`vice_cyc=90487723`)

To capture state near the first `compare_loader_branches` mismatch (VICE side), use the dedicated preset:

```bash
python3 scripts/vice_monitor_client.py --launch-x64sc --kill-x64sc \
  --preset mismatch_90487723 \
  --output vice_capture_mismatch_90487723.log \
  --json-output vice_capture_mismatch_90487723.jsonl
```

Optional tuning (decimal counts; `ignore` is still sent to VICE as hex digits):

- `--phase1-ceiling-cycle` (default `90487723`)
- `--phase1-margin-cycles` (default `32768`)
- `--phase1-max-ignore` (default `32768`)
- `--phase1-guess-cyc-per-hit` (default `2800`)
- `--phase1-bootstrap-ignore` (default `8192`)

What it does:

- **Phase 1:** `delete; break 0881`, dynamic `ignore` on that checkpoint, poll `g; r` (no `z`), switch to phase 2 when STOPWATCH `>= 90487000` at `pc=$0881`
- **Phase 2:** `delete; break 010f`, stop when trace cycle `>= 90487723` at `pc=$010f`
- dumps on final stop: `0000 0001`, `002d 0030`, `00f8 0118`, `0100 01ff`, `e5f0 e610`, `e750 e770`

**Important (manual mode):**

- `x64sc` must be started **outside any sandbox/restricted environment** so it can open listening sockets.
- The **text** remote monitor port is **dynamic** (in our run it was `54379`). Discover it with `lsof` (look for the non-`6502` port).
- Restart `x64sc` before each capture to avoid unknown state.

Start VICE:

```bash
cd /path/to/c64py
x64sc -remotemonitor -remotemonitoraddress 127.0.0.1 \
  -autostart programs/BruceLee.prg --warp --console
```

Find the text monitor port:

```bash
lsof -nP -iTCP -sTCP:LISTEN | rg "x64sc"
```

You should see something like:

- `127.0.0.1:<TEXT_PORT> (LISTEN)`  ← use this
- `127.0.0.1:6502 (LISTEN)`        ← binary monitor / ignore for this script

Run the capture (stops at `$00FA/$00FC`, dumps ZP + key ranges):

```bash
python3 scripts/vice_monitor_client.py --host 127.0.0.1 --port <TEXT_PORT> \
  --preset e5f0_second_00fa \
  --output vice_capture_e5f0_second_00fa.log \
  --json-output vice_capture_e5f0_second_00fa.jsonl \
  --quiet
```

The resulting JSONL is machine-readable; for example our capture produced:

- ZP `$2D-$30`: `f0 e5 53 e7`
- regs at stop: `A=20 X=E7 Y=00 SP=FD P(bin)=10100100` (this stop is at `PC=$00FC`)

To quit from the monitor when you're done:

```text
q
```

### Milestones + `$2F`/`$30` write histogram (c64py)

```bash
export C64PY_LOADER_PTR_MILESTONES=1
export C64PY_LOADER_PTR_SRC_COUNT=1
export C64PY_LOADER_PTR_MILESTONES_LOG=/tmp/loader_milestones.log
export C64PY_LOADER_PTR_SRC_COUNT_LOG=/tmp/loader_ptr_src_count.log
# Optional: JSR $0103 / $00FA from $087E–$0884 in the same window
export C64PY_LOADER_JSR_COUNT=1
export C64PY_LOADER_JSR_COUNT_LOG=/tmp/loader_jsr.log

python3 C64.py programs/BruceLee.prg --headless --turbo \
  --max-cycles 14500000 --autoquit --rom-dir /path/to/roms
```

### Bruce Lee targeted log (STA, branches, pointers, …)

```bash
export C64PY_BRUCELEE_DEBUG=1
export C64PY_BRUCELEE_DEBUG_LOG=/tmp/bruce.log

python3 C64.py programs/BruceLee.prg --headless --turbo \
  --max-cycles 13150000 --autoquit --rom-dir /path/to/roms
```

**Regenerate + compare in one shot** (from repo root; `--rom-dir roms` resolves next to `C64.py`):

```bash
export C64PY_BRUCELEE_DEBUG=1
export C64PY_BRUCELEE_DEBUG_LOG=/tmp/bruce_fresh.log

python3 C64.py programs/BruceLee.prg --headless --turbo \
  --max-cycles 13200000 --autoquit --rom-dir roms

python3 scripts/compare_loader_branches.py \
  --c64py-log /tmp/bruce_fresh.log \
  --vice-trace vice_full_trace.log \
  --max-diff 10 --inject-hint
```

As of 2026-04, a fresh log still hits the first **phase-slip** mismatch at **`idx=105706`** (`c64py` **`$088A`** cyc **12794852** vs VICE **`$010F`** cyc **90487723**; same take/Z). The log is large (~tens of MB); **`bruce_fresh.log`** at repo root is gitignored if you prefer that path.

### Compare branch sequence (pc, take, z) vs VICE trace

Requires `STA_INDY` with **`eff=$4CF5`** in the c64py log for anchor.

The Bruce log must include **`BRANCH_TRACE`** at **`$00FE`**, **`$010F`**, **`$088A`** (current `cpu.py`). Older logs that only show **`$012C`–`$0134`** will **mismatch at idx 0** — regenerate with `C64PY_BRUCELEE_DEBUG=1` on a current tree.

```bash
python3 scripts/compare_loader_branches.py \
  --c64py-log /tmp/bruce.log \
  --vice-trace /path/to/vice_full_trace.log \
  --max-diff 20 \
  --inject-hint
```

**`--inject-hint`:** on the first **pc/take/z** mismatch, prints **`c64py_cyc`** / **`vice_cyc`**, **`c64py_cyc_last_010f_before_mismatch`** (last **`BRANCH_TRACE @ $010F`** strictly before the mismatch cycle), a suggested **`C64.py`** line with **`--debug-inject-map`** from that **`$010F`** line (regs + ZP), and the alternate [`vice_trace_to_inject.py`](../scripts/vice_trace_to_inject.py) stub for the raw first-mismatch cycle (often **`$088A`**).

### VICE: count `INC $2F` @ `$010D` (38911 vs 38913 experiment)

```bash
python3 scripts/vice_trace_loader_counts.py /path/to/vice_full_trace.log
```

### VICE: JSR from `$087E`–`$0884` (compare to `C64PY_LOADER_JSR_COUNT`)

Prefer **`--end-at-next-sta00fa-after-nth-inc`** so the window ends just before the next `STA ($2D),Y` @ `$00FA` after the Nth inner bump (closer to c64py’s `first_e5f0` cut than using the INC line cycle alone).

```bash
python3 scripts/vice_trace_loader_jsr_counts.py /path/to/vice_full_trace.log \
  --nth-inc 38911 --end-at-next-sta00fa-after-nth-inc
python3 scripts/vice_trace_loader_jsr_counts.py /path/to/vice_full_trace.log \
  --nth-inc 38913 --end-at-next-sta00fa-after-nth-inc
```

On very large traces, prefer **`rg`** yourself if needed.

### c64py: VICE-format CPU trace

```bash
python3 C64.py programs/BruceLee.prg --headless --turbo \
  --max-cycles 13150000 --autoquit --rom-dir /path/to/roms \
  --vice-trace /tmp/c64py_cpu.trace
```

### One-shot state inject (c64py)

After **`CPU.cycles >= N`**, at the **start** of the next **`step()`** (before fetch), poke RAM and/or **A/X/Y/P** once (stderr + optional Bruce log). Use **`--debug-inject-file`** for many bytes (e.g. stack **`$0100`–`$01FF`**) from a VICE dump; combine with **`--debug-inject-map`** for regs/ZP. **`--debug-inject-map`** requires a fixed parser in **`C64.py`** (see § [Inject semantics](#inject-semantics-and-capture-coherence-apr-2026)).

```bash
python3 C64.py programs/BruceLee.prg --headless --turbo \
  --max-cycles 13150000 --autoquit --rom-dir /path/to/roms \
  --debug-inject-at-cycle 12794852 \
  --debug-inject-file test/fixtures/debug_inject_stack.example.txt \
  --debug-inject-map a=d6,x=da,y=00,p=a5,2d=f0,2e=e5,2f=53,30=e7
```

### VICE monitor dump → `--debug-inject-file` (stack page)

1. Capture with [`vice_monitor_client.py`](../scripts/vice_monitor_client.py) (preset **`mismatch_90487723`** already dumps **`0100 01ff`**) or manually: **`m 0100 01ff`** in the monitor.
2. Convert **`>C:....`** lines to inject format:

```bash
# From a log slice that contains only the desired `m` output (avoid mixing multiple dumps):
python3 scripts/vice_mem_dump_to_inject.py --low 0100 --high 01ff --strict capture.log > /tmp/stack.inject

# From JSONL (match the mem_dump command):
python3 scripts/vice_mem_dump_to_inject.py --jsonl --match-command "0100 01ff" \
  vice_capture_mismatch_90487723.jsonl --low 0100 --high 01ff --strict > /tmp/stack.inject
```

3. Re-run c64py with **`--debug-inject-file /tmp/stack.inject`** plus **`--debug-inject-map`** from [`vice_trace_to_inject.py`](../scripts/vice_trace_to_inject.py), then **`compare_loader_branches`** again to see if mismatch **idx** moves.

### Build `--debug-inject-map` from a VICE trace line

Use **VICE** cycle in `--match-vice-cycle`; use **c64py** cycle in `--inject-cycle` for `C64.py`.

```bash
python3 scripts/vice_trace_to_inject.py \
  --file vice_full_trace.log \
  --match-vice-cycle 90487723 \
  --fast-rg \
  --zp-2d-to-30 f0,e5,53,e7 \
  --inject-cycle 12794852 \
  --print-c64py-command programs/BruceLee.prg /path/to/roms
```

## Tools and code references

| Piece | Location |
|-------|----------|
| `BRANCH_TRACE` PCs | [cpu.py](../cpu.py) (includes `$00FE`, `$010F`, `$088A`, …) |
| Loader milestones / src write window | [memory.py](../memory.py) (`C64PY_LOADER_PTR_*`, `C64PY_LOADER_PTR_SRC_COUNT_*`; flush includes **`sta00fa_zp2d_before_e5f0`**) |
| `STA ($2D),Y` hook @ `$00FA` | [cpu.py](../cpu.py) `_sta_indy` |
| Debug inject | [cpu.py](../cpu.py) `_maybe_apply_debug_inject`; [C64.py](../C64.py) `--debug-inject-at-cycle`, `--debug-inject-map`, **`--debug-inject-file`** |
| Compare branches | [scripts/compare_loader_branches.py](../scripts/compare_loader_branches.py) |
| VICE `INC $2F` counts | [scripts/vice_trace_loader_counts.py](../scripts/vice_trace_loader_counts.py) |
| VICE / c64py JSR band counts | [scripts/vice_trace_loader_jsr_counts.py](../scripts/vice_trace_loader_jsr_counts.py), `C64PY_LOADER_JSR_COUNT` |
| VICE line → inject map | [scripts/vice_trace_to_inject.py](../scripts/vice_trace_to_inject.py) |
| VICE `m` dump → inject file | [scripts/vice_mem_dump_to_inject.py](../scripts/vice_mem_dump_to_inject.py) |
| VICE monitor automation | [scripts/vice_monitor_client.py](../scripts/vice_monitor_client.py) |
| Regen Bruce log + compare | [scripts/regen_bruce_fresh_compare.sh](../scripts/regen_bruce_fresh_compare.sh) |
| Planning / roadmap | [docs/LOADER_DEBUG_PLAN.md](LOADER_DEBUG_PLAN.md) |
| Static loader disasm / CIA scan | [scripts/loader_map_dump.py](../scripts/loader_map_dump.py), [scripts/disasm6502.py](../scripts/disasm6502.py) |
| RMW / port | [test/test_6510_rmw_port.py](../test/test_6510_rmw_port.py) |

## Loader static map (PRG + pointers)

Regenerate the listing anytime:

```bash
python3 scripts/loader_map_dump.py programs/BruceLee.prg
```

### `$0840`–`$08AE` driver (on-disk = runtime PC)

The outer driver lives entirely inside the PRG file (load `$0801`). Highlights:

- **`$0841` / `$0849` / `$0856` / `$0865` / `$088B`:** repeated **`JSR $016F`** (16-bit / table helper in page 1).
- **`$087F`–`$0889`:** 16-bit add into **`$F9`/`$FA`** from **`$FC`/`$FD`** (pointer arithmetic; not `$2D`–`$30` directly).
- **`$088B`:** **`JSR $01A7`** — enters the **page‑$01** copy/helper that contains **`$00FA` `STA ($2D),Y`**, **`$010D` `INC $2F`**, **`$0113` `LDA ($2F),Y`**, etc. That region is **not** present at `$01xx` inside the `.prg` file (RAM is filled at run time).
- **`$088E`:** **`BEQ $082A`** — long backward branch when the copy routine reports “done”.
- **`$0894`–`$08AE`:** alternate path using **`($FE),Y`** (table stream in ZP **`$FE`/`$FF`**), **`STA $01`**, **`CLI`**, **`JMP $080D`**.

So: **destination/source pointers `$2D`–`$30` are owned by the code reached via `JSR $01A7`**, not by the snippet at `$0840` itself.

### `$00F8`–`$0135` (RAM helper)

Not contained in the PRG file as a linear image at those addresses (file starts at `$0801`). Disassemble this window in VICE with **`d 00f8`** after the loader has installed it, or use a CPU trace. The on-disk sequence that **includes** opcode **`$91 $2D`** (`STA ($2D),Y`) starts at file address **`$0918`**; bytes **after** that opcode are **not** a clean instruction stream in the file (interleaved data / relocation) — treat **`loader_map_dump.py`**’s second block as a **signature anchor**, not a full high-level “decompressor” listing.

### Pointer provenance (from static driver + known RAM routine)

| ZP / role | Set in PRG driver? | Notes |
|-----------|-------------------|--------|
| `$2D`/`$2E` dest | No (in `$01A7` path) | Destination for `STA ($2D),Y` @ `$00FA`. |
| `$2F`/`$30` source | No (in `$01A7` path) | Advanced by `INC $2F` / `INC $30` in helper; **+2 bug** = two extra inner iterations vs VICE. |
| `$F9`/`$FA`, `$FC`/`$FD` | Yes (`$087F`–`$0889`) | 16-bit accumulate before calling `$01A7`. |
| `$FE`/`$FF` | Used `$0897` | Indirect load path for table / length. |
| `$F6`–`$F8` | Yes (`$084E`–`$0852`, `$085E`–`$087D`) | Local driver state / loop counter / bit bucket. |

This matches a **generic “table + memcpy” packager**: outer loop at **`$0881`** (ADC/`JSR $01A7`) and inner bump of **`$2F`**. Whether bytes are **literal copy** or **filtered** is not fully settled from static PRG alone; the helper would need a full RAM disasm.

### `$0849` and “SMC”

In **`programs/BruceLee.prg`**, address **`$0849`** is opcode **`$20`**: **`JSR $016F`**, not a store into code. Any **self-modifying** behaviour must be verified **at runtime** (e.g. watch stores over `$0840`–`$08C0` or compare RAM bytes to file). The earlier “SMC @ `$0849`” note in informal discussion often mixed **first store to `$E5F0`** (watchpoint) with **PC** — use the static map above to avoid that confusion.

### Raster / CIA / IRQ (static scan)

- In the **`$0840`–`$08AE`** and **`$0918`–`$0980`** windows: **no** operands **`$D012`**, **`$D011`**, **`$DC0D`**, **`$DD0D`** in the disassembly listing.
- **Whole PRG** still contains at least one **`LDA $DC0D`** at **`$0D64`** (unrelated subroutine — likely delay/IRQ ack elsewhere on tape/disk path).
- **Conclusion for the hot copy loop:** there is **no evidence** in these windows that the loader **busy-waits on raster or CIA IRQ**; timing-sensitive bugs should still be tracked for **VIC steal / badlines** at the **CPU/RMW** level, but not because this driver obviously reads `$D012`.

### Stack / bulk inject

[`C64.py`](../C64.py) accepts **`--debug-inject-file PATH`**: one **`addr=value`** line per row (hex), **`#` comments**. Pairs are applied **before** **`--debug-inject-map`** (map appends and can override duplicate addresses if listed later — currently duplicates append in order; last write in `debug_inject_writes` wins in `_maybe_apply_debug_inject`).

Example fixture: [test/fixtures/debug_inject_stack.example.txt](../test/fixtures/debug_inject_stack.example.txt).

Workflow: capture **`m 0100 01ff`** (and loader ZP) from VICE JSONL → convert to lines → add **`--debug-inject-file`** next to existing inject cycle → re-run **`compare_loader_branches`**.

**Note:** `compare_loader_branches` needs a **fresh** Bruce log with **`BRANCH_TRACE`** on **`$00FE`/`$010F`/`$088A`** and the correct **`STA_INDY_TRACE`** anchor. Older logs (e.g. `brucelee_debug_branch3.log`) can show **mismatch at idx=0** immediately because the hot-path PCs were not traced yet.

## Hypothesis (active)

- The **+2** on the source pointer is **two extra executions** of the **`INC $2F`** path (or equivalent), not a random KERNAL constant.
- **Phase slip** vs VICE after a long matching **(pc, take, z)** prefix supports an **off-by-one/two** in loop structure or a **missing/extra** call/return on the **`$0881` / `JSR $0103` / `JSR $00FA`** path—not badlines/IRQ in the measured window (0 IRQ there in c64py).

## Next steps (checklist)

- [x] Re-run **`compare_loader_branches`** after **inject** at **`--inject-hint`** cycle **12794852** with **full stack + ZP + regs** from **one** live JSONL capture → **idx unchanged (105706)**; inject **PC** was **`$088A`**, not **`$010F`** (see § [Inject semantics](#inject-semantics-and-capture-coherence-apr-2026)).
- [ ] Re-run inject using **`c64py_cyc_last_010f_before_mismatch`** from **`compare_loader_branches --inject-hint`** (and map from the hint, plus optional stack file).
- [x] Count **`JSR $0103`** / **`JSR $00FA`** from **`$087E`–`$0884`**: **`C64PY_LOADER_JSR_COUNT`** + [`vice_trace_loader_jsr_counts.py`](../scripts/vice_trace_loader_jsr_counts.py) (see [JSR counts](#jsr-counts-outer-driver)).
- [x] **`sta00fa_zp2d_before_e5f0`** in `loader_ptr_src_count.log` matches VICE **39162** `STA @ $00FA` in the same bracket — JSR-only gap ~46 is trace **cycle** vs emu **instruction** boundary, not wrong dest/store count.
- [ ] Optional: VICE **`trace exec 00f8 0135`** and smaller logs; optional future **PC-filtered** c64py trace in [debug.py](../debug.py).
- [ ] If a specific opcode is suspected, add a **focused unit test** (pattern: `test_6510_rmw_port.py`).

## Injection experiments log

| Date | c64py cycle | map (short) | result (e.g. compare idx, crash, OK) |
|------|-------------|-------------|----------------------------------------|
| (template) | 12794852 | `--match-vice-cycle 90487723`, ZP from monitor | Run `compare_loader_branches` again after inject; note new **idx** if phase aligns |
| 2026-04-01 | 12794852 | `a=d6,x=da,y=00,p=a5,2d=f0,2e=e5,2f=53,30=e7` | `compare_loader_branches`: **idx stays 105706**; first mismatch regs match (A/X/Y), but **PC still $088A vs $010F** and subsequent events diverge → ZP+regs alone not enough to re-phase |
| 2026-04-01 | 12794852 | Full **`0100`–`01ff`** via `vice_mem_dump_to_inject.py --jsonl` + map **`a=5a,x=db,y=00,p=24,2d=f3,2e=d9,2f=57,30=db`** from **same** `mismatch_*` JSONL stop | **`DEBUG_INJECT` at cyc=12794852 with PC=`$088A`**; vs **`vice_full_trace.log`**: **idx still 105706**; A/X at mismatch row reflect **live** capture, not archive **d6/da** — confirms coherent RAM snapshot does not fix **sequence** vs old trace |
| 2026-04-01 | (VICE-only) | `x64sc -remotemonitor …` + `vice_monitor_client.py --preset e5f0_second_00fa` | Captured stop at `.C:00fa ... A:20 X:E7 Y:00 ...` and ZP `$2D-$30 = f0 e5 53 e7` into `vice_capture_e5f0_second_00fa.jsonl` (usable to build inject maps reliably) |
|      |             |             |                                        |

(Add rows as you try snapshots.)

## Custom “inject function” hook (not implemented)

Current inject only applies **memory + CPU registers** once. A **callable hook** (e.g. env `C64PY_DEBUG_HOOK=module:function` at a cycle) would be a separate small feature if you need arbitrary Python side effects.
