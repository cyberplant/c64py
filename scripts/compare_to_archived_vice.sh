#!/usr/bin/env bash
# Compare c64py (accurate-python + --vice-trace) to a local VICE CPU log
# (e.g. logs/vice_full_trace.log; large; not committed).
#
# Default sync is **semantic**: first occurrence of MATCH_PC on each trace
# (i.e. game entry after the loader's JMP), not the same cumulative cycle
# — VICE includes real disk load; c64py with KERNAL hooks reaches that PC
# many millions of cycles earlier.
#
# With --vice-trace, accurate-python and accurate-rust both use Python step() — one trace
# is enough. Use --vice-monitor-dedupe for x64sc monitor logs.
#
# Usage:
#   PRG=programs/your_game.prg ./scripts/compare_to_archived_vice.sh
#   PRG=programs/your_game.prg MATCH_PC=c200 ./scripts/compare_to_archived_vice.sh \
#       --max-cycles 24000000 --first-only
#
# Env:
#   VICE_TRACE           path to VICE log (default: logs/vice_full_trace.log)
#   PRG                  PRG to run in c64py (required)
#   MATCH_PC             hex PC for semantic sync (default: c200)
#   OUR_MIN_CYCLE        optional --our-match-min-cycle (c64py trace)
#   VICE_MIN_CYCLE       optional --vice-match-min-cycle (VICE trace)

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/.venv/bin/python"
VICE_TRACE="${VICE_TRACE:-${ROOT}/logs/vice_full_trace.log}"
PRG="${PRG:-}"
MATCH_PC="${MATCH_PC:-c200}"
MAX_CYCLES="${MAX_CYCLES:-24000000}"
FIRST_ONLY=""
EXTRA_COMPARE=(--vice-monitor-dedupe --match-cycles-at "$MATCH_PC")

[[ -n "${OUR_MIN_CYCLE:-}" ]] && EXTRA_COMPARE+=(--our-match-min-cycle "$OUR_MIN_CYCLE")
[[ -n "${VICE_MIN_CYCLE:-}" ]] && EXTRA_COMPARE+=(--vice-match-min-cycle "$VICE_MIN_CYCLE")

while [[ $# -gt 0 ]]; do
  case "$1" in
    --max-cycles) MAX_CYCLES="$2"; shift 2 ;;
    --first-only) FIRST_ONLY=1; shift ;;
    --match-cycles-at) EXTRA_COMPARE=(--vice-monitor-dedupe --match-cycles-at "$2")
      [[ -n "${OUR_MIN_CYCLE:-}" ]] && EXTRA_COMPARE+=(--our-match-min-cycle "$OUR_MIN_CYCLE")
      [[ -n "${VICE_MIN_CYCLE:-}" ]] && EXTRA_COMPARE+=(--vice-match-min-cycle "$VICE_MIN_CYCLE")
      shift 2 ;;
    --match-min-cycle) EXTRA_COMPARE+=(--match-min-cycle "$2"); shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$PRG" ]]; then
  echo "PRG=path/to/your_game.prg env var is required" >&2
  exit 2
fi

if [[ ! -x "$PY" ]]; then
  echo "missing $PY" >&2
  exit 1
fi
if [[ ! -f "$PRG" ]]; then
  echo "missing PRG: $PRG" >&2
  exit 1
fi
if [[ ! -f "$VICE_TRACE" ]]; then
  echo "missing VICE trace: $VICE_TRACE" >&2
  echo "Add logs/vice_full_trace.log or set VICE_TRACE." >&2
  exit 1
fi

OURS="${TMPDIR:-/tmp}/c64py_compare_ours.$$"
cleanup() { rm -f "$OURS"; }
trap cleanup EXIT

echo "== c64py trace → $OURS  (max_cycles=$MAX_CYCLES, accurate-python, sync PC=\$$MATCH_PC vs VICE)"
"$PY" C64.py "$PRG" \
  --headless --autoquit --turbo --no-colors --enable-resid --video-render accurate \
  --vic-emulation accurate-python \
  --max-cycles "$MAX_CYCLES" \
  --vice-trace "$OURS"

COMPARE_FLAGS=(
  --our-trace "$OURS"
  --vice-trace "$VICE_TRACE"
  --nocolor
  "${EXTRA_COMPARE[@]}"
)
if [[ -n "$FIRST_ONLY" ]]; then
  COMPARE_FLAGS+=(--stop-after-first-divergence)
fi

echo "== tools/compare_traces.py ${COMPARE_FLAGS[*]}"
set +e
"$PY" tools/compare_traces.py "${COMPARE_FLAGS[@]}"
code=$?
set -e

if [[ "$code" -ne 0 && -z "$FIRST_ONLY" ]]; then
  echo "(exit $code — try --first-only, or OUR_MIN_CYCLE / VICE_MIN_CYCLE if the wrong \$${MATCH_PC} was chosen)" >&2
fi
exit "$code"
