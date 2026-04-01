# Loader / VICE trace debugging — planning

Short **roadmap and status** for the Bruce Lee loader investigation. Deep technical notes, commands, and references live in [bruce_lee_loader_investigation.md](bruce_lee_loader_investigation.md).

## Objective

Explain and fix the **c64py vs VICE** divergence on Bruce Lee: wrong byte around **`$E5F0`**, source pointer **`$2F/$30`** effectively **two bytes ahead** in c64py, and a long matching prefix of **`(pc, branch-take, Z)`** in [`scripts/compare_loader_branches.py`](../scripts/compare_loader_branches.py) followed by **phase slip** (same take/Z but **different PC ordering** vs the archived VICE trace).

## Done (tooling and measurements)

- **Branch comparison** anchored on first **`STA ($2D),Y` @ `$00FA`** with **`eff=$4CF5`** (c64py) / **A:CC X:4F** (VICE).
- **Loader metrics** in c64py: **`sta00fa_zp2d_before_e5f0`**, JSR counts from **`$087E`–`$0884`**, histograms — aligned with VICE on **store counts**; remaining **~46** JSR gap explained as **cycle-stamp vs instruction-boundary** semantics, not wrong destination progression.
- **VICE automation**: [`scripts/vice_monitor_client.py`](../scripts/vice_monitor_client.py) presets (**`e5f0_second_00fa`**, **`mismatch_90487723`**, etc.), JSONL + text logs.
- **Inject pipeline**: [`scripts/vice_mem_dump_to_inject.py`](../scripts/vice_mem_dump_to_inject.py) (monitor **`m`** / JSONL → **`--debug-inject-file`**), [`scripts/vice_trace_to_inject.py`](../scripts/vice_trace_to_inject.py), [`C64.py`](../C64.py) **`--debug-inject-at-cycle`**, **`--debug-inject-map`**, **`--debug-inject-file`**.
- **`C64.py` fix**: **`--debug-inject-map`** parsing no longer raises **`NameError`** (undefined `source` in error path).
- **Regen script**: [`scripts/regen_bruce_fresh_compare.sh`](../scripts/regen_bruce_fresh_compare.sh).

## Recent findings (2026-04)

1. **Archived trace vs live capture**  
   `compare_loader_branches` is tied to a fixed VICE line (**cycle ~90487723** @ **`$010F`** in **`vice_full_trace.log`**). A **new** `x64sc` run uses a **different** STOPWATCH baseline and sampling instant; the **`mismatch_90487723`** preset may stop near that line but the **numeric cycle** on the **`.C:010f`** row can differ (e.g. **90627944**). Treat as **run-to-run / monitor timing**, not necessarily a broken preset.

2. **Inject cycle ≠ “same instruction as VICE”**  
   Debug inject runs at the **start** of **`cpu.step()`** when **`cycles >= --debug-inject-at-cycle`**, **before** the opcode fetch. The cycle **12794852** from **`compare_loader_branches`** is c64py’s **first sequence mismatch** index; at that instant the **PC was `$088A`**, not **`$010F`**, while the archived VICE mismatch line shows **`$010F`**. So **`--inject-hint`**’s cycle pairs **semantic anchor** (branch stream) with **different instruction boundaries** on each side.

3. **Coherent snapshot requirement**  
   For meaningful RAM experiments, **registers, ZP, and stack** must come from the **same** VICE stop (same JSONL / log). Mixing **regs from `vice_full_trace.log`** with **stack from another capture** invalidates comparison against the **archived** trace’s register columns.

4. **Stack + ZP + regs inject (single live JSONL)**  
   Full **`$0100`–`$01FF`** from capture + map **`a,x,y,p,$2D-$30`** from the **same** stop still yielded **`idx=105706`** vs **`vice_full_trace.log`**: **branch order** did not realign; at the mismatch row **A/X** no longer match the **archive** (expected if the map reflected the **live** stop). Confirms **phase slip** is not fixed by **one** full-page stack inject at the **hint** cycle alone.

5. **Prefix histogram + local divergence (`--prefix-pc-counts`, `loader_branch_window`)**  
   Global counts of **`$00FE`/`$010F`/`$088A`** match over the **105 706**-event prefix (**35886 / 35637 / 34183**). **Locally** at **`idx=105706`**: after the same **`$010F`** at **105705** (c64 **A=`$0A`**, VICE **A=`$AA`**), **VICE** executes **another** **`$010F`** (**A=`$D6`**, cycle **90487723**; second **`BNE`** after **`JSR $0103`** / **`INC $2F`** — see **`vice_full_trace`** ~**90487671→90487723**), while **c64py** goes to **`$088A`**. So c64py skips that **inner** **`$010F`** beat; totals still match because the multiset is rearranged elsewhere. Tool: [`scripts/loader_branch_window.py`](../scripts/loader_branch_window.py).

6. **Inject at `c64py_cyc_last_010f_before_mismatch` with log-derived map**  
   When the map matches current RAM/regs (**no-op** `DEBUG_INJECT`), mismatch **idx** stays **105706** — expected; changing phase requires correcting **emulation timing** or **unlogged** state, not re-poking identical ZP/regs.

7. **`--accurate-vic` vs archived `vice_full_trace.log`**  
   A c64py run with **`--accurate-vic`** shifts **anchor cycles** and **branch-event count** (~100k vs ~120k events to the same wall time); it **cannot** be compared meaningfully to a trace captured with the **fast** VIC path. For accurate-VIC work, capture a **new** VICE trace (or align on semantic milestones only).

## Next actions (priority order)

1. **Re-run inject** using **`--debug-inject-at-cycle`** from **`compare_loader_branches --inject-hint`**: it now prints **`c64py_cyc_last_010f_before_mismatch`** (last **`BRANCH_TRACE pc=$010F`** with **`cyc` < first mismatch) plus a suggested **`--debug-inject-map`** parsed from that log line (regs + ZP). Add **`--debug-inject-file`** from VICE when stack matters.
2. ~~**Optional**: extend **`compare_loader_branches --inject-hint`**~~ **Done:** hint prints mismatch cycle vs **last `$010F`** before mismatch and a stub **`C64.py`** command.
3. **`$0103` helper / second `$010F`:** disassemble or trace (**`loader_branch_window`**, VICE **`sed`/`rg`** around **90487671**) to find why c64py reaches **`$088A`** without the extra **`$010F`** (**`D0` @ `$010F`**, **A=`$D6`**) that VICE runs after **`INC $2F`**. Suspects: wrong **cycles** inside **`JSR $0103`**, **`DEC $D020`**, port **`$01`**, or **RTS** target.
4. **Unit tests** if a specific opcode / RMW / port behaviour is isolated (pattern: [`test/test_6510_rmw_port.py`](../test/test_6510_rmw_port.py)).

## References

| Topic | Location |
|--------|----------|
| Full investigation | [docs/bruce_lee_loader_investigation.md](bruce_lee_loader_investigation.md) |
| Inject implementation | [cpu.py](../cpu.py) `_maybe_apply_debug_inject`; [C64.py](../C64.py) CLI |
| README pointer | [README.md](../README.md) (debug / Bruce Lee link) |
