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
import os
import re
import subprocess
import sys
import tempfile
from collections import deque
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple, TextIO

try:
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
    from rich.panel import Panel
    from rich.table import Table
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

console = Console() if RICH_AVAILABLE else None


def print_msg(msg: str, style: str = None):
    """Print message with optional Rich styling"""
    if console and style:
        console.print(msg, style=style)
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


def parse_trace_line(line: str, line_num: int = 0) -> Optional[TraceLine]:
    """Parse a VICE-format trace line"""
    line = line.strip()
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


def get_file_size_mb(filename: str) -> float:
    """Get file size in MB"""
    return os.path.getsize(filename) / (1024 * 1024)


def run_emulator(prg_file: str, max_cycles: int, trace_file: str) -> bool:
    """Run our emulator and generate trace"""
    cmd = [
        sys.executable, 'C64.py',
        prg_file,
        '--vice-trace', trace_file,
        '--max-cycles', str(max_cycles),
        '--autoquit',
        '--turbo'
    ]
    
    try:
        result = subprocess.run(
            cmd,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=300
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print_msg("❌ Emulator timed out", "red")
        return False
    except Exception as e:
        print_msg(f"❌ Failed to run emulator: {e}", "red")
        return False


def find_address_in_trace(filename: str, target_pc: int, max_search: int = 10000000) -> Optional[int]:
    """Find line number where PC first matches target_pc"""
    print_msg(f"   🔍 Searching PC=${target_pc:04X} in {os.path.basename(filename)}...", "dim")
    with open(filename, 'r') as f:
        for line_num, tl in iterate_trace_file(f):
            if line_num % 500000 == 0:
                print_msg(f"      ...scanned {line_num:,} lines", "dim")
            if tl.pc == target_pc:
                print_msg(f"      ✅ Found at line {line_num:,}", "green")
                return line_num
            if line_num >= max_search:
                print_msg(f"      ❌ Not found in first {max_search:,} lines", "red")
                return None
    print_msg(f"      ❌ Not found in file", "red")
    return None


def compare_traces_streaming(
    our_trace: str,
    vice_trace: str,
    ignore_cycles: bool = False,
    match_cycles_at: Optional[int] = None,
    context_lines: int = 10,
    max_lines: Optional[int] = None
) -> None:
    """Compare two trace files using streaming (memory efficient)"""
    
    our_size = get_file_size_mb(our_trace)
    vice_size = get_file_size_mb(vice_trace)
    
    print_msg(f"📂 Ours: {our_trace} ({our_size:.1f} MB)", "cyan")
    print_msg(f"📂 VICE: {vice_trace} ({vice_size:.1f} MB)", "cyan")
    print()
    
    # If match_cycles_at specified, find start positions first
    our_start_line = 0
    vice_start_line = 0
    cycle_offset = 0
    
    if match_cycles_at is not None:
        print_msg(f"🔄 Searching sync point PC=${match_cycles_at:04X}...", "yellow")
        
        our_start_line = find_address_in_trace(our_trace, match_cycles_at)
        if our_start_line is None:
            print_msg(f"❌ PC=${match_cycles_at:04X} not found in our trace", "red")
            return
        
        vice_start_line = find_address_in_trace(vice_trace, match_cycles_at)
        if vice_start_line is None:
            print_msg(f"❌ PC=${match_cycles_at:04X} not found in VICE trace", "red")
            return
        
        # Get cycle values at sync point
        with open(our_trace, 'r') as f:
            for _, tl in iterate_trace_file(f):
                if tl.line_num == our_start_line:
                    our_sync_cycles = tl.cycles
                    break
        
        with open(vice_trace, 'r') as f:
            for _, tl in iterate_trace_file(f):
                if tl.line_num == vice_start_line:
                    vice_sync_cycles = tl.cycles
                    break
        
        cycle_offset = vice_sync_cycles - our_sync_cycles
        print_msg(f"\n✅ Synchronized:", "green")
        print_msg(f"   Ours line {our_start_line:,}, VICE line {vice_start_line:,}", "dim")
        print_msg(f"   VICE cycles: {vice_sync_cycles:,}, Ours: {our_sync_cycles:,}", "dim")
        print_msg(f"   Offset: {cycle_offset:+,} cycles", "dim")
        print()
    
    # Context buffer for showing lines BEFORE divergence
    context_before: deque = deque(maxlen=context_lines // 2)
    
    matches = 0
    first_divergence = None
    first_cycle_drift = None
    lines_compared = 0
    divergence_line_pair = None  # Store the divergent line pair
    
    print_msg("🔄 Comparing traces...", "yellow")
    
    with open(our_trace, 'r') as f_our, open(vice_trace, 'r') as f_vice:
        our_iter = iterate_trace_file(f_our, start_line=our_start_line - 1 if our_start_line > 0 else 0)
        vice_iter = iterate_trace_file(f_vice, start_line=vice_start_line - 1 if vice_start_line > 0 else 0)
        
        our_exhausted = False
        vice_exhausted = False
        
        while True:
            # Check max lines limit
            if max_lines and lines_compared >= max_lines:
                print_msg(f"\n⏹️  Limit of {max_lines:,} lines reached", "yellow")
                break
            
            # Get next lines
            try:
                our_num, our_line = next(our_iter)
            except StopIteration:
                our_exhausted = True
                our_line = None
            
            try:
                vice_num, vice_line = next(vice_iter)
            except StopIteration:
                vice_exhausted = True
                vice_line = None
            
            # Check if either file exhausted
            if our_exhausted or vice_exhausted:
                break
            
            lines_compared += 1
            
            # Progress every 100k lines
            if lines_compared % 100000 == 0:
                print_msg(f"   ...compared {lines_compared:,} lines, {matches:,} match", "dim")
            
            # Compare lines
            our_cmp = our_line.without_cycles()
            vice_cmp = vice_line.without_cycles()
            
            if our_cmp != vice_cmp:
                first_divergence = lines_compared
                divergence_line_pair = (our_line, vice_line)
                break
            
            # Store in context buffer (only matching lines, for "before" context)
            context_before.append((our_line, vice_line))
            
            # Check cycle drift
            if match_cycles_at is not None and first_cycle_drift is None:
                expected = vice_line.cycles
                actual = our_line.cycles + cycle_offset
                if expected != actual:
                    first_cycle_drift = (lines_compared, expected, actual, expected - actual,
                                        our_line, vice_line)
            
            matches += 1
        
        # If divergence found, read lines AFTER for context
        context_after: List[Tuple[TraceLine, TraceLine]] = []
        if first_divergence is not None and not (our_exhausted or vice_exhausted):
            after_count = context_lines // 2
            for _ in range(after_count):
                try:
                    _, our_after = next(our_iter)
                    _, vice_after = next(vice_iter)
                    context_after.append((our_after, vice_after))
                except StopIteration:
                    break
    
    # Report results
    print()
    if match_cycles_at is not None:
        print_msg(f"✅ {matches:,} lines match (post-sync)", "green bold")
    else:
        print_msg(f"✅ {matches:,} lines match", "green bold")
    
    if first_divergence is not None:
        print()
        print_msg(f"❌ First divergence at line {first_divergence:,}:", "red bold")
        print()
        
        # Show context: before + divergence + after
        print_msg("  Context:", "yellow")
        print_msg("  " + "=" * 90, "dim")
        
        # Lines BEFORE divergence
        before_list = list(context_before)
        for our, vice in before_list:
            print_msg(f"      {our.raw.strip()}", "dim")
        
        # The divergent line
        if divergence_line_pair:
            our_div, vice_div = divergence_line_pair
            print_msg(f"  >>> OURS:  {our_div.raw.strip()}", "red")
            print_msg(f"  >>> VICE:  {vice_div.raw.strip()}", "green")
        
        # Lines AFTER divergence
        for our, vice in context_after:
            print_msg(f"  ... OURS:  {our.raw.strip()}", "red dim")
            print_msg(f"  ... VICE:  {vice.raw.strip()}", "green dim")
        
        print_msg("  " + "=" * 90, "dim")
        print_msg(f"  ({len(before_list)} lines before, {len(context_after)} lines after)", "dim")
    
    elif first_cycle_drift is not None:
        idx, expected, actual, drift, our_line, vice_line = first_cycle_drift
        print()
        print_msg(f"⚠️  Cycle drift detected at line {idx:,}:", "yellow bold")
        print_msg(f"    Expected: {expected:,}, Actual: {actual:,}", "dim")
        print_msg(f"    Drift: {drift:+,} cycles", "yellow")
        print()
        print_msg(f"    OURS: {our_line.raw.strip()}", "dim")
        print_msg(f"    VICE: {vice_line.raw.strip()}", "dim")
    
    elif our_exhausted != vice_exhausted:
        print()
        which = "ours" if our_exhausted else "VICE"
        print_msg(f"⚠️  Trace {which} ended first", "yellow")
    
    else:
        print()
        print_msg("🎉 Traces are identical!", "green bold")


def main():
    parser = argparse.ArgumentParser(
        description='Compare C64 emulator traces with VICE',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s game.prg --vice-trace vice.txt
  %(prog)s game.prg --vice-trace vice.txt --match-cycles-at 0100
  %(prog)s game.prg --vice-trace vice.txt --ignore-cycles
  %(prog)s --our-trace ours.txt --vice-trace vice.txt
  %(prog)s --our-trace ours.txt --vice-trace vice.txt --max-lines 1000000
        """
    )
    
    parser.add_argument('program', nargs='?', help='PRG file to run (optional if --our-trace provided)')
    parser.add_argument('--vice-trace', required=True, help='VICE trace file to compare against')
    parser.add_argument('--our-trace', help='Use existing trace instead of running emulator')
    parser.add_argument('--max-cycles', type=int, default=3000000, help='Max cycles when running emulator (default: 3M)')
    parser.add_argument('--max-lines', type=int, help='Max lines to compare (for large traces)')
    parser.add_argument('--ignore-cycles', action='store_true', help='Ignore cycle counts in comparison')
    parser.add_argument('--match-cycles-at', type=str, metavar='ADDR',
                        help='Sync cycle counts at this PC address (hex, e.g., 0100)')
    parser.add_argument('--context', type=int, default=10, help='Context lines to show (default: 10)')
    
    args = parser.parse_args()
    
    if not args.our_trace and not args.program:
        parser.error("Either program or --our-trace must be provided")
    
    if not os.path.exists(args.vice_trace):
        print_msg(f"❌ VICE trace not found: {args.vice_trace}", "red")
        sys.exit(1)
    
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
        print()
        
        if not run_emulator(args.program, args.max_cycles, our_trace):
            print_msg("❌ Failed to generate trace", "red")
            sys.exit(1)
        
        print_msg(f"✅ Trace generated: {our_trace}", "green")
        print()
    
    compare_traces_streaming(
        our_trace=our_trace,
        vice_trace=args.vice_trace,
        ignore_cycles=args.ignore_cycles,
        match_cycles_at=match_cycles_at,
        context_lines=args.context,
        max_lines=args.max_lines
    )
    
    if not args.our_trace and os.path.exists(our_trace):
        os.unlink(our_trace)


if __name__ == '__main__':
    main()
