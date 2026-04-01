# Bruce Lee loader: trace divergence investigation

Persistent notes for the c64py vs VICE mismatch (wrong byte at `$E5F0`, source pointer `$2F/$30` two bytes ahead in c64py). Use this file to resume work without relying on chat context.

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

**`--inject-hint`:** on the first **pc/take/z** mismatch, prints **`c64py_cyc`** and **`vice_cyc`** plus a stub command for [`vice_trace_to_inject.py`](../scripts/vice_trace_to_inject.py) (replace ZP bytes from a VICE monitor dump).

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

After **`CPU.cycles >= N`** at an instruction boundary, poke RAM and/or **A/X/Y/P** once (stderr + optional Bruce log).

```bash
python3 C64.py programs/BruceLee.prg --headless --turbo \
  --max-cycles 13150000 --autoquit --rom-dir /path/to/roms \
  --debug-inject-at-cycle 12794852 \
  --debug-inject-map a=d6,x=da,y=00,p=a5,2d=f0,2e=e5,2f=53,30=e7
```

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
| Debug inject | [cpu.py](../cpu.py) `_maybe_apply_debug_inject`; [C64.py](../C64.py) `--debug-inject-*` |
| Compare branches | [scripts/compare_loader_branches.py](../scripts/compare_loader_branches.py) |
| VICE `INC $2F` counts | [scripts/vice_trace_loader_counts.py](../scripts/vice_trace_loader_counts.py) |
| VICE / c64py JSR band counts | [scripts/vice_trace_loader_jsr_counts.py](../scripts/vice_trace_loader_jsr_counts.py), `C64PY_LOADER_JSR_COUNT` |
| VICE line → inject map | [scripts/vice_trace_to_inject.py](../scripts/vice_trace_to_inject.py) |
| VICE monitor automation | [scripts/vice_monitor_client.py](../scripts/vice_monitor_client.py) |
| RMW / port | [test/test_6510_rmw_port.py](../test/test_6510_rmw_port.py) |

## Hypothesis (active)

- The **+2** on the source pointer is **two extra executions** of the **`INC $2F`** path (or equivalent), not a random KERNAL constant.
- **Phase slip** vs VICE after a long matching **(pc, take, z)** prefix supports an **off-by-one/two** in loop structure or a **missing/extra** call/return on the **`$0881` / `JSR $0103` / `JSR $00FA`** path—not badlines/IRQ in the measured window (0 IRQ there in c64py).

## Next steps (checklist)

- [ ] Re-run **`compare_loader_branches`** after an **inject** at the first known desync cycle; note whether mismatch **index moves** or **vanishes**.
- [x] Count **`JSR $0103`** / **`JSR $00FA`** from **`$087E`–`$0884`**: **`C64PY_LOADER_JSR_COUNT`** + [`vice_trace_loader_jsr_counts.py`](../scripts/vice_trace_loader_jsr_counts.py) (see [JSR counts](#jsr-counts-outer-driver)).
- [x] **`sta00fa_zp2d_before_e5f0`** in `loader_ptr_src_count.log` matches VICE **39162** `STA @ $00FA` in the same bracket — JSR-only gap ~46 is trace **cycle** vs emu **instruction** boundary, not wrong dest/store count.
- [ ] Optional: VICE **`trace exec 00f8 0135`** and smaller logs; optional future **PC-filtered** c64py trace in [debug.py](../debug.py).
- [ ] If a specific opcode is suspected, add a **focused unit test** (pattern: `test_6510_rmw_port.py`).

## Injection experiments log

| Date | c64py cycle | map (short) | result (e.g. compare idx, crash, OK) |
|------|-------------|-------------|----------------------------------------|
| (template) | 12794852 | `--match-vice-cycle 90487723`, ZP from monitor | Run `compare_loader_branches` again after inject; note new **idx** if phase aligns |
| 2026-04-01 | 12794852 | `a=d6,x=da,y=00,p=a5,2d=f0,2e=e5,2f=53,30=e7` | `compare_loader_branches`: **idx stays 105706**; first mismatch regs match (A/X/Y), but **PC still $088A vs $010F** and subsequent events diverge → ZP+regs alone not enough to re-phase |
| 2026-04-01 | (VICE-only) | `x64sc -remotemonitor …` + `vice_monitor_client.py --preset e5f0_second_00fa` | Captured stop at `.C:00fa ... A:20 X:E7 Y:00 ...` and ZP `$2D-$30 = f0 e5 53 e7` into `vice_capture_e5f0_second_00fa.jsonl` (usable to build inject maps reliably) |
|      |             |             |                                        |

(Add rows as you try snapshots.)

## Custom “inject function” hook (not implemented)

Current inject only applies **memory + CPU registers** once. A **callable hook** (e.g. env `C64PY_DEBUG_HOOK=module:function` at a cycle) would be a separate small feature if you need arbitrary Python side effects.
