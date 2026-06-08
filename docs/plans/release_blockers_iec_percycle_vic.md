# Release blockers: IEC logical JSON bridge + per-cycle VIC

This document splits two large release items into **parallel workstreams** with explicit **dependencies**, **acceptance criteria**, **file pointers**, and **worker skill tags** (L = junior-friendly, M = needs emulator familiarity, H = deep hardware/protocol).

Use it to assign tasks to contributors with different experience levels. **Merge order** matters only where noted; otherwise streams can proceed in parallel on separate branches.

---

## 0. Goals and “done” for the release

### IEC (TCP / headless drive server)

**User-visible goal:** With `--tcp-drive`, a stock KERNAL program can use **`OPEN` / `PRINT#` / `INPUT#` / `CLOSE`** against the remote D64, not only **`LOAD` / `SAVE`** (fast hooks). The same logical operations the in-process drive sees must flow as **JSON** to `c1541_emulator` (or hardware peers implementing the same contract).

**Technical goal:** CIA2 bit-banged line updates must drive (or be decoded into) **`IECBus.send_command` / `send_byte` / `receive_byte`** so `TcpDriveClient` emits `listen`, `open_channel`, `send_byte`, etc.

**Acceptance (release bar):**

1. `test/fixtures/disk_host_write.bas` completes on **`--tcp-drive`** without hang: creates `HOSTSEQ`, writes a line, **`SAVE "HOSTPRG",8`**.
2. `test/fixtures/disk_host_read.bas` reads `HOSTSEQ` and prints the line on TCP.
3. Automated test (pytest) that asserts **JSON message sequence** (or RPC counters) for a minimal `OPEN` + one `PRINT#` + `CLOSE` (can use scripted CPU or short BASIC smoke with cycle cap). Use a **real** `c1541_emulator` subprocess—no mock TCP peer (§6.3).
4. No regression: existing `fast_load` / `fast_save` paths still pass.

### Per-cycle VIC

**User-visible goal:** `--video-rendering per-cycle` (or agreed flag) reproduces effects that **per-raster** misses (FLI, border tricks, sub-row color/charset).

**Acceptance (release bar):**

1. Flag documented in `README.md` / CLI help; default remains **per-raster** (or current default).
2. At least one **deterministic** regression: prefer a **committed expected frame hash** (see §6) from `scripts/render_n_frames.py` (or a dedicated harness) where per-raster ≠ per-cycle and per-cycle matches that golden. Use a **minimal homebrew-style** test pattern where possible so CI does not depend on commercial game frames.
3. Performance documented (expected slowdown). Implement the per-cycle path **in Python first**; replicate **hot paths in Rust** only after correctness is settled (§6). Rust is **not** required for the first release if the Python path is correct.

---

## 1. Workstream A — IEC: KERNAL → logical bus → JSON

### A0 — Baseline and vocabulary (L, ~0.5 day)

**Owner skills:** Read Python, run pytest, read docs.

**Tasks:**

1. Read `docs/drive_emulator.md` (TCP JSON contract) and `drives/tcp_drive_client.py` (`_send`, `on_listen`, `iec_receive_byte`, …).
2. Read `memory.py` `apply_cia2_port_a_to_iec_bus` and `iec_bus.py` logical phase docstring (LISTEN / OPEN / UNLISTEN).
3. Confirm current behavior: `OPEN` under TCP hangs because **only wires** update; **no** `send_command`.

**Deliverable:** Short comment in `iec_kernal_bridge.py` module docstring (already started) or a one-page note — no code required.

**Depends on:** Nothing.

**Blocks:** Nothing (informational).

---

### A1 — Phase 0 tap (L/M, ~1 day) **DONE in tree as of this plan**

**Goal:** Whenever a `TcpDriveClient` is attached, record **(cycle, ATN, CLK, DATA)** on each resolved bus change after CIA2 apply.

**Files:** `iec_kernal_bridge.py`, `memory.py` (`iec_kernal_tap` field + hook in `apply_cia2_port_a_to_iec_bus`), `emulator.py` (`_sync_iec_kernal_tap`).

**Tests:** `test/test_iec_kernal_tap.py`.

**Acceptance:** Pytest green; tap `transition_count` increases when `cia2_pra` changes IEC-relevant bits.

