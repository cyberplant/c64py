#!/usr/bin/env bash
# Inspect VICE + optional c64py traces around a target cumulative cycle (default 22M).
#
# For **VICE vs c64py** when disk timing differs, prefer game-entry sync:
#   GAME_PC=c200 ./scripts/trace_game_entry.sh
#
# Usage:
#   ./scripts/trace_near_cycle.sh [cycle]
#   C64PY_TRACE=/tmp/c64py.trace ./scripts/trace_near_cycle.sh 22000000
#
# Env:
#   VICE_TRACE   (default: logs/vice_full_trace.log)
#   C64PY_TRACE  if set, run the same window + loop stats on the c64py trace

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/.venv/bin/python"
CYCLE="${1:-22000000}"
VICE_TRACE="${VICE_TRACE:-${ROOT}/logs/vice_full_trace.log}"

if [[ ! -f "$VICE_TRACE" ]]; then
  echo "missing $VICE_TRACE" >&2
  exit 1
fi

echo "======== VICE ($VICE_TRACE) @ cycle >= $CYCLE ========"
"$PY" "${ROOT}/scripts/trace_window_at_cycle.py" "$VICE_TRACE" \
  --at-cycle "$CYCLE" \
  --vice-monitor-dedupe \
  --before 25 --after 50 \
  --analyze 8000 \
  --print-sync-hint

if [[ -n "${C64PY_TRACE:-}" ]]; then
  if [[ ! -f "$C64PY_TRACE" ]]; then
    echo "C64PY_TRACE set but missing file: $C64PY_TRACE" >&2
    exit 1
  fi
  echo ""
  echo "======== c64py ($C64PY_TRACE) @ cycle >= $CYCLE ========"
  "$PY" "${ROOT}/scripts/trace_window_at_cycle.py" "$C64PY_TRACE" \
    --at-cycle "$CYCLE" \
    --before 25 --after 50 \
    --analyze 8000 \
    --print-sync-hint
fi

echo ""
echo "Generate c64py trace (example, ~minutes at 24M cycles):"
echo "  .venv/bin/python C64.py programs/your_game.prg --headless --autoquit --turbo --no-colors \\"
echo "    --enable-resid --video-render accurate --vic-emulation accurate-python \\"
echo "    --max-cycles 24000000 --vice-trace /tmp/c64py.trace"
echo "  C64PY_TRACE=/tmp/c64py.trace $0 $CYCLE"
