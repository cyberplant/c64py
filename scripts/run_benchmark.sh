#!/usr/bin/env bash
# Run c64py benchmark combinations; tee each run to logs/; append NDJSON to logs/benchmark-log.json.
#
# Default: all 4 combinations
#   - headless × (fast VIC | accurate VIC)
#   - graphics + ReSID × (fast VIC | accurate VIC)
#
# Usage:
#   ./scripts/run_benchmark.sh [options] [ROM_DIR]
#   ROM_DIR=/path/to/roms BENCHMARK_CYCLES=5000000 ./scripts/run_benchmark.sh
#
# Options:
#   --headless-only        Only headless runs (2 by default, or 1 with --vic-*)
#   --graphics-resid-only  Only --graphics --enable-resid runs
#   --vic-fast-only        Only fast VIC (no --accurate-vic)
#   --vic-accurate-only    Only accurate VIC
#   --cycles N             Override BENCHMARK_CYCLES / default (20_000_000)
#   --cprofile             Run under python -m cProfile; write .prof + .pstats.txt
#   --vice-trace           Pass --vice-trace FILE per run (VICE-format CPU trace in logs/)
#   --vice-trace-wall      With --vice-trace: add --vice-trace-wall (host dt between instructions)
#   -h, --help             This help
#
# cProfile vs VICE trace:
#   If you pass BOTH --cprofile and (--vice-trace or --vice-trace-wall), each stack×VIC combo
#   runs TWICE: (1) trace + wall, no cProfile  (2) cProfile, no trace — so .pstats are not
#   polluted by trace I/O. With only one of them, a single run per combo is used.
#
# Logs:
#   logs/benchmark-<timestamp>_<slug>.log     one tee file per run
#   logs/benchmark-<timestamp>_<slug>.prof   optional cProfile output
#   logs/benchmark-<timestamp>_<slug>.pstats.txt  optional top functions (cumulative)
#   logs/benchmark-<timestamp>_<slug>.vice.log optional CPU trace
#   logs/benchmark-log.json                    one JSON object per line (NDJSON)
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p "$ROOT/logs"

PY=python3
if ! command -v "$PY" &>/dev/null; then PY=python; fi
APPEND_PY="$ROOT/scripts/benchmark_log_append.py"

STACK_HEADLESS=true
STACK_GRAPHICS=true
VIC_FAST=true
VIC_ACCURATE=true
CYCLES="${BENCHMARK_CYCLES:-20000000}"
ROM=""
ENABLE_CPROFILE=false
ENABLE_VICE_TRACE=false
VICE_TRACE_WALL=false

usage() {
  sed -n '1,45p' "$0" | tail -n +2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --headless-only)
      STACK_HEADLESS=true
      STACK_GRAPHICS=false
      shift
      ;;
    --graphics-resid-only)
      STACK_HEADLESS=false
      STACK_GRAPHICS=true
      shift
      ;;
    --vic-fast-only)
      VIC_FAST=true
      VIC_ACCURATE=false
      shift
      ;;
    --vic-accurate-only)
      VIC_FAST=false
      VIC_ACCURATE=true
      shift
      ;;
    --cycles)
      CYCLES="$2"
      shift 2
      ;;
    --cprofile)
      ENABLE_CPROFILE=true
      shift
      ;;
    --vice-trace)
      ENABLE_VICE_TRACE=true
      shift
      ;;
    --vice-trace-wall)
      VICE_TRACE_WALL=true
      shift
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      ROM="$1"
      shift
      ;;
  esac
done

ROM="${ROM:-${ROM_DIR:-}}"
ROM_ARGS=()
if [[ -n "$ROM" ]]; then
  ROM_ARGS=(--rom-dir "$ROM")
fi

# --vice-trace-wall implies a trace file
if $VICE_TRACE_WALL; then
  ENABLE_VICE_TRACE=true
fi

SESSION_TS="$(date +%Y%m%d-%H%M%S)"

