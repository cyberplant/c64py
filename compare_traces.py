#!/usr/bin/env python3
"""
Trace Comparison Tool for C64 Emulator

Compares our emulator's trace output with VICE traces to find divergences
and timing drift. Uses streaming comparison for large files.

Usage:
    python compare_traces.py program.prg --vice-trace vice_trace.txt
    python compare_traces.py program.prg --vice-trace vice_trace.txt --match-cycles-at 0100
"""

import argparse
import itertools
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections import deque
from dataclasses import dataclass
from typing import BinaryIO, Iterator, List, Optional, Tuple, TextIO

try:
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

# Initialised in main() once --nocolor is known; used as a fallback before that.
console: "Console | None" = None


def make_console(nocolor: bool = False) -> "Console | None":
    """Create a Rich console.

    By default forces ANSI output so colours survive pipes (e.g. ``less -R``).
    With *nocolor=True* the console is set to ``None`` so all output falls
    through to plain ``print()`` — no ANSI escape codes whatsoever.
    """
    if not RICH_AVAILABLE or nocolor:
        return None
    return Console(force_terminal=True, highlight=False)


def print_msg(msg: str, style: str = None):
    """Print message with optional Rich styling"""
    if console and style:
        console.print(msg, style=style)
    elif console:
        console.print(msg)
    else:
        print(msg)


@dataclass
class TraceLine:
    """Parsed trace line"""
    raw: str
    pc: int
    opcode: int
    mnemonic: str
    a: int
    x: int
    y: int
    sp: int
    flags: str
    cycles: int
    line_num: int = 0

    def without_cycles(self) -> str:
        """Return line without cycle count for comparison"""
        return re.sub(r'\s+\d+\s*$', '', self.raw).strip()

    def registers_str(self) -> str:
        """Return register state as string"""
        return f"A:{self.a:02X} X:{self.x:02X} Y:{self.y:02X} SP:{self.sp:02X} {self.flags}"


@dataclass
class SyncAnchor:
    """Result of locating --match-cycles-at in a trace file."""

    byte_offset: int
    """Absolute file offset of the first byte of the matching trace line (use with --*-skip-bytes)."""
    physical_lines_read: int
    """Physical lines read from the search start (including comments/blanks) to reach the match."""
    trace_line: TraceLine
    """The matching parsed line (includes .cycles for sync)."""


def parse_trace_line(line: str, line_num: int = 0) -> Optional[TraceLine]:
    """Parse a VICE-format trace line"""
    line = line.strip()
    # Comment / metadata lines (e.g. "; w <seconds>") are intentionally ignored.
    if line.startswith(";"):
        return None
    if not line or not line.startswith('.C:'):
        return None

    try:
        pc_match = re.match(r'\.C:([0-9A-Fa-f]{4})', line)
        if not pc_match:
            return None
        pc = int(pc_match.group(1), 16)

        opcode_match = re.search(r'\.C:[0-9A-Fa-f]{4}\s+([0-9A-Fa-f]{2})', line)
        opcode = int(opcode_match.group(1), 16) if opcode_match else 0

        mnemonic_match = re.search(r'\s([A-Z]{3})\s', line)
        mnemonic = mnemonic_match.group(1) if mnemonic_match else "???"

        a_match = re.search(r'A:([0-9A-Fa-f]{2})', line)
        x_match = re.search(r'X:([0-9A-Fa-f]{2})', line)
        y_match = re.search(r'Y:([0-9A-Fa-f]{2})', line)
        sp_match = re.search(r'SP:([0-9A-Fa-f]{2})', line)

        a = int(a_match.group(1), 16) if a_match else 0
        x = int(x_match.group(1), 16) if x_match else 0
        y = int(y_match.group(1), 16) if y_match else 0
        sp = int(sp_match.group(1), 16) if sp_match else 0

        flags_match = re.search(r'SP:[0-9A-Fa-f]{2}\s+([N.][V.][-.][B.][D.][I.][Z.][C.])', line)
        flags = flags_match.group(1) if flags_match else "........"

        cycles_match = re.search(r'\s(\d+)\s*$', line)
        cycles = int(cycles_match.group(1)) if cycles_match else 0

        return TraceLine(
            raw=line, pc=pc, opcode=opcode, mnemonic=mnemonic,
            a=a, x=x, y=y, sp=sp, flags=flags, cycles=cycles, line_num=line_num
        )
    except Exception:
        return None


# Default PC ranges for --skip-interrupt-divergences (stock KERNAL / IRQ path).
# Custom IRQ routines in RAM are not covered; use --irq-skip-pc-range for those.
DEFAULT_IRQ_SKIP_PC_RANGES: Tuple[Tuple[int, int], ...] = (
    (0xEA00, 0xFFFF),
)


def parse_hex_irq_range(s: str) -> Tuple[int, int]:
    """Parse ``START:END`` (16-bit hex, inclusive)."""
    s = s.strip()
    if ":" not in s:
        raise ValueError("expected START:END hex range")
    a, b = s.split(":", 1)
    lo, hi = int(a.strip(), 16), int(b.strip(), 16)
    if lo > hi:
        lo, hi = hi, lo
    lo &= 0xFFFF
    hi &= 0xFFFF
    return lo, hi


def pc_xor_irq_skip_ranges(pc_a: int, pc_b: int, ranges: List[Tuple[int, int]]) -> bool:
    """True when exactly one PC is in a skip range (typical game vs KERNAL IRQ slip).

    If both PCs are inside the range, we do not skip (could be a real KERNAL bug or
    unrelated boot mismatch). If neither is in range, do not skip (application code).
    """

    def in_any(pc: int) -> bool:
        return any(lo <= pc <= hi for lo, hi in ranges)

    a_in = in_any(pc_a)
    b_in = in_any(pc_b)
    return a_in != b_in


