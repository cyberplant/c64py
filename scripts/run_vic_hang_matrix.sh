#!/usr/bin/env bash
# Reproduce / compare VIC + Rust paths for title-screen hangs (see docs/DEBUGGING.md §0).
# Each run writes a raw 64 KiB RAM file plus prints full-RAM sha256 and CPU registers in one pass
# (so you do not need separate long runs for fingerprint vs narrow hex range).
# Requires the repo .venv with c64py_rust_core (maturin develop).
#
# Usage: ./scripts/run_vic_hang_matrix.sh [PRG] [MAX_CYCLES] [OUT_DIR]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/.venv/bin/python"
PRG="${1:-${ROOT}/programs/your_game.prg}"
CYCLES="${2:-23500000}"
OUTDIR="${3:-${ROOT}/vic_matrix_snapshots}"

if [[ ! -x "$PY" ]]; then
  echo "error: expected ${PY} — create .venv and install the project + Rust core there." >&2
  exit 1
fi

if ! "$PY" -c "import c64py_rust_core as r; print('c64py_rust_core', r.rust_core_version())" 2>/dev/null; then
  echo "error: c64py_rust_core not importable with ${PY} (run maturin develop in this venv)." >&2
  exit 1
fi

mkdir -p "$OUTDIR"
echo "Snapshots under: $OUTDIR"
echo

# --enable-resid: lockstep SID in accurate modes (matches typical title-screen repros).
# --turbo: skip host speed limiting (helps wall time on Rust batch; Python path still CPU-bound).
_common=( "$PY" C64.py --enable-resid --video-render accurate "$PRG"
  --headless --max-cycles "$CYCLES" --autoquit --no-colors --turbo )

# Extra snapshot flags (one line each on stdout; raw RAM for cmp / vbindiff).
_snap() {
  local slug="$1"
  echo --dump-ram-sha256 --dump-cpu-state --dump-ram-raw "${OUTDIR}/${slug}.ram"
}

run_one() {
  local title="$1"
  local slug="${2:-${title//[^a-zA-Z0-9._-]/_}}"
  shift 2
  echo "========== ${title} (ram=${slug}.ram) =========="
  # shellcheck disable=SC2068
  # PETSCII in "Final Screen output" can make grep treat stdin as binary; -a forces text mode.
  /usr/bin/time -p "$@" $(_snap "$slug") 2>&1 \
    | grep -a -E '^(=== ram-sha256-full|=== cpu-state|Cycles:|Time:|Speed:|Raw 65536)' || true
  echo
}

run_one "accurate-python" "accurate-python" \
  bash -c 'unset C64PY_USE_RUST_FAST C64PY_RUST_HYBRID_VIC C64PY_RUST_BATCH 2>/dev/null || true; exec "$@"' bash "${_common[@]}" --vic-emulation accurate-python

run_one "accurate-rust" "accurate-rust" \
  bash -c 'unset C64PY_USE_RUST_FAST C64PY_RUST_HYBRID_VIC C64PY_RUST_BATCH 2>/dev/null || true; exec "$@"' bash "${_common[@]}" --vic-emulation accurate-rust

run_one "C64PY_RUST_BATCH=1" "C64PY_RUST_BATCH_1" \
  bash -c 'unset C64PY_USE_RUST_FAST C64PY_RUST_HYBRID_VIC 2>/dev/null || true; export C64PY_RUST_BATCH=1; exec "$@"' bash "${_common[@]}" --vic-emulation accurate-rust

run_one "C64PY_USE_RUST_FAST=0" "C64PY_USE_RUST_FAST_0" \
  bash -c 'unset C64PY_RUST_HYBRID_VIC C64PY_RUST_BATCH 2>/dev/null || true; export C64PY_USE_RUST_FAST=0; exec "$@"' bash "${_common[@]}" --vic-emulation accurate-rust

run_one "C64PY_RUST_HYBRID_VIC=0" "C64PY_RUST_HYBRID_VIC_0" \
  bash -c 'unset C64PY_USE_RUST_FAST C64PY_RUST_BATCH 2>/dev/null || true; export C64PY_RUST_HYBRID_VIC=0; exec "$@"' bash "${_common[@]}" --vic-emulation accurate-rust

run_one "vic-fast" "vic-fast" \
  bash -c 'unset C64PY_USE_RUST_FAST C64PY_RUST_HYBRID_VIC C64PY_RUST_BATCH 2>/dev/null || true; exec "$@"' bash "${_common[@]}" --vic-emulation fast

echo "Done. Compare === ram-sha256-full === and === cpu-state === lines above."
echo "=== SHA-256 of written .ram files (authoritative if grep was confused) ==="
shasum -a 256 "$OUTDIR"/*.ram 2>/dev/null | sort || true
echo "Diff two modes: cmp ${OUTDIR}/accurate-python.ram ${OUTDIR}/accurate-rust.ram; echo exit=\$?"
echo "First differing byte: cmp -l ${OUTDIR}/accurate-python.ram ${OUTDIR}/accurate-rust.ram | head"