# run_one stack vic suffix use_cprof use_vice use_wall_trace
# suffix empty, or walltrace, or cprofile (appended to slug and benchmark_type)
run_one() {
  local stack="$1" vic="$2" suffix="$3"
  local use_cprof="$4" use_vice="$5" use_wall_trace="$6"
  local bench_type slug logf argvf t0 t1 host_wall rc
  local -a CMD

  if [[ "$stack" == "headless" ]]; then
    slug="headless"
    bench_type="headless"
  else
    slug="graphics-resid"
    bench_type="graphics_resid"
  fi

  if [[ "$vic" == "fast" ]]; then
    slug+="_fast-vic"
    bench_type+="_fast_vic"
  else
    slug+="_accurate-vic"
    bench_type+="_accurate_vic"
  fi

  if [[ -n "$suffix" ]]; then
    slug+="_${suffix}"
    bench_type+="_${suffix}"
  fi

  logf="$ROOT/logs/benchmark-${SESSION_TS}_${slug}.log"
  argvf="$(mktemp)"
  # shellcheck disable=SC2064
  trap "rm -f '$argvf'" RETURN

  CMD=("C64.py" "--benchmark" "--max-cycles" "$CYCLES")
  if [[ "$vic" == "fast" ]]; then
    CMD+=(--vic-emulation fast)
  else
    CMD+=(--vic-emulation accurate-python)
  fi
  if [[ "$stack" != "headless" ]]; then
    CMD+=(--graphics --enable-resid)
  fi
  if [[ ${#ROM_ARGS[@]} -gt 0 ]]; then
    CMD+=("${ROM_ARGS[@]}")
  fi

  local prof_path="" pstats_path="" vice_path="" rel_prof="" rel_pstats="" rel_vice=""
  if $use_vice; then
    vice_path="$ROOT/logs/benchmark-${SESSION_TS}_${slug}.vice.log"
    rel_vice="logs/benchmark-${SESSION_TS}_${slug}.vice.log"
    CMD+=(--vice-trace "$vice_path")
    if $use_wall_trace; then
      CMD+=(--vice-trace-wall)
    fi
  fi

  local -a RUN
  RUN=("$PY")
  if $use_cprof; then
    prof_path="$ROOT/logs/benchmark-${SESSION_TS}_${slug}.prof"
    pstats_path="$ROOT/logs/benchmark-${SESSION_TS}_${slug}.pstats.txt"
    rel_prof="logs/benchmark-${SESSION_TS}_${slug}.prof"
    rel_pstats="logs/benchmark-${SESSION_TS}_${slug}.pstats.txt"
    RUN+=(-m cProfile -o "$prof_path")
  fi
  RUN+=("${CMD[@]}")

  printf '%s\n' "${RUN[@]}" >"$argvf"

  echo "============================================================" | tee -a "$logf"
  echo "# benchmark_type=$bench_type  slug=$slug  cycles=$CYCLES" | tee -a "$logf"
  echo "# use_cprof=$use_cprof use_vice=$use_vice use_wall_trace=$use_wall_trace" | tee -a "$logf"
  echo "# $(date -u '+%Y-%m-%dT%H:%M:%SZ') start" | tee -a "$logf"
  echo "============================================================" | tee -a "$logf"

  t0="$("$PY" -c 'import time; print(time.perf_counter())')"
  set +o pipefail
  set +e
  "${RUN[@]}" 2>&1 | tee -a "$logf"
  rc="${PIPESTATUS[0]}"
  set -e
  set -o pipefail
  t1="$("$PY" -c 'import time; print(time.perf_counter())')"
  host_wall="$("$PY" -c "print(round($t1 - $t0, 6))")"

  if $use_cprof && [[ -n "$prof_path" && -f "$prof_path" ]]; then
    BENCHMARK_PROF="$prof_path" BENCHMARK_PSTATS="$pstats_path" "$PY" -c "
import os, pstats
prof, out = os.environ['BENCHMARK_PROF'], os.environ['BENCHMARK_PSTATS']
with open(out, 'w') as f:
    s = pstats.Stats(prof, stream=f)
    s.strip_dirs()
    s.sort_stats('cumulative')
    s.print_stats(40)
" || true
    echo "# cProfile stats: $rel_pstats" | tee -a "$logf"
  fi

  echo "# exit_code=$rc  host_wall_seconds=$host_wall" | tee -a "$logf"

  local rel_log="logs/benchmark-${SESSION_TS}_${slug}.log"
  local -a append_args=(
    --repo-root "$ROOT"
    --benchmark-type "$bench_type"
    --argv-file "$argvf"
    --exit-code "$rc"
    --host-wall-seconds "$host_wall"
    --log-file "$rel_log"
    --tee-log-path "$logf"
  )
  if $use_cprof && [[ -n "$rel_prof" ]]; then
    append_args+=(--cprofile-prof "$rel_prof" --cprofile-pstats "$rel_pstats")
  fi
  if [[ -n "$rel_vice" ]]; then
    append_args+=(--vice-trace-file "$rel_vice")
    $use_wall_trace && append_args+=(--vice-trace-wall)
  fi
  "$PY" "$APPEND_PY" "${append_args[@]}"

  rm -f "$argvf"
  trap - RETURN
}

invoke_combo() {
  local stack="$1" vic="$2"
  if $ENABLE_CPROFILE && { $ENABLE_VICE_TRACE || $VICE_TRACE_WALL; }; then
    echo "# Split diag: trace+wall (no cProfile), then cProfile (no trace)" >&2
    # Phase 1 always records host dt between instructions (--vice-trace-wall).
    run_one "$stack" "$vic" walltrace false true true
    run_one "$stack" "$vic" cprofile true false false
  elif $ENABLE_CPROFILE; then
    run_one "$stack" "$vic" "" true false false
  elif $ENABLE_VICE_TRACE || $VICE_TRACE_WALL; then
    run_one "$stack" "$vic" "" false true "$VICE_TRACE_WALL"
  else
    run_one "$stack" "$vic" "" false false false
  fi
}

echo "# Session $SESSION_TS  ROOT=$ROOT  cycles=$CYCLES" >&2
echo "# stacks: headless=$STACK_HEADLESS graphics=$STACK_GRAPHICS  vic: fast=$VIC_FAST accurate=$VIC_ACCURATE" >&2
echo "# ENABLE_CPROFILE=$ENABLE_CPROFILE ENABLE_VICE_TRACE=$ENABLE_VICE_TRACE VICE_TRACE_WALL=$VICE_TRACE_WALL" >&2

if $STACK_HEADLESS; then
  $VIC_FAST && invoke_combo headless fast
  $VIC_ACCURATE && invoke_combo headless accurate
fi
if $STACK_GRAPHICS; then
  $VIC_FAST && invoke_combo graphics fast
  $VIC_ACCURATE && invoke_combo graphics accurate
fi

echo "# Done. NDJSON appended to $ROOT/logs/benchmark-log.json" >&2