**Depends on:** A0.

**Blocks:** A2 (decoder needs traces), A5 (debug).

---

### A2 — Capture / replay harness (M, ~2–3 days)

**Goal:** Save **tap events** to JSONL (guarded by env var, e.g. `C64PY_IEC_TAP_JSONL=/path`) for one scripted run. Optional **replay** into a dummy `IECBus` listener to validate ordering.

**Tasks:**

1. Add optional flush in `KernalIecTap` or a small wrapper: append `{ "cyc", "atn", "clk", "data" }` per transition.
2. Document format in `docs/drive_emulator.md` appendix or comment in bridge file (keep schema stable).
3. Script: `scripts/dump_iec_tap.py` running `C64.py` with `--tcp-drive` and a tiny `.bas` that only does `OPEN` then stop — proves file I/O for debugging.

**Acceptance:** One checked-in golden `.jsonl` fragment (few ms of OPEN) in `test/fixtures/` **or** generated in CI with skip if no ROMs — prefer **unit-level** golden from synthetic `MemoryMap` only to avoid ROM dependency.

**Depends on:** A1.

**Blocks:** A3 (decoder validation).

---

### A3 — Wire protocol decoder state machine (H, ~1–2 weeks)

**Goal:** From **line transitions** (and **timing** if KERNAL waits on CLK/DATA), infer **byte under ATN** (commands + secondary addresses) and **data bytes** (filename, `PRINT#` payload).

**Sub-phases:**

1. **A3a — ATN command bytes:** LISTEN `0x20+dev`, TALK `0x40+dev`, UNLISTEN `0x3F`, UNTALK `0x5F`, secondary `0x60|0xE0|0xF0` + channel. Map each completed byte to `IECBus.send_command(byte)` (or internal equivalents that `TcpDriveClient` already hooks).
2. **A3b — OPEN filename:** Bytes after `OPEN` secondary until UNLISTEN; buffer then call path that matches `open_channel` semantics (reuse `IECBus` logic — **prefer calling into `IECBus` private helpers** or adding a package-level `iec_decode.py` rather than duplicating TCP JSON).
3. **A3c — Data phase:** After listen + data secondary, `PRINT#` / `INPUT#` bit-level EOI; align with `send_byte` / `receive_byte` and drive `request_byte` / `iec_receive_byte`.

**Files (expected):** `iec_kernal_bridge.py` (grow decoder class), possibly `iec_bus.py` (expose a narrow “inject command” API if needed), `drives/tcp_drive_client.py` (only if hook points missing).

**References:** C64 KERNAL IEC routines (disassembly or documented algorithm), 1541 IEC layer (Larry Greenhill summaries), VICE traces for byte timing.

**Acceptance:**

- Unit tests with **synthetic** line sequences (no full C64) for: LISTEN+OPEN+filename+UNLISTEN; LISTEN+DATA+one byte+UNLISTEN.
- Integration: `OPEN` on TCP returns without infinite loop (cycle-bounded test).

**Depends on:** A1, A2 helpful.

**Blocks:** A4.

**Risk:** KERNAL uses **software delays**; if CPU and bus are not stepped in the right order, CLK sampling may be wrong — document requirement: decoder runs **after** `apply_cia2_port_a_to_iec_bus` on the same memory snapshot the CPU will see on the next read.

---

### A4 — Integration with `TcpDriveClient` and server (M/H, ~3–5 days)

**Goal:** End-to-end BASIC fixtures on `--tcp-drive`.

**Tasks:**

1. Ensure `notify_bus_change` or new hook triggers decoder tick if needed (today may be no-op — only extend if bit-level path requires per-opcode stepping).
2. Drive server `c1541_emulator.py`: confirm JSON handlers cover all emitted ops; extend if OPEN secondary variants missing.
3. Add pytest **integration** gated on ROMs. **Always spawn** a real `c1541_emulator` subprocess (same as production); do **not** use a mock TCP peer for these tests—longer runs are acceptable to avoid false greens (§6).

**Acceptance:** `disk_host_write.bas` / `disk_host_read.bas` manual recipe in `test/fixtures/README_disk_bas.md` works for TCP section.

**Depends on:** A3.

**Blocks:** Release (IEC side).

