#!/usr/bin/env bash
# Inspect traces anchored at the **first hit of a given PC** (game-entry sync),
# not at the same cumulative cycle.
#
# c64py with KERNAL hooks reaches the game many millions of cycles before a
# full VICE trace that includes real 1541 traffic. Syncing on the first
# occurrence of GAME_PC (the loader's JMP target into game code) lines them up.
#
# Usage:
#   GAME_PC=c200 ./scripts/trace_game_entry.sh
#   VICE_TRACE=/path/to/vice.log C64PY_TRACE=/tmp/c64py.trace \
#       GAME_PC=c200 ./scripts/trace_game_entry.sh
#
# Optional: if GAME_PC is hit spuriously before the real game on one side, set
#   OUR_MIN_CYCLE=21000000  or  VICE_MIN_CYCLE=80000000
# and pass to trace_window (edit below) or use compare_traces --our-match-min-cycle / --vice-match-min-cycle.
#
# Env:
#   GAME_PC     hex PC of game entry (default C200; set this to whatever your loader jumps to)
#   VICE_TRACE  default logs/vice_full_trace.log
#   C64PY_TRACE optional second trace for side-by-side loop stats

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/.venv/bin/python"
GAME_PC="${GAME_PC:-C200}"
VICE_TRACE="${VICE_TRACE:-${ROOT}/logs/vice_full_trace.log}"

EXTRA_VICE=()
EXTRA_C64=()
[[ -n "${VICE_MIN_CYCLE:-}" ]] && EXTRA_VICE+=(--min-cycle "$VICE_MIN_CYCLE")
[[ -n "${OUR_MIN_CYCLE:-}" ]] && EXTRA_C64+=(--min-cycle "$OUR_MIN_CYCLE")

if [[ ! -f "$VICE_TRACE" ]]; then
  echo "missing $VICE_TRACE" >&2
  exit 1
fi

echo "======== VICE: first PC=\$$GAME_PC ($VICE_TRACE) ========"
"$PY" "${ROOT}/scripts/trace_window_at_cycle.py" "$VICE_TRACE" \
  --first-at-pc "$GAME_PC" \
  "${EXTRA_VICE[@]}" \
  --vice-monitor-dedupe \
  --before 25 --after 60 \
  --analyze 8000 \
  --print-sync-hint

if [[ -n "${C64PY_TRACE:-}" ]]; then
  if [[ ! -f "$C64PY_TRACE" ]]; then
    echo "C64PY_TRACE set but missing: $C64PY_TRACE" >&2
    exit 1
  fi
  echo ""
  echo "======== c64py: first PC=\$$GAME_PC ($C64PY_TRACE) ========"
  "$PY" "${ROOT}/scripts/trace_window_at_cycle.py" "$C64PY_TRACE" \
    --first-at-pc "$GAME_PC" \
    "${EXTRA_C64[@]}" \
    --before 25 --after 60 \
    --analyze 8000 \
    --print-sync-hint
fi

echo ""
_pc_lc="$(printf '%s' "$GAME_PC" | tr '[:upper:]' '[:lower:]')"
echo "Compare full traces from game entry (adjust *_MIN_CYCLE if needed):"
echo "  .venv/bin/python tools/compare_traces.py --our-trace /tmp/c64py.trace --vice-trace \"$VICE_TRACE\" \\"
echo "    --vice-monitor-dedupe --match-cycles-at $_pc_lc \\"
echo "    # optional: --our-match-min-cycle N --vice-match-min-cycle M"
echo "(omit per-side mins if the first \$$GAME_PC hit is correct on both traces.)"
