#!/usr/bin/env python3
"""Append one benchmark run to logs/benchmark-log.json as a single NDJSON line.

Each line is a JSON object (schema 2) with git metadata, argv, exit code,
host wall time, path to the tee log, and the parsed C64PY_BENCHMARK payload.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone


def _git_info(repo_root: str) -> tuple[str | None, bool, str | None]:
    try:
        commit = subprocess.check_output(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None, False, None
    try:
        dirty = bool(
            subprocess.check_output(
                ["git", "-C", repo_root, "status", "--porcelain"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        dirty = False
    try:
        desc = subprocess.check_output(
            ["git", "-C", repo_root, "describe", "--always", "--dirty"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        desc = None
    return commit, dirty, desc


def _c64_json_from_tee_log(path: str) -> dict:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("C64PY_BENCHMARK "):
                    raw = line[len("C64PY_BENCHMARK ") :].strip()
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        return {"_parse_error": raw[:500]}
    except OSError as e:
        return {"_log_read_error": str(e)}
    return {}


def _argv_from_file(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [line.rstrip("\n") for line in f]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--benchmark-type", required=True)
    ap.add_argument("--argv-file", required=True, help="One command-line token per line")
    ap.add_argument("--exit-code", type=int, required=True)
    ap.add_argument("--host-wall-seconds", type=float, required=True)
    ap.add_argument("--log-file", required=True, help="Path to tee log, relative to repo root")
    ap.add_argument("--tee-log-path", required=True, help="Absolute path to tee log (to grep metrics)")
    ap.add_argument("--cprofile-prof", default=None, help="Relative path to .prof (optional)")
    ap.add_argument("--cprofile-pstats", default=None, help="Relative path to pstats text (optional)")
    ap.add_argument("--vice-trace-file", default=None, help="Relative path to VICE-format trace (optional)")
    ap.add_argument(
        "--vice-trace-wall",
        action="store_true",
        help="Trace includes host wall deltas between instructions",
    )
    ap.add_argument("--print-only", action="store_true", help="Write JSON line to stdout; do not append")
    args = ap.parse_args()

    commit, dirty, desc = _git_info(args.repo_root)
    c64py_data = _c64_json_from_tee_log(args.tee_log_path)
    argv = _argv_from_file(args.argv_file)

    record: dict = {
        "schema": 2,
        "record_version": 1,
        "timestamp_iso": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "git_commit": commit,
        "git_dirty": dirty,
        "git_describe": desc,
        "benchmark_type": args.benchmark_type,
        "argv": argv,
        "exit_code": args.exit_code,
        "host_wall_seconds": round(args.host_wall_seconds, 6),
        "log_file": args.log_file,
        "c64py_benchmark": c64py_data,
    }

    if isinstance(c64py_data, dict):
        for key in (
            "cycles",
            "emulated_cpu_mhz",
            "accurate_vic",
            "enable_resid",
            "enable_sid",
            "turbo",
            "target_hz",
            "video_standard",
            "prg",
            "max_cycles_arg",
        ):
            if key in c64py_data:
                record[key] = c64py_data[key]
        if "wall_seconds" in c64py_data:
            record["wall_seconds_emulator"] = c64py_data["wall_seconds"]

    if args.cprofile_prof:
        record["cprofile_prof"] = args.cprofile_prof
        record["cprofile_pstats"] = args.cprofile_pstats
    if args.vice_trace_file:
        record["vice_trace_file"] = args.vice_trace_file
        record["vice_trace_wall"] = bool(args.vice_trace_wall)

    line = json.dumps(record, sort_keys=True) + "\n"
    if args.print_only:
        sys.stdout.write(line)
        return

    logs_dir = os.path.join(args.repo_root, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    out_path = os.path.join(logs_dir, "benchmark-log.json")
    with open(out_path, "a", encoding="utf-8") as out:
        out.write(line)


if __name__ == "__main__":
    main()