**Parallel note:** A4 can start **documentation and server audit** in parallel with A3 if interfaces are frozen.

---

### A5 — Hardening (M, ~2–4 days)

- Timeouts: if decoder stalls, log last N transitions; optional synthetic NAK (align with existing “no drive” behavior).
- **1571 / dual** devices: out of scope for v1 unless trivial; document “device 8 only” if so.
- **Rust fast path:** If `C64PY_USE_RUST_FAST=1`, confirm CIA2 writes still hit `apply_cia2_port_a_to_iec_bus` — add test or doc caveat. IEC decode itself: **Python first**, Rust later only where profiling shows need (§6).

**Depends on:** A4.

---

### IEC dependency graph (merge order)

```text
A0 ─→ A1 ─→ A2 ─┐
                  ├─→ A3 ─→ A4 ─→ A5
                  │
            (parallel docs/server audit)
```

**Suggested branches:**

- `feature/iec-kernal-tap` — A1 (land first).
- `feature/iec-tap-jsonl` — A2.
- `feature/iec-wire-decoder` — A3 (largest).
- `feature/iec-tcp-open-e2e` — A4 + fixture README updates.

---

## 2. Workstream B — Per-cycle VIC

### B0 — Requirements freeze (L/M, ~0.5 day)

**Goal:** Pick **one** golden demo (FLI or border trick—prefer something **small and original** to CI). **Primary artifact:** one or more **expected frame hashes** checked into the repo (§6). Optional: generate PNG **locally only** when debugging a failure (not required in git).

**Deliverable:** Section in this doc or `docs/per_cycle_vic.md` listing chosen program + expected hash constant(s) / file path.

**Depends on:** Nothing.

**Blocks:** B6 (golden test).

---

### B1 — Cycle budget and buffer layout (M, ~1–2 days)

**Goal:** Fix numeric bounds: PAL/NTSC lines × cycles/line; visible window; memory for snapshots (reuse `beam_vic_flat` patterns if applicable).

**Files:** `memory.py`, `graphics.py` entrypoints, any `vic_*cycle*` engine if present.

**Acceptance:** Written constants + `ensure_*` allocator; no rendering yet.

**Depends on:** B0.

**Blocks:** B2.

---

### B2 — Per-cycle VIC sampler (H, ~1 week)

**Goal:** Each **CPU cycle** in the visible region, record minimal VIC state needed for pixels (regs, fetch state, matrix/color pointers as needed).

**Files:** Likely `cpu.py` / main step loop / `vicii_cycle_engine` (search codebase for `ViciiCycleEngine`).

**Acceptance:** Unit test: fixed small snippet of cycles produces deterministic buffer length and non-zero diffs when `$D011` toggles mid-line (synthetic).

**Depends on:** B1.

**Blocks:** B3, B4.

---

### B3 — Text / MCM / ECM pixel walkers (H, ~1–2 weeks)

**Goal:** For each mode, emit **one pixel per emulated cycle** (or per half-cycle if you document luma phase — pick one convention and stick to it).

**Sub-split for workers:**

- **B3a** — Text mode only (L/M under review).
- **B3b** — Bitmap hires/MCM (M/H).
- **B3c** — Sprites overlay with same timing as current beam work (M/H; may reuse sprite snapshot logic).

**Depends on:** B2.

**Blocks:** B5.

---

### B4 — CLI / config wiring (L, ~1 day)

**Goal:** `--video-rendering per-cycle` (or config TOML key) routes to new pipeline; default unchanged.

**Files:** `C64.py`, `docs/config.md` (if schema extended).

**Acceptance:** Help text + parser rejects unknown value.

**Depends on:** B1 (flag can land early behind “not implemented” error until B3 ready — coordinate to avoid broken main).

**Blocks:** None (can merge after B3 if guarded).

---

### B5 — Integration with `graphics.py` / pygame (M, ~3–5 days)

**Goal:** Fill host framebuffer from per-cycle buffer; respect `vic_render_snapshots` / turbo modes.

**Depends on:** B3.

**Blocks:** B6.

---

### B6 — Golden frame regression (M, ~2 days)

