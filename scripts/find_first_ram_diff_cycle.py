#!/usr/bin/env python3
"""
Find the smallest max_cycles C such that full 64 KiB RAM dumps differ between two
``--vic-emulation`` modes (same PRG, ROMs, and other flags).

Uses an optional coarse linear scan, then binary search. Each probe runs two headless
emulations from reset to ``C`` cycles (expensive: plan ``--coarse-step`` for large ``--max-hi``).

**Disk cache (default: ``.c64py_ram_cache/`` in the repo):** 64 KiB RAM snapshots and
combined stdout/stderr logs are reused when the PRG content hash, ``max_cycles``, vic mode,
and flags match. Slow **accurate-python** re-runs are skipped on cache hits so you can
iterate binary search and debug sessions without waiting hundreds of seconds per probe.
Invalidate with ``rm -rf .c64py_ram_cache`` (or bump cache when changing ``CACHE_FORMAT``).

Example::

    ./scripts/find_first_ram_diff_cycle.py programs/your_game.prg \\
        --max-hi 23500000 --coarse-step 500000 \\
        --vic-a accurate-python --vic-b accurate-rust

Requires a venv with ``c64py_rust_core`` when ``--vic-b`` uses the Rust path (default
``.venv/bin/python`` when present).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

# Bump when cache key inputs or emulator semantics for this tool change.
_CACHE_FORMAT = 3  # 3: KERNAL CHROUT/CHRIN/CINT paths now advance cpu.state.cycles (sync with emulated_cycles)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_python(repo: Path) -> str:
    cand = repo / ".venv" / "bin" / "python"
    if cand.is_file():
        return str(cand)
    return sys.executable


def _prg_digest(prg: Path) -> str:
    h = hashlib.sha256()
    with open(prg, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_cache_key(
    prg_digest: str,
    max_cycles: int,
    vic: str,
    *,
    video_render: str,
    enable_resid: bool,
    extra_args: List[str],
    env: Optional[Dict[str, str]],
) -> str:
    payload = {
        "fmt": _CACHE_FORMAT,
        "prg_sha256": prg_digest,
        "max_cycles": int(max_cycles),
        "vic": vic,
        "video_render": video_render,
        "enable_resid": bool(enable_resid),
        "extra_args": sorted(extra_args),
        "env": sorted((env or {}).items()),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _build_cmd(
    repo: Path,
    python_exe: str,
    prg: Path,
    max_cycles: int,
    vic: str,
    ram_out: Path,
    *,
    video_render: str,
    enable_resid: bool,
    extra_args: List[str],
) -> List[str]:
    cmd: List[str] = [
        python_exe,
        str(repo / "C64.py"),
        str(prg),
        "--headless",
        "--max-cycles",
        str(max_cycles),
        "--no-colors",
        "--turbo",
        "--video-render",
        video_render,
        "--vic-emulation",
        vic,
        "--dump-ram-raw",
        str(ram_out),
    ]
    if enable_resid:
        cmd.insert(-2, "--enable-resid")
    cmd.extend(extra_args)
    return cmd


def _run_snapshot(
    repo: Path,
    python_exe: str,
    prg: Path,
    max_cycles: int,
    vic: str,
    ram_out: Path,
    *,
    prg_digest: str,
    video_render: str,
    enable_resid: bool,
    extra_args: List[str],
    env: Optional[Dict[str, str]],
    timeout: Optional[float],
    verbose: bool,
    cache_dir: Optional[Path],
    use_cache: bool,
) -> None:
    ram_out.parent.mkdir(parents=True, exist_ok=True)
    if ram_out.exists():
        ram_out.unlink()

    key = _snapshot_cache_key(
        prg_digest,
        max_cycles,
        vic,
        video_render=video_render,
        enable_resid=enable_resid,
        extra_args=extra_args,
        env=env,
    )
    c_ram: Optional[Path] = None
    c_log: Optional[Path] = None
    if use_cache and cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        c_ram = cache_dir / f"{key}.ram"
        c_log = cache_dir / f"{key}.log"

    if c_ram is not None and c_ram.is_file() and c_ram.stat().st_size == 65536:
        shutil.copy2(c_ram, ram_out)
        if verbose:
            print(f"  CACHE HIT vic={vic!r} max_cycles={max_cycles} {c_ram.name}", flush=True)
            if c_log is not None and c_log.is_file():
                tail = c_log.read_text(encoding="utf-8", errors="replace")[-4000:]
                if tail.strip():
                    print(f"  --- cached log tail ({c_log.name}) ---\n{tail}", flush=True)
        return

    cmd = _build_cmd(
        repo,
        python_exe,
        prg,
        max_cycles,
        vic,
        ram_out,
        video_render=video_render,
        enable_resid=enable_resid,
        extra_args=extra_args,
    )
    if verbose:
        print(f"  RUN vic={vic!r} max_cycles={max_cycles}\n    {' '.join(cmd)}", flush=True)
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    r = subprocess.run(
        cmd,
        cwd=str(repo),
        env=run_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = r.stdout or ""
    err = r.stderr or ""
    if verbose and (out or err):
        if out:
            print(out, end="", flush=True)
        if err:
            print(err, end="", file=sys.stderr, flush=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"C64.py failed (exit {r.returncode}) vic={vic!r} cycles={max_cycles}\n"
            f"stderr: {err[-4000:]}\nstdout: {out[-2000:]}"
        )
    if not ram_out.is_file() or ram_out.stat().st_size != 65536:
        raise RuntimeError(
            f"Expected 65536-byte {ram_out}; got "
            f"{ram_out.stat().st_size if ram_out.is_file() else 'missing'}"
        )

    if c_ram is not None:
        shutil.copy2(ram_out, c_ram)
        if c_log is not None:
            meta = {
                "cmd": cmd,
                "returncode": r.returncode,
                "vic": vic,
                "max_cycles": max_cycles,
                "cache_key": key,
            }
            c_log.write_text(
                json.dumps(meta, indent=2)
                + "\n\n--- stdout ---\n"
                + out
                + "\n--- stderr ---\n"
                + err,
                encoding="utf-8",
                errors="replace",
            )


def _ram_equal(a: Path, b: Path) -> bool:
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            xa, xb = fa.read(65536), fb.read(65536)
            if xa != xb:
                return False
            if not xa:
                return True


def _compare_modes(
    repo: Path,
    python_exe: str,
    prg: Path,
    prg_digest: str,
    cycles: int,
    vic_a: str,
    vic_b: str,
    work_dir: Path,
    *,
    video_render: str,
    enable_resid: bool,
    extra_args: List[str],
    env_a: Optional[Dict[str, str]],
    env_b: Optional[Dict[str, str]],
    timeout: Optional[float],
    verbose: bool,
    cache_dir: Optional[Path],
    use_cache: bool,
) -> bool:
    """Return True if full RAM is identical at ``cycles`` for both modes."""
    pa = work_dir / f"a_{cycles}.ram"
    pb = work_dir / f"b_{cycles}.ram"
    _run_snapshot(
        repo,
        python_exe,
        prg,
        cycles,
        vic_a,
        pa,
        prg_digest=prg_digest,
        video_render=video_render,
        enable_resid=enable_resid,
        extra_args=extra_args,
        env=env_a,
        timeout=timeout,
        verbose=verbose,
        cache_dir=cache_dir,
        use_cache=use_cache,
    )
    _run_snapshot(
        repo,
        python_exe,
        prg,
        cycles,
        vic_b,
        pb,
        prg_digest=prg_digest,
        video_render=video_render,
        enable_resid=enable_resid,
        extra_args=extra_args,
        env=env_b,
        timeout=timeout,
        verbose=verbose,
        cache_dir=cache_dir,
        use_cache=use_cache,
    )
    return _ram_equal(pa, pb)


def _parse_env_kv(pairs: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for p in pairs:
        if "=" not in p:
            raise ValueError(f"Expected KEY=VALUE, got {p!r}")
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main() -> int:
    repo = _repo_root()
    ap = argparse.ArgumentParser(
        description="Binary-search the first max_cycles where two VIC modes diverge in full RAM."
    )
    ap.add_argument("prg", type=Path, help="PRG path (relative to repo root ok)")
    ap.add_argument("--repo-root", type=Path, default=repo, help="C64.py directory (default: repo root)")
    ap.add_argument("--python", dest="python_exe", default=None, help="Interpreter (default: .venv/bin/python)")
    ap.add_argument("--vic-a", default="accurate-python", help="First --vic-emulation mode")
    ap.add_argument("--vic-b", default="accurate-rust", help="Second --vic-emulation mode")
    ap.add_argument(
        "--env-a",
        action="append",
        default=[],
        metavar="KEY=VAL",
        help="Extra env for mode A (repeatable).",
    )
    ap.add_argument("--env-b", action="append", default=[], metavar="KEY=VAL", help="Extra env for mode B")
    ap.add_argument("--max-hi", type=int, default=23_500_000, help="Upper bound cycle count (inclusive probe)")
    ap.add_argument(
        "--coarse-step",
        type=int,
        default=0,
        help="If >0, scan step,2*step,... then binary-search inside last equal / first diff window",
    )
    ap.add_argument("--video-render", default="accurate", choices=("accurate", "fast"))
    ap.add_argument("--no-resid", action="store_true", help="Omit --enable-resid")
    ap.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="Extra C64.py argument (repeatable), e.g. --extra-arg=--video-standard=ntsc",
    )
    ap.add_argument("--timeout", type=float, default=900.0, help="Subprocess timeout seconds (per snapshot)")
    ap.add_argument("--work-dir", type=Path, default=None, help="Temp RAM files (default: system temp)")
    ap.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Store/load 64 KiB RAM + C64.py logs (default: REPO/.c64py_ram_cache)",
    )
    ap.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable disk cache (always run C64.py for every probe)",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="Print each C64.py invocation / cache hits")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned search only (no emulator runs)",
    )
    args = ap.parse_args()

    repo = args.repo_root.resolve()
    prg = args.prg if args.prg.is_absolute() else (repo / args.prg)
    if not prg.is_file():
        print(f"error: PRG not found: {prg}", file=sys.stderr)
        return 2

    python_exe = args.python_exe or _default_python(repo)
    enable_resid = not args.no_resid
    env_a = _parse_env_kv(args.env_a) if args.env_a else None
    env_b = _parse_env_kv(args.env_b) if args.env_b else None
    hi = max(1, int(args.max_hi))
    coarse = max(0, int(args.coarse_step))
    use_cache = not args.no_cache
    cache_dir: Optional[Path] = None
    if use_cache:
        cache_dir = args.cache_dir if args.cache_dir is not None else (repo / ".c64py_ram_cache")

    if args.dry_run:
        print(
            f"Would search [1, {hi}] vic-a={args.vic_a!r} vic-b={args.vic_b!r} coarse={coarse or 'off'} "
            f"cache={'off' if args.no_cache else cache_dir}"
        )
        return 0

    prg_digest = _prg_digest(prg)
    print(f"PRG digest sha256={prg_digest[:16]}… (full hash in cache keys)", flush=True)
    if use_cache:
        print(f"RAM+log cache: {cache_dir}  (invalidate: rm -rf that directory)", flush=True)

    work_dir = args.work_dir
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="c64py_ram_diff_"))
        cleanup_work = True
    else:
        work_dir.mkdir(parents=True, exist_ok=True)
        cleanup_work = False

    def probe(cycles: int) -> bool:
        """True if RAM equal at this max_cycles."""
        return _compare_modes(
            repo,
            python_exe,
            prg,
            prg_digest,
            cycles,
            args.vic_a,
            args.vic_b,
            work_dir,
            video_render=args.video_render,
            enable_resid=enable_resid,
            extra_args=args.extra_arg,
            env_a=env_a,
            env_b=env_b,
            timeout=args.timeout,
            verbose=args.verbose,
            cache_dir=cache_dir,
            use_cache=use_cache,
        )

    try:
        print(f"Probe upper bound max_cycles={hi} …", flush=True)
        if probe(hi):
            print(
                f"No difference in full 64 KiB RAM up to max_cycles={hi} "
                f"({args.vic_a!r} vs {args.vic_b!r})."
            )
            return 0

        lo, hi_b = 1, hi
        if coarse > 0:
            prev = 0
            c = coarse
            print(f"Coarse scan step={coarse} …", flush=True)
            while c <= hi:
                print(f"  coarse probe max_cycles={c} …", flush=True)
                if not probe(c):
                    lo, hi_b = prev + 1, c
                    print(f"  bracket: ({lo}, {hi_b}] first diff in this range", flush=True)
                    break
                prev = c
                c += coarse
            else:
                if prev >= hi:
                    print("Coarse scan: no difference up to max-hi (unexpected if upper probe differed).")
                    return 1
                lo, hi_b = prev + 1, hi
                print(f"  bracket after coarse: ({lo}, {hi_b}]", flush=True)

        print(f"Binary search on max_cycles in [{lo}, {hi_b}] …", flush=True)
        while lo < hi_b:
            mid = (lo + hi_b) // 2
            print(f"  mid={mid} …", flush=True)
            if probe(mid):
                lo = mid + 1
            else:
                hi_b = mid

        first = lo
        print(
            f"\nFirst max_cycles where 64 KiB RAM differs: {first}\n"
            f"  ({args.vic_a!r} vs {args.vic_b!r}, same PRG and flags as above)\n"
            f"  Re-run with --max-cycles {first} and --dump-cpu-state on each side to compare PC/regs."
        )
        return 0
    except (KeyboardInterrupt, subprocess.TimeoutExpired) as e:
        print(f"Aborted: {e}", file=sys.stderr)
        return 130 if isinstance(e, KeyboardInterrupt) else 124
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        if cleanup_work:
            try:
                for p in work_dir.glob("*.ram"):
                    p.unlink(missing_ok=True)
                work_dir.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
