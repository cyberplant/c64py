#!/usr/bin/env python3
"""
Run the graphics + ReSID + turbo swinth benchmark with CPU-thread cProfile enabled,
or compare two captured runs (different git commits on the same machine).

The emulator thread is profiled via C64PY_PROFILE_CPU_THREAD (see graphics.py);
`python -m cProfile C64.py --graphics ...` mostly profiles the pygame thread instead.

Examples:
  cd /path/to/c64py
  python3 scripts/profile_swinth_graphics_compare.py capture --label HEAD
  git checkout <older-full-speed-commit>
  python3 scripts/profile_swinth_graphics_compare.py capture --label before
  git checkout -
  python3 scripts/profile_swinth_graphics_compare.py compare \\
      logs/swinth-profile-HEAD logs/swinth-profile-before

Compare defaults to merging stats by (basename, function name) so line-number moves between
commits do not look like “0 vs huge” false positives. Use --per-line only for same-revision diffs.

Older git commits do not include C64PY_PROFILE_CPU_THREAD unless you add them. To profile
the CPU thread on an old revision, use a throwaway branch from that commit and cherry-pick
the instrumentation commit (or merge only the profiling changes into graphics.py), run
capture, then discard the branch. For MHz/throughput alone you can run the same C64.py
command on the old commit without env vars — no .prof, but the speed lines still compare.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_C64_ARGS = [
    "C64.py",
    "--graphics",
    "--enable-resid",
    "--max-cycles",
    "8000000",
    "--autoquit",
    "programs/swinth.prg",
    "--turbo",
]

SPEED_RE = re.compile(r"Speed:\s+([0-9.]+)\s+MHz")
RUNLOG_CPU_WALL_RE = re.compile(r"Time:\s+([0-9.]+)s\s+\(wall since CPU thread start")


def _git_head() -> Dict[str, Any]:
    def run_git(args: List[str]) -> str:
        r = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return (r.stdout or "").strip()

    return {
        "describe": run_git(["describe", "--always", "--dirty"]),
        "rev_parse_HEAD": run_git(["rev-parse", "HEAD"]),
        "status_porcelain": run_git(["status", "--porcelain"]),
    }


def _parse_run_log(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for line in text.splitlines():
        if line.startswith("C64PY_BENCHMARK "):
            try:
                out["c64py_benchmark"] = json.loads(line[len("C64PY_BENCHMARK ") :].strip())
            except json.JSONDecodeError:
                out["c64py_benchmark_raw"] = line
        m = SPEED_RE.search(line)
        if m:
            out["mhz_from_banner"] = float(m.group(1))
        m = RUNLOG_CPU_WALL_RE.search(line)
        if m:
            out["wall_seconds_cpu_thread"] = float(m.group(1))
    return out


def _write_pstats(prof_path: Path, txt_path: Path, top_n: int) -> None:
    import pstats

    with open(txt_path, "w", encoding="utf-8") as f:
        s = pstats.Stats(str(prof_path), stream=f)
        s.strip_dirs()
        s.sort_stats("cumulative")
        s.print_stats(top_n)


def _load_tottime_cumtime(prof_path: Path) -> Dict[Tuple[str, int, str], Tuple[float, float]]:
    """Map stats key -> (tottime, cumtime) per cProfile/pstats tuple layout."""
    import pstats

    s = pstats.Stats(str(prof_path))
    s.strip_dirs()
    return {k: (v[2], v[3]) for k, v in s.stats.items()}


def _wall_seconds_from_capture(prof_path: Path) -> Optional[float]:
    parent = prof_path.parent
    meta_path = parent / "meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            pl = meta.get("parsed_log") or {}
            b = pl.get("c64py_benchmark")
            if isinstance(b, dict) and "wall_seconds" in b:
                return float(b["wall_seconds"])
            if "wall_seconds_cpu_thread" in pl:
                return float(pl["wall_seconds_cpu_thread"])
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    run_log = parent / "run.log"
    if run_log.is_file():
        try:
            parsed = _parse_run_log(run_log.read_text(encoding="utf-8"))
            if "wall_seconds_cpu_thread" in parsed:
                return float(parsed["wall_seconds_cpu_thread"])
        except (OSError, ValueError, TypeError):
            pass
    return None


def _aggregate_tottime_by_name(
    raw: Dict[Tuple[str, int, str], Tuple[float, float]]
) -> Dict[Tuple[str, str], float]:
    """Sum exclusive time per (basename, function) so A/B compares across line shifts."""
    out: Dict[Tuple[str, str], float] = {}
    for (fn, _line, name), (tt, _ct) in raw.items():
        base = os.path.basename(fn)
        key = (base, name)
        out[key] = out.get(key, 0.0) + tt
    return out


def cmd_capture(args: argparse.Namespace) -> int:
    label = args.label.strip() or "run"
    out_dir = Path(args.out_dir) if args.out_dir else ROOT / "logs" / f"swinth-profile-{label}"
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    prof_path = out_dir / "cpu_thread.prof"
    c64_argv = [sys.executable, *DEFAULT_C64_ARGS]
    if args.rom_dir:
        c64_argv.extend(["--rom-dir", args.rom_dir])
    c64_argv.extend(args.extra)

    meta: Dict[str, Any] = {
        "label": label,
        "out_dir": str(out_dir),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": platform.platform(),
        "git": _git_head(),
        "argv": c64_argv,
        "env_profile": {
            "C64PY_PROFILE_CPU_THREAD": "1",
            "C64PY_PROFILE_CPU_OUT": str(prof_path),
        },
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    env = os.environ.copy()
    env["C64PY_PROFILE_CPU_THREAD"] = "1"
    env["C64PY_PROFILE_CPU_OUT"] = str(prof_path)

    run_log = out_dir / "run.log"
    proc = subprocess.run(
        c64_argv,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    run_log.write_text(combined, encoding="utf-8")

    parsed = _parse_run_log(combined)
    meta["exit_code"] = proc.returncode
    meta["parsed_log"] = parsed
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if prof_path.is_file():
        _write_pstats(prof_path, out_dir / "cpu_thread.pstats.txt", args.top_stats)
    else:
        print(f"warning: expected profile missing: {prof_path}", file=sys.stderr)

    print(f"Wrote {out_dir}  exit_code={proc.returncode}")
    if "mhz_from_banner" in parsed:
        print(f"  MHz (banner): {parsed['mhz_from_banner']}")
    if "c64py_benchmark" in parsed:
        b = parsed["c64py_benchmark"]
        if isinstance(b, dict) and "emulated_cpu_mhz" in b:
            print(f"  MHz (C64PY_BENCHMARK): {b['emulated_cpu_mhz']}")
    return proc.returncode


def cmd_compare(args: argparse.Namespace) -> int:
    a = Path(args.dir_a).resolve()
    b = Path(args.dir_b).resolve()
    prof_a = a / "cpu_thread.prof" if a.is_dir() else a
    prof_b = b / "cpu_thread.prof" if b.is_dir() else b
    if not prof_a.is_file() or not prof_b.is_file():
        print(f"need cpu_thread.prof in each path (or pass .prof files): {prof_a!s} {prof_b!s}", file=sys.stderr)
        return 2

    ta = _load_tottime_cumtime(prof_a)
    tb = _load_tottime_cumtime(prof_b)

    wall_a = _wall_seconds_from_capture(prof_a)
    wall_b = _wall_seconds_from_capture(prof_b)
    use_wall = wall_a is not None and wall_b is not None
    if not use_wall:
        print(
            "warning: could not read wall time from meta/run.log; "
            "comparing raw exclusive (tottime) seconds (less meaningful if wall times differ).",
            file=sys.stderr,
        )

    rows: List[Tuple[float, str, float, float, float]] = []
    if args.per_line:
        keys = set(ta) | set(tb)
        for k in keys:
            tta, _ = ta.get(k, (0.0, 0.0))
            ttb, _ = tb.get(k, (0.0, 0.0))
            if use_wall:
                na = tta / wall_a  # type: ignore[operator]
                nb = ttb / wall_b  # type: ignore[operator]
            else:
                na, nb = tta, ttb
            fn, line, name = k
            label = f"{name} ({fn}:{line})"
            rows.append((max(na, nb), label, na, nb, nb - na))
    else:
        aa = _aggregate_tottime_by_name(ta)
        ab = _aggregate_tottime_by_name(tb)
        for k in set(aa) | set(ab):
            tta = aa.get(k, 0.0)
            ttb = ab.get(k, 0.0)
            if use_wall:
                na = tta / wall_a  # type: ignore[operator]
                nb = ttb / wall_b  # type: ignore[operator]
            else:
                na, nb = tta, ttb
            base, name = k
            label = f"{name} ({base})"
            rows.append((max(na, nb), label, na, nb, nb - na))

    rows.sort(key=lambda r: r[0], reverse=True)
    w = args.compare_top

    label_a, label_b = str(a), str(b)
    unit = "excl/wall_s" if use_wall else "tottime_s"
    mode = "per-line keys" if args.per_line else "merged by (file basename, function name)"
    print(f"# compare mode: {mode}")
    print(f"# A: {label_a}" + (f"  wall_s={wall_a:.4f}" if use_wall else ""))
    print(f"# B: {label_b}" + (f"  wall_s={wall_b:.4f}" if use_wall else ""))
    print(f"# positive d => more exclusive time in B than A ({unit}; same max_cycles recommended)")
    print(f"{'A':>14} {'B':>14} {'d':>12}  function")
    for _, name, na, nb, d in rows[:w]:
        print(f"{na:12.6f} {nb:12.6f} {d:10.6f}  {name}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("capture", help="Run swinth benchmark and write profile artifacts under logs/")
    pc.add_argument("--label", default="", help="Label for default output dir logs/swinth-profile-<label>")
    pc.add_argument("--out-dir", default="", help="Output directory (overrides --label default)")
    pc.add_argument("--rom-dir", default="", help="Passed through as --rom-dir to C64.py")
    pc.add_argument("--top-stats", type=int, default=50, help="Lines in cpu_thread.pstats.txt")
    pc.add_argument(
        "extra",
        nargs="*",
        help="Extra arguments appended to C64.py after defaults (e.g. --rom-dir ./roms)",
    )
    pc.set_defaults(func=cmd_capture)

    pq = sub.add_parser(
        "compare",
        help="Compare exclusive (tottime) between two capture dirs or cpu_thread.prof files",
    )
    pq.add_argument("dir_a", help="First capture directory or cpu_thread.prof")
    pq.add_argument("dir_b", help="Second capture directory or cpu_thread.prof")
    pq.add_argument("--compare-top", type=int, default=40, help="How many rows to print")
    pq.add_argument(
        "--per-line",
        action="store_true",
        help="Match cProfile keys including line numbers (misleading across git revisions)",
    )
    pq.set_defaults(func=cmd_compare)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
