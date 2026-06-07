#!/bin/bash
# Compare instruction timing between accurate-python and accurate-rust traces
# Usage: ./scripts/compare_traces_timing.sh path/to/game.prg [max_cycles]

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PRG="${1:-}"
MAX_CYCLES="${2:-2080000}"
PY_TRACE=/tmp/py_$$_${MAX_CYCLES}.trace
RS_TRACE=/tmp/rs_$$_${MAX_CYCLES}.trace

if [[ -z "$PRG" ]]; then
  echo "Usage: $0 path/to/game.prg [max_cycles]" >&2
  exit 2
fi
if [[ ! -f "$PRG" ]]; then
  echo "missing PRG: $PRG" >&2
  exit 1
fi

echo "=== Generating Python trace up to $MAX_CYCLES cycles ==="
.venv/bin/python "$REPO/C64.py" "$PRG" \
  --vic-emulation accurate-python --max-cycles "$MAX_CYCLES" \
  --enable-resid --turbo --headless --no-colors \
  --vice-trace "$PY_TRACE" 2>&1 | tail -5

echo ""
echo "=== Generating Rust trace up to $MAX_CYCLES cycles ==="
C64PY_RUST_VICE_TRACE="$RS_TRACE" .venv/bin/python "$REPO/C64.py" "$PRG" \
  --vic-emulation accurate-rust --max-cycles "$MAX_CYCLES" \
  --enable-resid --turbo --headless --no-colors 2>&1 | tail -5

echo ""
echo "=== Python last 30 lines (EA/E4/E5 range) ==="
grep -E '\.C:e[a45]' "$PY_TRACE" | tail -30

echo ""
echo "=== Rust last 30 lines (EA/E4/E5 range) ==="
grep -E '\.C:e[a45]' "$RS_TRACE" | tail -30

echo ""
echo "=== Find cycle gap between emulators ==="
PY_LAST_CYC=$(grep -E '\.C:ea07' "$PY_TRACE" | tail -1 | awk '{print $NF}')
RS_LAST_CYC=$(grep -E '\.C:ea07' "$RS_TRACE" | tail -1 | awk '{print $NF}')
echo "Python last JSR at ea07: cycle $PY_LAST_CYC"
echo "Rust last JSR at ea07:   cycle $RS_LAST_CYC"
echo "Rust is ahead by: $(echo "$PY_LAST_CYC - $RS_LAST_CYC" | bc) cycles"

echo ""
echo "=== Timing of STA indirect Y instructions ==="
echo "--- Python STA (ZP),Y timings (last 10) ---"
grep 'STA' "$PY_TRACE" | grep -E '\$[DF][13]' | tail -10

echo ""
echo "--- Rust STA (ZP),Y timings (last 10) ---"
grep 'STA' "$RS_TRACE" | grep -E '\$[DF][13]' | tail -10
