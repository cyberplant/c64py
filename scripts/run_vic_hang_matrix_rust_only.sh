#!/usr/bin/env bash
# Rust-side matrix only: skips slow accurate-python runs (~minutes each).
# Compare output to a baseline RAM dump from a prior full matrix or a one-off:
#
#   ./.venv/bin/python C64.py programs/your_game.prg --enable-resid --turbo \
#     --video-render accurate --headless --max-cycles 23500000 --autoquit --no-colors \
#     --dump-ram-raw vic_matrix_snapshots/_baseline_python.ram --vic-emulation accurate-python
#
# Usage: ./scripts/run_vic_hang_matrix_rust_only.sh [PRG] [MAX_CYCLES] [OUT_DIR] [BASELINE_RAM]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/.venv/bin/python"
PRG="${1:-${ROOT}/programs/your_game.prg}"
CYCLES="${2:-23500000}"
OUTDIR="${3:-${ROOT}/vic_matrix_snapshots}"
BASELINE="${4:-${OUTDIR}/_baseline_python.ram}"

if [[ ! -x "$PY" ]]; then
  echo "error: expected ${PY}" >&2
  exit 1
fi
if ! "$PY" -c "import c64py_rust_core" 2>/dev/null; then
  echo "error: c64py_rust_core not importable with ${PY}" >&2
  exit 1
fi
if [[ ! -f "$BASELINE" ]]; then
  echo "error: baseline RAM not found: $BASELINE" >&2
  echo "Create it once with accurate-python + --dump-ram-raw (see script header)." >&2
  exit 1
fi

mkdir -p "$OUTDIR"
echo "Baseline: $BASELINE"
echo "Rust snapshots under: $OUTDIR"
echo

_common=( "$PY" C64.py --enable-resid --turbo --video-render accurate "$PRG"
  --headless --max-cycles "$CYCLES" --autoquit --no-colors )

_snap() {
  echo --dump-ram-sha256 --dump-cpu-state --dump-ram-raw "${OUTDIR}/$1.ram"
}

run_one() {
  local title="$1"
  local slug="$2"
  shift 2
  echo "========== ${title} (ram=${slug}.ram) =========="
  # shellcheck disable=SC2068
  /usr/bin/time -p "$@" $(_snap "$slug") 2>&1 \
    | grep -a -E '^(=== ram-sha256-full|=== cpu-state|Cycles:|Time:|Speed:|Raw 65536)' || true
  local nd
  nd=$(cmp -l "$BASELINE" "${OUTDIR}/${slug}.ram" 2>/dev/null | wc -l | tr -d ' ')
  echo "diff_bytes_vs_baseline=${nd}"
  echo
}

run_one "accurate-rust" "_rust_accurate-rust" \
  bash -c 'unset C64PY_USE_RUST_FAST C64PY_RUST_HYBRID_VIC C64PY_RUST_BATCH 2>/dev/null || true; exec "$@"' bash "${_common[@]}" --vic-emulation accurate-rust

run_one "vic-fast" "_rust_vic-fast" \
  bash -c 'unset C64PY_USE_RUST_FAST C64PY_RUST_HYBRID_VIC C64PY_RUST_BATCH 2>/dev/null || true; exec "$@"' bash "${_common[@]}" --vic-emulation fast

echo "(optional) C64PY_RUST_BATCH=1 is very slow; uncomment in script if needed."
# run_one "C64PY_RUST_BATCH=1" "_rust_batch1" \
#   bash -c 'unset C64PY_USE_RUST_FAST C64PY_RUST_HYBRID_VIC 2>/dev/null || true; export C64PY_RUST_BATCH=1; exec "$@"' bash "${_common[@]}" --vic-emulation accurate-rust

echo "=== SHA-256 (rust runs + baseline) ==="
shopt -s nullglob
_rust_rams=( "$OUTDIR"/_rust_*.ram )
if ((${#_rust_rams[@]})); then
  shasum -a 256 "$BASELINE" "${_rust_rams[@]}" | sort
else
  shasum -a 256 "$BASELINE"
fi
shopt -u nullglob