**Goal:** Script + **committed expected hash(es)** for the chosen frame(s); CI runs with `--no-config` and skips if ROMs/assets missing. When a test fails, the harness may **write a PNG to a temp path** for human inspection—**do not** commit game screenshots. Hashes are weaker for “what broke?” debugging; mitigate with a **tiny** repro program and good commit messages when golden hash changes.

**Depends on:** B0, B5.

**Blocks:** Release (VIC side).

---

### B7 — Performance and opt-in docs (L/M, ~1–2 days)

Document slowdown; optional profiling hooks.

---

### B8 — (Optional post-release) Rust core (H)

After Python `B2+B3` is correct and profiled, port **hot** sampler/renderer paths to `c64py-core` behind the same flag (§6). Parity tests between Python and Rust tiers are desirable once both exist.

**Depends on:** B6 stable in Python.

---

### VIC dependency graph

```text
B0 ─→ B1 ─→ B2 ─→ B3a ─┐
                        ├─→ B5 ─→ B6 ─→ B7
            B2 ─→ B3b ─┘
            B2 ─→ B3c ─┘
B1 ─→ B4 (parallel)
B6 ─→ B8 (optional)
```

**Parallelization:** B3a / B3b / B3c are three different workers **after** B2 API is frozen (interface types + “pixel walker” function signature checked in by lead).

---

## 3. Cross-cutting checklist (any worker)

- Run `python -m py_compile` on touched files.
- Prefer **small PRs**: A1 alone, then A2, then A3 in chunks (A3a, A3b, A3c).
- **Do not** weaken `disk_host_*.bas` fixtures: they stay as **SEQ + SAVE** regression targets for TCP.
- Coordinate with **drive JSON version** if message types are added (bump version field if present).
- **TCP IEC integration tests:** real `c1541_emulator` subprocess only (§6).

---

## 4. Suggested weekly assignment (example team of 4)

| Week | Worker 1 (H) | Worker 2 (M) | Worker 3 (M) | Worker 4 (L) |
|------|----------------|--------------|--------------|--------------|
| 1 | A3a decoder skeleton | B2 sampler spike | A2 tap JSONL | B4 CLI flag stub |
| 2 | A3b OPEN buffering | B3a text walker | A4 server audit | B1 buffer layout tests |
| 3 | A3c data/EOI | B3b bitmap | A4 pytest TCP OPEN | B6 golden harness prep |
| 4 | A5 hardening | B5 pygame glue | integration burn-in | docs / release notes |

---

## 5. File index (quick reference)

| Area | Files |
|------|--------|
| CIA2 → wires | `memory.py` |
| Tap / future decoder | `iec_kernal_bridge.py` |
| Logical bus | `iec_bus.py` |
| TCP JSON client | `drives/tcp_drive_client.py` |
| Headless server | `drives/c1541_emulator.py` |
| Fixtures | `test/fixtures/disk_host_*.bas`, `test/fixtures/README_disk_bas.md` |
| Per-cycle design | `docs/per_cycle_vic.md` |
| Render harness | `scripts/render_n_frames.py` (verify in repo) |

---

## 6. Project decisions (answered)

These choices are **fixed** for this release track so workers do not re-litigate them.

### 6.1 Python first, Rust for speed later

- Implement **IEC KERNAL bridge**, **per-cycle VIC**, and related logic **in Python** first (same approach as the rest of c64py).
- After behavior is correct and bottlenecks are known, **replicate hot paths in Rust** (`c64py-core`) for throughput—see workstream **B8** and IEC notes in **A5**.

### 6.2 Per-cycle golden: prefer hash in repo

- **Primary:** store **expected frame hash(es)** in the test suite or a small text fixture—avoids **copyright risk** from checking in game PNGs and keeps the repo light.
- **Tradeoff:** a hash alone does not show *what* changed; mitigate with a **minimal** dedicated test program, optional **local** PNG dump on failure (temp dir, not committed), and clear golden updates in PRs.
- If the team later wants stronger visuals, consider **synthetic** reference images you own, or git-lfs **non**-commercial assets—not required for v1.

### 6.3 TCP integration tests: always real `c1541_emulator`

- **Always spawn** the real `python -m c64py.drives.c1541_emulator` (or equivalent) subprocess for IEC-over-TCP integration tests.
- **Do not** replace that with a mock socket server: mocks risk **false positives**; slower, faithful tests are preferred.