def _parse_irq_range_arg(s: str) -> Tuple[int, int]:
    try:
        return parse_hex_irq_range(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def count_lines(filename: str) -> int:
    """Count lines in file efficiently"""
    count = 0
    with open(filename, 'rb') as f:
        for _ in f:
            count += 1
    return count


def iterate_trace_file(f: TextIO, start_line: int = 0) -> Iterator[Tuple[int, TraceLine]]:
    """Iterate over trace lines from file handle, yielding (line_num, TraceLine)"""
    for line_num, line in enumerate(f, 1):
        if line_num <= start_line:
            continue
        parsed = parse_trace_line(line, line_num)
        if parsed:
            yield line_num, parsed


def _prepare_binary_trace_read(f: BinaryIO, start_byte: int) -> int:
    """Seek to *start_byte* and align so the next ``readline()`` starts at a full line.

    If *start_byte* points at the beginning of a ``.C:`` trace line, that line is not
    consumed. Returns the number of physical lines consumed during alignment (0 or 1).
    """
    if start_byte <= 0:
        f.seek(0)
        return 0
    f.seek(start_byte)
    pos = f.tell()
    peek = f.read(3)
    if len(peek) < 3:
        f.seek(pos)
        return 0
    # First three bytes of a trace row are b".C:" (must compare 3 bytes, not b".C")
    if peek == b".C:":
        f.seek(pos)
        return 0
    f.seek(pos)
    f.readline()
    return 1


def iter_parsed_trace_binary(
    f: BinaryIO,
    start_byte: int = 0,
) -> Iterator[Tuple[int, int, TraceLine]]:
    """Yield ``(line_start_byte, physical_line_since_prepare, TraceLine)`` for each parsed trace row.

    *physical_line_since_prepare* counts every ``readline()`` after alignment (including
    lines that are not valid ``.C:`` traces).
    """
    align_lines = _prepare_binary_trace_read(f, start_byte)
    phys = align_lines
    while True:
        line_start = f.tell()
        raw = f.readline()
        if not raw:
            break
        phys += 1
        text = raw.decode("utf-8", errors="replace")
        parsed = parse_trace_line(text, phys)
        if parsed:
            yield line_start, phys, parsed


def iter_trace_compare(binary_f: BinaryIO, start_byte: int = 0) -> Iterator[Tuple[int, TraceLine]]:
    """``(physical_line, TraceLine)`` stream for compare/resync (same shape as ``iterate_trace_file``)."""
    for _boff, phys, tl in iter_parsed_trace_binary(binary_f, start_byte):
        yield phys, tl


def get_file_size_mb(filename: str) -> float:
    """Get file size in MB"""
    return os.path.getsize(filename) / (1024 * 1024)


def run_emulator(
    prg_file: str,
    max_cycles: int,
    trace_file: str,
    sync_pc: Optional[int] = None,
    extra_emulator_args: Optional[List[str]] = None,
) -> bool:
    """Run our emulator and generate trace.

    *extra_emulator_args* are appended after the default flags (trace, turbo, headless, …).
    C64.py applies ``--graphics`` after ``--headless``, so graphics wins if both are set.
    """
    cmd = [
        sys.executable, 'C64.py',
        prg_file,
        '--vice-trace', trace_file,
        '--max-cycles', str(max_cycles),
        '--autoquit',
        '--turbo',
        '--headless',
    ]
    if extra_emulator_args:
        cmd.extend(extra_emulator_args)

    # When comparing against VICE we typically sync at a PC address. However, the VIC
    # raster phase at that point can differ between runs due to power-on timing and
    # boot path differences. If requested, set an env var so the emulator can reset
    # raster phase at the sync PC, making drift analysis focus on badline logic.
    env = os.environ.copy()
    if sync_pc is not None:
        env["C64PY_TRACE_SYNC_PC"] = f"{sync_pc:04X}"
    
    try:
        result = subprocess.run(
            cmd,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            env=env,
            timeout=300
        )
        if result.returncode != 0:
            if result.stderr:
                print_msg("Emulator stderr:", "red")
                print(result.stderr)
            if result.stdout:
                print_msg("Emulator stdout:", "red")
                print(result.stdout)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print_msg("❌ Emulator timed out", "red")
        return False
    except Exception as e:
        print_msg(f"❌ Failed to run emulator: {e}", "red")
        return False


def find_address_in_trace(
    filename: str,
    target_pc: int,
    max_search: int = 100_000_000,
    min_cycles: Optional[int] = None,
    start_byte: int = 0,
    resume_flag: str = "--skip-bytes",
) -> Optional[SyncAnchor]:
    """Find first parsed trace row with PC *target_pc* (optional min cumulative cycles).

    Scans at most *max_search* **physical** lines (every ``readline``, including blanks
    and ``;`` comments). *start_byte* seeks before scanning (see *resume_flag* for copy-paste).

    Returns a :class:`SyncAnchor` whose ``byte_offset`` is the file offset of the matching
    line's first byte (use with ``--vice-skip-bytes`` / ``--our-skip-bytes``).
    """
    hint = f" (cycles>={min_cycles:,})" if min_cycles is not None else ""
    skip_hint = f" from byte {start_byte:,}" if start_byte > 0 else ""
    print_msg(
        f"   🔍 Searching PC=${target_pc:04X}{hint}{skip_hint} in {os.path.basename(filename)}...",
        "dim",
    )
    with open(filename, "rb") as f:
        align_lines = _prepare_binary_trace_read(f, start_byte)
        lines_read = align_lines
        while True:
            line_start = f.tell()
            raw = f.readline()
            if not raw:
                print_msg(f"      ❌ Not found in file", "red")
                return None
            lines_read += 1
            if lines_read % 500_000 == 0:
                print_msg(f"      ...scanned {lines_read:,} physical lines", "dim")
            if lines_read > max_search:
                print_msg(f"      ❌ Not found in first {max_search:,} physical lines", "red")
                return None
            text = raw.decode("utf-8", errors="replace")
            parsed = parse_trace_line(text, lines_read)
            if not parsed:
                continue
            if parsed.pc != target_pc:
                continue
            if min_cycles is not None and parsed.cycles < min_cycles:
                continue
            print_msg(
                f"      ✅ Found at physical line {lines_read:,}, byte offset {line_start:,} "
                f"(cyc={parsed.cycles:,})",
                "green",
            )
            print_msg(
                f"      📎 Next session: {resume_flag} {line_start}",
                "cyan",
            )
            return SyncAnchor(
                byte_offset=line_start,
                physical_lines_read=lines_read,
                trace_line=parsed,
            )


def _col_width() -> int:
    """Return per-column character width based on terminal size."""
    try:
        import shutil
        tw = shutil.get_terminal_size(fallback=(160, 40)).columns
    except Exception:
        tw = 160
    return max(40, (tw - 3) // 2)


def _adjust_vice_raw(raw: str, vice_cycle: int, cycle_delta: Optional[int]) -> str:
    """Return the vice raw line with its cycle replaced by the adjusted value.

    adjusted = vice_cycle - cycle_delta  (brings VICE cycles to our scale).
    If *cycle_delta* is None (not yet established) the raw line is returned unchanged.
    """
    if cycle_delta is None:
        return raw.strip()
    adjusted = vice_cycle - cycle_delta
    return re.sub(r'(\s+)\d+\s*$', lambda m: f'{m.group(1)}{adjusted}', raw.strip())


def _row(
    our_text: str,
    vice_text: str,
    our_style: str,
    vice_style: str,
    col: int,
    annotation: str = "",
    vice_cycle: "Optional[int]" = None,
) -> None:
    """Print one side-by-side row to the console.

    *vice_cycle*: when provided, appended as a dim ``[cy: N]`` suffix after
    the vice cell so the raw VICE cycle is always visible even when the line
    is truncated by column width.
    *annotation*: appended after everything in yellow bold (for drift info).
    """
    sep = " | "
    our_cell = our_text[:col].ljust(col)
    vice_cell = vice_text[:col]
    cy_suffix = f"  [cy:{vice_cycle}]" if vice_cycle is not None else ""

    if RICH_AVAILABLE and console:
        line = Text()
        line.append(our_cell, style=our_style)
        line.append(sep, style="dim")
        line.append(vice_cell, style=vice_style)
        if cy_suffix:
            line.append(cy_suffix, style="dim")
        if annotation:
            line.append(f"  {annotation}", style="yellow bold")
        console.print(line)
    else:
        print(f"{our_cell}{sep}{vice_cell}{cy_suffix}{'  ' + annotation if annotation else ''}")


def _show_context_sidebyside(
    before_list: "List[Tuple[TraceLine, TraceLine]]",
    divergence_pair: "Optional[Tuple[TraceLine, TraceLine]]",
    ours_gap: "List[Optional[TraceLine]]",
    vice_gap: "List[Optional[TraceLine]]",
    resync_pair: "Optional[Tuple[TraceLine, TraceLine]]",
    resync_annotation: str = "",
    cycle_delta: "Optional[int]" = None,
) -> None:
    """Render the full divergence block in side-by-side format.

    *ours_gap* / *vice_gap* are parallel lists of the same length; ``None``
    entries represent missing lines on that side (rendered as ``---``).
    """
    col = _col_width()
    sep = " | "

    # Header
    if RICH_AVAILABLE and console:
        hdr = Text()
        hdr.append("OURS".ljust(col), style="bold cyan")
        hdr.append(sep, style="dim")
        hdr.append("VICE", style="bold cyan")
        console.print(hdr)
        console.print(Text("─" * col + sep + "─" * col, style="dim"))
    else:
        print(f"{'OURS':<{col}}{sep}VICE")
        print(f"{'─' * col}{sep}{'─' * col}")

    # Context before divergence (matching lines, dimmed)
    for our, vice in before_list:
        _row(
            f" {our.raw.strip()}",
            f" {_adjust_vice_raw(vice.raw, vice.cycles, cycle_delta)}",
            "dim", "dim", col,
            vice_cycle=vice.cycles,
        )

    # First diverging pair
    if divergence_pair:
        our_d, vice_d = divergence_pair
        _row(
            f">{our_d.raw.strip()}",
            f">{_adjust_vice_raw(vice_d.raw, vice_d.cycles, cycle_delta)}",
            "red bold", "green bold", col,
            vice_cycle=vice_d.cycles,
        )

    # Gap lines (ours_gap / vice_gap aligned, None → ---)
    for our_g, vice_g in zip(ours_gap, vice_gap):
        our_text = f".{our_g.raw.strip()}" if our_g else "---"
        vice_text = (
            f".{_adjust_vice_raw(vice_g.raw, vice_g.cycles, cycle_delta)}"
            if vice_g else "---"
        )
        our_style = "red" if our_g else "dim"
        vice_style = "green" if vice_g else "dim"
        _row(our_text, vice_text, our_style, vice_style, col,
             vice_cycle=vice_g.cycles if vice_g else None)

    # Re-sync line (if found)
    if resync_pair:
        our_r, vice_r = resync_pair
        # Resync: use the NEW delta (vice_r.cycles - our_r.cycles) so the
        # adjusted cycle matches our side exactly at the re-sync point.
        resync_delta = vice_r.cycles - our_r.cycles
        _row(
            f" {our_r.raw.strip()}",
            f" {_adjust_vice_raw(vice_r.raw, vice_r.cycles, resync_delta)}",
            "bold", "bold", col,
            annotation=resync_annotation,
            vice_cycle=vice_r.cycles,
        )

    if RICH_AVAILABLE and console:
        console.print(Text("─" * col + sep + "─" * col, style="dim"))
    else:
        print(f"{'─' * col}{sep}{'─' * col}")


def find_resync(
    our_iter: "Iterator[Tuple[int, TraceLine]]",
    vice_iter: "Iterator[Tuple[int, TraceLine]]",
    lookahead: int,
) -> "Tuple[List[Optional[TraceLine]], List[Optional[TraceLine]], Optional[Tuple[TraceLine, TraceLine]], List[TraceLine], List[TraceLine], int]":
    """Look ahead in both iterators to find the next matching line.

    Returns ``(ours_gap, vice_gap, resync_pair, our_tail, vice_tail, resync_cost)`` where:
    - gaps are parallel lists of the same length with ``None`` for missing sides
    - *resync_pair* is ``None`` if no match found within lookahead
    - *our_tail* / *vice_tail* are the unconsumed buffer items after the resync
      point; prepend these back to the iterators with ``itertools.chain``
    - *resync_cost* is (i+j) for the chosen resync, or a large sentinel when none found
    """
    our_buf: List[TraceLine] = []
    vice_buf: List[TraceLine] = []

    for _ in range(lookahead):
        try:
            _, tl = next(our_iter)
            our_buf.append(tl)
        except StopIteration:
            break

    for _ in range(lookahead):
        try:
            _, tl = next(vice_iter)
            vice_buf.append(tl)
        except StopIteration:
            break

    # Find earliest (i+j) match — keep FIRST occurrence of each signature
    # so repeated instructions in a loop don't map to the wrong position.
    best_i, best_j = None, None
    best_cost = lookahead * 2 + 1
    our_set: dict = {}
    for i, tl in enumerate(our_buf):
        key = tl.without_cycles()
        if key not in our_set:
            our_set[key] = i
    for j, vice_tl in enumerate(vice_buf):
        key = vice_tl.without_cycles()
        if key in our_set:
            i = our_set[key]
            cost = i + j
            if cost < best_cost:
                best_cost = cost
                best_i, best_j = i, j

    if best_i is None:
        # No re-sync: show everything buffered, no None padding needed
        max_len = max(len(our_buf), len(vice_buf), 1)
        ours_gap: List[Optional[TraceLine]] = list(our_buf) + [None] * (max_len - len(our_buf))
        vice_gap: List[Optional[TraceLine]] = list(vice_buf) + [None] * (max_len - len(vice_buf))
        return ours_gap, vice_gap, None, [], [], best_cost

    # Build parallel gap lists with None where one side is shorter
    ours_raw = our_buf[:best_i]
    vice_raw = vice_buf[:best_j]
    max_len = max(len(ours_raw), len(vice_raw))
    ours_gap = list(ours_raw) + [None] * (max_len - len(ours_raw))
    vice_gap = list(vice_raw) + [None] * (max_len - len(vice_raw))
    resync_pair = (our_buf[best_i], vice_buf[best_j])
    our_tail = our_buf[best_i + 1:]
    vice_tail = vice_buf[best_j + 1:]
    return ours_gap, vice_gap, resync_pair, our_tail, vice_tail, best_cost
def _show_cycle_drift_inline(
    before_lines: "List[Tuple[TraceLine, TraceLine]]",
    our_line: "TraceLine",
    vice_line: "TraceLine",
    after_lines: "List[Tuple[TraceLine, TraceLine]]",
    old_delta: int,
    new_delta: int,
    line_num: int,
) -> None:
    """Print a side-by-side context block around a cycle-delta change.

    before_lines and the drift line are displayed with *old_delta* so the
    mismatch on the drift line is visible (adjusted vice ≠ our).
    after_lines are displayed with *new_delta* so they show alignment again.
    """
    col = _col_width()
    sep = " | "
    change = new_delta - old_delta

    # Header
    header = (
        f"⚡ Cycle delta changed at line {line_num:,}: "
        f"Δ was {old_delta:+,}, now {new_delta:+,}  (shift: {change:+,})"
    )
    print_msg(header, "yellow bold")

    if RICH_AVAILABLE and console:
        console.print(Text("─" * col + sep + "─" * col, style="dim"))
    else:
        print(f"{'─' * col}{sep}{'─' * col}")

    # Before lines (old_delta — were aligned)
    for our_b, vice_b in before_lines:
        _row(f" {our_b.raw.strip()}",
             f" {_adjust_vice_raw(vice_b.raw, vice_b.cycles, old_delta)}",
             "dim", "dim", col, vice_cycle=vice_b.cycles)

    # The drift line itself (old_delta — shows the mismatch)
    ann = f"({change:+d} cy shift)"
    our_cell = f"⚡{our_line.raw.strip()}"[:col].ljust(col)
    adj_vice = _adjust_vice_raw(vice_line.raw, vice_line.cycles, old_delta)
    vice_cell = f"⚡{adj_vice}"[:col]
    cy_suffix = f"  [cy:{vice_line.cycles}]"
    if RICH_AVAILABLE and console:
        row = Text()
        row.append(our_cell, style="yellow")
        row.append(sep, style="dim")
        row.append(vice_cell, style="yellow")
        row.append(cy_suffix, style="dim")
        row.append(f"  {ann}", style="yellow bold")
        console.print(row)
    else:
        print(f"{our_cell}{sep}{vice_cell}{cy_suffix}  {ann}")

    # After lines (new_delta — aligned again)
    for our_a, vice_a in after_lines:
        _row(f" {our_a.raw.strip()}",
             f" {_adjust_vice_raw(vice_a.raw, vice_a.cycles, new_delta)}",
             "dim", "dim", col, vice_cycle=vice_a.cycles)

    if RICH_AVAILABLE and console:
        console.print(Text("─" * col + sep + "─" * col, style="dim"))
    else:
        print(f"{'─' * col}{sep}{'─' * col}")


def compare_traces_streaming(
    our_trace: str,
    vice_trace: str,
    ignore_cycles: bool = False,
    match_cycles_at: Optional[int] = None,
    match_min_cycle: Optional[int] = None,
    match_search_max_lines: int = 100_000_000,
    our_skip_bytes: int = 0,
    vice_skip_bytes: int = 0,
    context_lines: int = 10,
    max_lines: Optional[int] = None,
    diffmode: str = "sidebyside",
    resync_lookahead: int = 50,
    drift_context: int = 3,
    skip_drift_report: bool = False,
    quiet_drift_summary: bool = False,
    stop_after_first_divergence: bool = False,
    skip_interrupt_divergences: bool = False,
    irq_skip_pc_ranges: Optional[List[Tuple[int, int]]] = None,
    min_resync_cost_to_report: int = 0,
    squash_repeated_divergences: bool = False,
    find_first_stable_divergence: bool = False,
) -> None:
    """Compare two trace files using streaming (memory efficient)"""
    
    our_size = get_file_size_mb(our_trace)
    vice_size = get_file_size_mb(vice_trace)
    
    print_msg(f"📂 Ours: {our_trace} ({our_size:.1f} MB)", "cyan")
    print_msg(f"📂 VICE: {vice_trace} ({vice_size:.1f} MB)", "cyan")
    print()
    
    # Sync: optional PC match + byte offsets for fast seek on huge VICE logs
    our_sync_byte = 0
    vice_sync_byte = 0
    cycle_offset = 0

    if match_cycles_at is not None:
        print_msg(f"🔄 Searching sync point PC=${match_cycles_at:04X}...", "yellow")

        mmin = match_min_cycle
        mmax = match_search_max_lines
        our_anchor = find_address_in_trace(
            our_trace,
            match_cycles_at,
            mmax,
            mmin,
            start_byte=our_skip_bytes,
            resume_flag="--our-skip-bytes",
        )
        if our_anchor is None:
            print_msg(f"❌ PC=${match_cycles_at:04X} not found in our trace", "red")
            return

        vice_anchor = find_address_in_trace(
            vice_trace,
            match_cycles_at,
            mmax,
            mmin,
            start_byte=vice_skip_bytes,
            resume_flag="--vice-skip-bytes",
        )
        if vice_anchor is None:
            print_msg(f"❌ PC=${match_cycles_at:04X} not found in VICE trace", "red")
            return

        our_sync_byte = our_anchor.byte_offset
        vice_sync_byte = vice_anchor.byte_offset
        our_sync_cycles = our_anchor.trace_line.cycles
        vice_sync_cycles = vice_anchor.trace_line.cycles
        cycle_offset = vice_sync_cycles - our_sync_cycles
        print_msg(f"\n✅ Synchronized:", "green")
        print_msg(
            f"   Ours: byte {our_sync_byte:,}, physical line {our_anchor.physical_lines_read:,}",
            "dim",
        )
        print_msg(
            f"   VICE: byte {vice_sync_byte:,}, physical line {vice_anchor.physical_lines_read:,}",
            "dim",
        )
        print_msg(f"   VICE cycles: {vice_sync_cycles:,}, Ours: {our_sync_cycles:,}", "dim")
        print_msg(f"   Offset: {cycle_offset:+,} cycles", "dim")
        print_msg(
            f"   📎 Resume: --our-skip-bytes {our_sync_byte} --vice-skip-bytes {vice_sync_byte}",
            "cyan",
        )
        print()
    else:
        our_sync_byte = max(0, our_skip_bytes)
        vice_sync_byte = max(0, vice_skip_bytes)
        if our_sync_byte or vice_sync_byte:
            print_msg(
                f"📎 Starting compare from bytes (no PC sync): "
                f"--our-skip-bytes {our_sync_byte} --vice-skip-bytes {vice_sync_byte}",
                "cyan",
            )
            print()

    # Running cycle delta (vice.cycles - our.cycles); None until first matched pair.
    # If --match-cycles-at was used we seed it with the known offset.
    cycle_delta: Optional[int] = cycle_offset if match_cycles_at is not None else None
    
    # Context buffer for showing lines BEFORE each divergence
    context_before: deque = deque(maxlen=max(1, context_lines // 2))

    segment_matches = 0   # matches in the current segment (since last re-sync)
    total_matches = 0     # cumulative matches across all segments
    divergence_count = 0
    drift_event_count = 0
    irq_skipped_count = 0
    squashed_divergence_count = 0
    lines_compared = 0
    our_exhausted = False
    vice_exhausted = False
    done = False
    last_divergence_resynced = True  # True until proven otherwise

    print_msg("🔄 Comparing traces...", "yellow")

    irq_ranges_eff: Optional[List[Tuple[int, int]]] = None
    if skip_interrupt_divergences:
        irq_ranges_eff = (
            list(irq_skip_pc_ranges)
            if irq_skip_pc_ranges
            else list(DEFAULT_IRQ_SKIP_PC_RANGES)
        )

    # Divergence squashing stats (used when IRQ timing jitter causes frequent short-lived mismatches)
    repeated_divergence_counts: dict[tuple, int] = {}
    repeated_divergence_squashed: dict[tuple, int] = {}

    def apply_resync(
        rp: Tuple[TraceLine, TraceLine],
        our_tail: List[TraceLine],
        vice_tail: List[TraceLine],
    ) -> None:
        nonlocal our_iter, vice_iter, cycle_delta, context_before, segment_matches, last_divergence_resynced
        our_r, vice_r = rp
        cycle_delta = vice_r.cycles - our_r.cycles
        if our_tail or vice_tail:
            our_iter = itertools.chain(((0, tl) for tl in our_tail), our_iter)
            vice_iter = itertools.chain(((0, tl) for tl in vice_tail), vice_iter)
        context_before = deque(maxlen=max(1, context_lines // 2))
        context_before.append((our_r, vice_r))
        segment_matches = 1
        last_divergence_resynced = True

    with open(our_trace, "rb") as f_our, open(vice_trace, "rb") as f_vice:
        our_iter: "Iterator[Tuple[int, TraceLine]]" = iter_trace_compare(f_our, our_sync_byte)
        vice_iter: "Iterator[Tuple[int, TraceLine]]" = iter_trace_compare(f_vice, vice_sync_byte)

        while not done:
            # Check max lines limit
            if max_lines and lines_compared >= max_lines:
                print_msg(f"\n⏹️  Limit of {max_lines:,} lines reached", "yellow")
                done = True
                break

            # Advance both iterators (check both before breaking)
            our_done_now = False
            vice_done_now = False
            try:
                _, our_line = next(our_iter)
            except StopIteration:
                our_done_now = True

            try:
                _, vice_line = next(vice_iter)
            except StopIteration:
                vice_done_now = True

            if our_done_now or vice_done_now:
                our_exhausted = our_done_now
                vice_exhausted = vice_done_now
                done = True
                break

            lines_compared += 1

            if lines_compared % 100000 == 0:
                print_msg(
                    f"   ...{lines_compared:,} lines compared, "
                    f"{total_matches + segment_matches:,} matched so far",
                    "dim",
                )

            our_cmp = our_line.without_cycles()
            vice_cmp = vice_line.without_cycles()

            if our_cmp != vice_cmp:
                # ── Divergence found ──────────────────────────────────────
                before_list = list(context_before)

                ours_gap, vice_gap, resync_pair, our_tail, vice_tail, resync_cost = find_resync(
                    our_iter, vice_iter, resync_lookahead
                )

                sig = (our_line.pc, our_line.opcode, vice_line.pc, vice_line.opcode)
                repeated_divergence_counts[sig] = repeated_divergence_counts.get(sig, 0) + 1

                irq_skippable = (
                    irq_ranges_eff is not None
                    and resync_pair is not None
                    and pc_xor_irq_skip_ranges(our_line.pc, vice_line.pc, irq_ranges_eff)
                )
                if irq_skippable:
                    total_matches += segment_matches
                    irq_skipped_count += 1
                    apply_resync(resync_pair, our_tail, vice_tail)
                    continue

                # Short-lived divergence squashing: if a re-sync exists and it is "cheap" (few lines),
                # treat it as phase noise (often IRQ-related) and avoid printing the full block.
                short_lived = (resync_pair is not None) and (resync_cost < min_resync_cost_to_report)

                if resync_pair is not None and (short_lived or squash_repeated_divergences):
                    if squash_repeated_divergences:
                        already_seen = repeated_divergence_counts[sig] > 1
                        if already_seen or short_lived:
                            repeated_divergence_squashed[sig] = repeated_divergence_squashed.get(sig, 0) + 1
                            squashed_divergence_count += 1
                            if repeated_divergence_counts[sig] == 2:
                                print_msg(
                                    f"… squashing repeated divergence at OURS ${our_line.pc:04X} / VICE ${vice_line.pc:04X} (opcode {our_line.opcode:02X}/{vice_line.opcode:02X})",
                                    "dim",
                                )
                            total_matches += segment_matches
                            apply_resync(resync_pair, our_tail, vice_tail)
                            continue
                    if short_lived:
                        squashed_divergence_count += 1
                        total_matches += segment_matches
                        apply_resync(resync_pair, our_tail, vice_tail)
                        continue

                divergence_count += 1
                total_matches += segment_matches

                # Report match count for this segment
                print()
                seg_label = "lines match" if divergence_count == 1 else f"lines match (since re-sync #{divergence_count - 1})"
                print_msg(f"✅ {segment_matches:,} {seg_label}", "green bold")
                print()
                print_msg(f"❌ Divergence #{divergence_count} at line {lines_compared:,}:", "red bold")
                print()

                resync_annotation = ""
                if resync_pair:
                    our_r, vice_r = resync_pair
                    resync_delta = vice_r.cycles - our_r.cycles
                    expected_delta = cycle_delta if cycle_delta is not None else 0
                    drift_at_resync = resync_delta - expected_delta
                    if drift_at_resync != 0:
                        resync_annotation = f"({drift_at_resync:+d} cy drift)"

                if diffmode == "sidebyside":
                    _show_context_sidebyside(
                        before_list,
                        (our_line, vice_line),
                        ours_gap,
                        vice_gap,
                        resync_pair,
                        resync_annotation,
                        cycle_delta=cycle_delta,
                    )
                    gap_len = sum(1 for x in ours_gap if x is not None) + sum(1 for x in vice_gap if x is not None)
                    resync_note = f" → re-synced {resync_annotation}".rstrip() if resync_pair else " (no re-sync found)"
                    print_msg(f"  ({len(before_list)} before, {gap_len} gap lines{resync_note})", "dim")
                else:
                    # simple mode: stacked display
                    print_msg("  Context:", "yellow")
                    print_msg("  " + "=" * 90, "dim")
                    for our, vice in before_list:
                        print_msg(f"      {our.raw.strip()}", "dim")
                    print_msg(f"  >>> OURS:  {our_line.raw.strip()}", "red")
                    print_msg(f"  >>> VICE:  {vice_line.raw.strip()}", "green")
                    for our_g, vice_g in zip(ours_gap, vice_gap):
                        if our_g:
                            print_msg(f"  ... OURS:  {our_g.raw.strip()}", "red dim")
                        if vice_g:
                            print_msg(f"  ... VICE:  {vice_g.raw.strip()}", "green dim")
                    if resync_pair:
                        our_r, vice_r = resync_pair
                        ann = f"  [{resync_annotation}]" if resync_annotation else ""
                        print_msg(f"  === RE-SYNC{ann} ===", "yellow")
                        print_msg(f"      OURS:  {our_r.raw.strip()}", "dim")
                        print_msg(f"      VICE:  {vice_r.raw.strip()}", "dim")
                    print_msg("  " + "=" * 90, "dim")

                if stop_after_first_divergence:
                    print_msg(
                        "⏹️  Stopping after first divergence (--stop-after-first-divergence).",
                        "yellow bold",
                    )
                    sys.exit(1)

                if resync_pair:
                    apply_resync(resync_pair, our_tail, vice_tail)
                else:
                    last_divergence_resynced = False
                    done = True
            else:
                # Lines match (same PC / registers / flags)
                context_before.append((our_line, vice_line))
                segment_matches += 1

                # Track running cycle delta; report when it changes
                current_delta = vice_line.cycles - our_line.cycles
                if cycle_delta is None:
                    cycle_delta = current_delta
                elif current_delta != cycle_delta:
                    drift_event_count += 1
                    if not skip_drift_report:
                        # Gather before-context from buffer (drift line is the last entry)
                        buf_list = list(context_before)
                        before_ctx = buf_list[:-1][-drift_context:] if drift_context > 0 else []
                        # Gather after-context by reading ahead, then restoring
                        after_ctx: List[Tuple[TraceLine, TraceLine]] = []
                        after_our: List[TraceLine] = []
                        after_vice: List[TraceLine] = []
                        for _ in range(drift_context):
                            try:
                                _, ao = next(our_iter)
                                _, av = next(vice_iter)
                                after_ctx.append((ao, av))
                                after_our.append(ao)
                                after_vice.append(av)
                            except StopIteration:
                                break
                        # Put the after lines back so the main loop sees them
                        if after_our or after_vice:
                            our_iter = itertools.chain(((0, tl) for tl in after_our), our_iter)
                            vice_iter = itertools.chain(((0, tl) for tl in after_vice), vice_iter)
                        _show_cycle_drift_inline(
                            before_ctx, our_line, vice_line, after_ctx,
                            old_delta=cycle_delta,
                            new_delta=current_delta,
                            line_num=lines_compared,
                        )
                    cycle_delta = current_delta
                    # Reset context so subsequent before-lines use the new delta
                    context_before = deque(maxlen=max(1, context_lines // 2))
                    context_before.append((our_line, vice_line))

    # ── Final report ─────────────────────────────────────────────────────────
    total_matches += segment_matches
    print()

    if divergence_count == 0:
        # No divergence at all — check for file length mismatch
        if our_exhausted != vice_exhausted:
            which = "ours" if our_exhausted else "VICE"
            print_msg(f"✅ {total_matches:,} lines match", "green bold")
            print()
            print_msg(f"⚠️  Trace {which} ended first", "yellow")
        else:
            print_msg(f"✅ {total_matches:,} lines match", "green bold")
            print()
            if drift_event_count == 0:
                print_msg("🎉 Traces are identical!", "green bold")
            else:
                if quiet_drift_summary:
                    print_msg("✅ Instruction stream identical!", "yellow bold")
                else:
                    print_msg(
                        f"✅ Instruction stream identical — {drift_event_count} cycle drift event(s) detected",
                        "yellow bold",
                    )
        if irq_skipped_count:
            print_msg(
                f"   ({irq_skipped_count} IRQ-skipped re-sync(s); timing mismatch in KERNAL/IRQ vs VICE)",
                "dim",
            )
        return

    # At least one divergence
    if last_divergence_resynced:
        # Ended cleanly after a re-sync (files exhausted or limit)
        seg_label = f"lines match (since re-sync #{divergence_count})"
        print_msg(f"✅ {segment_matches:,} {seg_label}", "green bold")
        if our_exhausted != vice_exhausted:
            which = "ours" if our_exhausted else "VICE"
            print()
            print_msg(f"⚠️  Trace {which} ended first", "yellow")
    else:
        print_msg(
            f"⚠️  Traces diverged completely at divergence #{divergence_count} "
            f"(no re-sync found within {resync_lookahead} lines)",
            "yellow bold",
        )

    print()
    drift_suffix = ""
    if drift_event_count and not quiet_drift_summary:
        drift_suffix = f" | {drift_event_count} cycle drift event(s)"
    irq_suffix = ""
    if irq_skipped_count:
        irq_suffix = f" | {irq_skipped_count} IRQ-skipped re-sync(s)"
    squash_suffix = ""
    if squashed_divergence_count:
        squash_suffix = f" | {squashed_divergence_count} squashed short-lived/repeated divergence(s)"
    print_msg(
        f"📊 Summary: {divergence_count} divergence{'s' if divergence_count != 1 else ''} found | "
        f"{total_matches:,} total lines matched{drift_suffix}{irq_suffix}{squash_suffix}",
        "cyan bold",
    )


def main():
    parser = argparse.ArgumentParser(
        description='Compare C64 emulator traces with VICE',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Cycle drift vs real bugs:
  When instruction text matches but the running offset (VICE cycles minus ours) changes,
  the tool reports "cycle drift" — that means phase slip vs VICE, not necessarily a wrong
  opcode at that line. Loader/IRQ issues may need traces narrowed around IRQ vectors or
  $D019, or future logs of raster line/cycle at IRQ.

IRQ skip (--skip-interrupt-divergences):
  When a mismatch re-syncs within --resync-lookahead and exactly one PC is in the
  KERNAL range (default $EA00–$FFFF) — typical "game code here vs IRQ there" —
  the tool can treat it as interrupt-phase noise and not count it as a divergence.
  If both PCs are in range, we still report (KERNAL vs KERNAL may be a real bug).
  Use --irq-skip-pc-range for custom IRQ handlers in RAM (repeatable).

Examples:
  %(prog)s game.prg --vice-trace vice.txt
  %(prog)s game.prg --vice-trace vice.txt --match-cycles-at 0100
  %(prog)s game.prg --vice-trace vice.txt --ignore-cycles
  %(prog)s --our-trace ours.txt --vice-trace vice.txt
  %(prog)s --our-trace ours.txt --vice-trace vice.txt --max-lines 1000000
  %(prog)s --our-trace ours.txt --vice-trace vice.txt --skip-drift-report --quiet-drift-summary

  %(prog)s game.prg --vice-trace vice.txt \\
      --emulator-args '--enable-resid --graphics'

Large VICE logs (slow sync scan): first run prints
  Resume: --our-skip-bytes N --vice-skip-bytes M
  Re-run with those flags so the tool seeks near the sync point instead of reading from offset 0.
        """
    )
    
    parser.add_argument('program', nargs='?', help='PRG file to run (optional if --our-trace provided)')
    parser.add_argument('--vice-trace', required=True, help='VICE trace file to compare against')
    parser.add_argument('--our-trace', help='Use existing trace instead of running emulator')
    parser.add_argument('--max-cycles', type=int, default=3000000, help='Max cycles when running emulator (default: 3M)')
    parser.add_argument('--max-lines', type=int, help='Max lines to compare (for large traces)')
    parser.add_argument('--ignore-cycles', action='store_true', help='Ignore cycle counts in comparison')
    parser.add_argument('--match-cycles-at', type=str, metavar='ADDR',
                        help='Sync at first trace line with this PC (hex, e.g., C200)')
    parser.add_argument(
        '--match-min-cycle',
        type=int,
        default=None,
        metavar='N',
        help=(
            'With --match-cycles-at: require cumulative trace cycle >= N on that line '
            '(skip earlier hits of the same PC, e.g. KERNAL noise before game $C200)'
        ),
    )
    parser.add_argument(
        '--match-search-max-lines',
        type=int,
        default=100_000_000,
        metavar='N',
        help='Max physical trace lines to scan when locating --match-cycles-at (default: 100M)',
    )
    parser.add_argument(
        '--our-skip-bytes',
        type=int,
        default=0,
        metavar='N',
        help=(
            'Seek this many bytes into our trace before sync search / compare '
            '(use byte offset printed after a successful sync; aligns to next line if not .C:)'
        ),
    )
    parser.add_argument(
        '--vice-skip-bytes',
        type=int,
        default=0,
        metavar='N',
        help=(
            'Seek into VICE trace before sync search / compare (same as --our-skip-bytes)'
        ),
    )
    parser.add_argument('--context', type=int, default=10, help='Context lines to show (default: 10)')
    parser.add_argument('--diffmode', choices=['sidebyside', 'simple'], default='sidebyside',
                        help='Diff display mode: sidebyside (default) or simple (stacked ours/vice)')
    parser.add_argument('--resync-lookahead', type=int, default=50, metavar='N',
                        help='Lines to scan when searching for re-sync after divergence (default: 50)')
    parser.add_argument('--drift-context', type=int, default=3, metavar='N',
                        help='Context lines to show before/after a cycle drift event (default: 3)')
    parser.add_argument('--skip-drift-report', action='store_true',
                        help='Count cycle drift but do not print drift blocks or read ahead (faster)')
    parser.add_argument('--quiet-drift-summary', action='store_true',
                        help='Omit drift counts from final summary (focus on instruction divergences)')
    parser.add_argument('--stop-after-first-divergence', action='store_true',
                        help='Exit with status 1 after printing the first instruction divergence')
    parser.add_argument(
        '--min-resync-cost-to-report',
        type=int,
        default=0,
        metavar='N',
        help='If a mismatch re-syncs within N total lookahead steps (i+j), squash it (default: 0 = report all)',
    )
    parser.add_argument(
        '--squash-repeated-divergences',
        action='store_true',
        help='If the same (pc/opcode) divergence repeats and re-syncs, suppress repeated full blocks',
    )
    parser.add_argument(
        '--find-first-stable-divergence',
        action='store_true',
        help='Triage mode: skip drift + IRQ-phase noise and stop at first non-squashed divergence',
    )
    parser.add_argument(
        '--skip-interrupt-divergences',
        action='store_true',
        help=(
            'If a mismatch re-syncs within lookahead and exactly one PC is in KERNAL '
            '($EA00–FFFF default), do not report it (game vs IRQ phase heuristic)'
        ),
    )
    parser.add_argument(
        '--irq-skip-pc-range',
        action='append',
        dest='irq_skip_pc_ranges',
        metavar='START:END',
        type=_parse_irq_range_arg,
        help='With --skip-interrupt-divergences: extra inclusive hex PC range (repeatable); '
        'defaults to EA00:FFFF if omitted',
    )
    parser.add_argument('--nocolor', action='store_true',
                        help='Disable ANSI colours (by default colour is forced for use with less -R)')
    parser.add_argument(
        '--emulator-args',
        dest='emulator_args_shell',
        default=None,
        metavar='STRING',
        help=(
            'Extra arguments for C64.py as one shell-quoted string (parsed with shlex). '
            'Avoids parent argparse consuming flags that start with dashes. '
            'Example: --emulator-args \'--enable-resid --graphics\''
        ),
    )

    args = parser.parse_args()

    extra_emulator_args: List[str] = []
    if args.emulator_args_shell is not None:
        shell = args.emulator_args_shell.strip()
        if shell:
            try:
                extra_emulator_args = shlex.split(shell, posix=True)
            except ValueError as e:
                parser.error(f"--emulator-args: invalid shell quoting ({e})")

    # Initialise the global console now that we know --nocolor
    global console
    console = make_console(nocolor=args.nocolor)

    if not args.our_trace and not args.program:
        parser.error("Either program or --our-trace must be provided")
    
    if not os.path.exists(args.vice_trace):
        print_msg(f"❌ VICE trace not found: {args.vice_trace}", "red")
        sys.exit(1)

    if args.our_skip_bytes < 0 or args.vice_skip_bytes < 0:
        parser.error("--our-skip-bytes and --vice-skip-bytes must be >= 0")

    match_cycles_at = None
    if args.match_cycles_at:
        try:
            match_cycles_at = int(args.match_cycles_at, 16)
        except ValueError:
            print_msg(f"❌ Invalid hex address: {args.match_cycles_at}", "red")
            sys.exit(1)
    
    if args.our_trace:
        our_trace = args.our_trace
        if not os.path.exists(our_trace):
            print_msg(f"❌ Our trace not found: {our_trace}", "red")
            sys.exit(1)
    else:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            our_trace = f.name
        
        print_msg(f"🚀 Running emulator on {args.program}...", "cyan")
        print_msg(f"   Max cycles: {args.max_cycles:,}", "dim")
        if extra_emulator_args:
            print_msg(f"   Extra C64.py args: {' '.join(extra_emulator_args)}", "dim")
        print()
        
        if not run_emulator(
            args.program,
            args.max_cycles,
            our_trace,
            sync_pc=match_cycles_at,
            extra_emulator_args=extra_emulator_args or None,
        ):
            print_msg("❌ Failed to generate trace", "red")
            sys.exit(1)
        
        print_msg(f"✅ Trace generated: {our_trace}", "green")
        print()
    
    compare_traces_streaming(
        our_trace=our_trace,
        vice_trace=args.vice_trace,
        ignore_cycles=args.ignore_cycles,
        match_cycles_at=match_cycles_at,
        match_min_cycle=args.match_min_cycle,
        match_search_max_lines=args.match_search_max_lines,
        our_skip_bytes=args.our_skip_bytes,
        vice_skip_bytes=args.vice_skip_bytes,
        context_lines=args.context,
        max_lines=args.max_lines,
        diffmode=args.diffmode,
        resync_lookahead=args.resync_lookahead,
        drift_context=args.drift_context,
        skip_drift_report=(args.skip_drift_report or args.find_first_stable_divergence),
        quiet_drift_summary=args.quiet_drift_summary,
        stop_after_first_divergence=(args.stop_after_first_divergence or args.find_first_stable_divergence),
        skip_interrupt_divergences=(args.skip_interrupt_divergences or args.find_first_stable_divergence),
        irq_skip_pc_ranges=args.irq_skip_pc_ranges,
        min_resync_cost_to_report=(
            args.min_resync_cost_to_report
            if (args.min_resync_cost_to_report != 0 or not args.find_first_stable_divergence)
            else 16
        ),
        squash_repeated_divergences=args.squash_repeated_divergences,
        find_first_stable_divergence=args.find_first_stable_divergence,
    )
    
    if not args.our_trace and os.path.exists(our_trace):
        os.unlink(our_trace)


if __name__ == '__main__':
    main()
