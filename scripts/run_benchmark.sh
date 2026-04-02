#!/usr/bin/env bash
# Run c64py benchmark twice (fast vs accurate VIC) and print JSON lines for comparison.
# Usage:
#   ROM_DIR=/path/to/roms ./scripts/run_benchmark.sh
#   ./scripts/run_benchmark.sh /path/to/roms
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
ROM="${1:-${ROM_DIR:-}}"
ROM_ARGS=()
if [[ -n "$ROM" ]]; then
  ROM_ARGS=(--rom-dir "$ROM")
fi
CYCLES="${BENCHMARK_CYCLES:-20000000}"
PY=python3
if ! command -v "$PY" &>/dev/null; then PY=python; fi

echo "# c64py benchmark: fast VIC, ${CYCLES} cycles" >&2
$PY C64.py --benchmark --max-cycles "$CYCLES" "${ROM_ARGS[@]}" 2>/dev/null | grep '^C64PY_BENCHMARK ' || true

echo "# c64py benchmark: accurate VIC, ${CYCLES} cycles" >&2
$PY C64.py --benchmark --max-cycles "$CYCLES" --accurate-vic "${ROM_ARGS[@]}" 2>/dev/null | grep '^C64PY_BENCHMARK ' || true
