#!/usr/bin/env bash
# Compare CPU traces: accurate-python (Python step + --vice-trace) vs accurate-rust
# (real Rust batch path with C64PY_RUST_VICE_TRACE, one instruction per batch).
#
# Note: Running BOTH with --vice-trace only exercises Python step() in both modes, so
# those traces match even when Rust batch diverges — use this script instead.
#
# Usage:
#   ./scripts/compare_accurate_vic_python_vs_rust_trace.sh path/to/game.prg [max_cycles]
#   ./scripts/compare_accurate_vic_python_vs_rust_trace.sh - [max_cycles]   # KERNAL-only
#
# Example:
#   ./scripts/compare_accurate_vic_python_vs_rust_trace.sh programs/your_game.prg 2050000
#
# Optional (exported env vars for tools/compare_traces.py):
#   MATCH_PC          hex PC for --match-cycles-at (e.g. C200) after the PRG is running
#   MATCH_MIN_CYCLE   --match-min-cycle N to skip earlier hits of the same PC (e.g. 2000000)
#
# Rust path logs one instruction per batch (slow at multi‑million cycles).
#
# Parallel capture (default): set C64PY_TRACE_COMPARE_SERIAL=1 to run one after the other.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Args: <game.prg|-> [max_cycles]  — use "-" for KERNAL-only (no PRG).
if [[ -z "${1:-}" ]]; then
  echo "usage: $0 <game.prg|-> [max_cycles]" >&2
  exit 1
fi
if [[ "$1" == "-" ]]; then
  PRG_ARGS=()
  MAX="${2:-2050000}"
else
  PRG_ARGS=("$1")
  MAX="${2:-2050000}"
fi
PY="${TMPDIR:-/tmp}/c64py_vic_trace_py.$$"
RS="${TMPDIR:-/tmp}/c64py_vic_trace_rust.$$"
PY_EXE="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY_EXE" ]]; then
  echo "missing $PY_EXE (create .venv and pip install -e .)" >&2
  exit 1
fi

cleanup() {
  rm -f "$PY" "$RS"
}
trap cleanup EXIT

_run_py_trace() {
  echo "== accurate-python + --vice-trace (reference) → $PY"
  local cmd=( "$PY_EXE" C64.py )
  if ((${#PRG_ARGS[@]} > 0)); then cmd+=("${PRG_ARGS[@]}"); fi
  cmd+=(
    --enable-resid --turbo --video-render accurate
    --headless --max-cycles "$MAX" --no-colors
    --vic-emulation accurate-python
    --vice-trace "$PY"
  )
  "${cmd[@]}"
}

_run_rs_trace() {
  echo "== accurate-rust + C64PY_RUST_VICE_TRACE (Rust path) → $RS"
  local cmd=( "$PY_EXE" C64.py )
  if ((${#PRG_ARGS[@]} > 0)); then cmd+=("${PRG_ARGS[@]}"); fi
  cmd+=(
    --enable-resid --turbo --video-render accurate
    --headless --max-cycles "$MAX" --no-colors
    --vic-emulation accurate-rust
  )
  C64PY_RUST_VICE_TRACE="$RS" "${cmd[@]}"
}

if [[ -n "${C64PY_TRACE_COMPARE_SERIAL:-}" ]]; then
  _run_py_trace
  _run_rs_trace
else
  echo "== parallel trace capture (wall-clock)"
  _run_py_trace >"${TMPDIR:-/tmp}/c64py_vic_trace_py.$$.log" 2>&1 &
  p1=$!
  _run_rs_trace >"${TMPDIR:-/tmp}/c64py_vic_trace_rust.$$.log" 2>&1 &
  p2=$!
  if ! wait $p1; then
    echo "Python trace run failed (log: ${TMPDIR:-/tmp}/c64py_vic_trace_py.$$.log)" >&2
    wait $p2 2>/dev/null || true
    exit 1
  fi
  if ! wait $p2; then
    echo "Rust trace run failed (log: ${TMPDIR:-/tmp}/c64py_vic_trace_rust.$$.log)" >&2
    exit 1
  fi
  rm -f "${TMPDIR:-/tmp}/c64py_vic_trace_py.$$.log" "${TMPDIR:-/tmp}/c64py_vic_trace_rust.$$.log"
fi

COMPARE_EXTRA=()
if [[ -n "${MATCH_PC:-}" ]]; then
  COMPARE_EXTRA+=(--match-cycles-at "$MATCH_PC")
fi
if [[ -n "${MATCH_MIN_CYCLE:-}" ]]; then
  COMPARE_EXTRA+=(--match-min-cycle "$MATCH_MIN_CYCLE")
fi

echo "== compare (first instruction divergence)"
if ((${#COMPARE_EXTRA[@]} > 0)); then
  "$PY_EXE" tools/compare_traces.py --our-trace "$PY" --vice-trace "$RS" \
    --stop-after-first-divergence --nocolor "${COMPARE_EXTRA[@]}"
else
  "$PY_EXE" tools/compare_traces.py --our-trace "$PY" --vice-trace "$RS" \
    --stop-after-first-divergence --nocolor
fi
