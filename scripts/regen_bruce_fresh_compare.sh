#!/usr/bin/env bash
# Regenerate Bruce Lee debug log and run compare_loader_branches vs vice_full_trace.log.
# Requires ROMs in ./roms (see docs/bruce_lee_loader_investigation.md).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
LOG="${BRUCE_LOG:-/tmp/bruce_fresh.log}"
export C64PY_BRUCELEE_DEBUG=1
export C64PY_BRUCELEE_DEBUG_LOG="$LOG"
echo "Writing $LOG ..."
python3 C64.py programs/BruceLee.prg --headless --turbo \
  --max-cycles 13200000 --autoquit --rom-dir roms
VICE="${VICE_TRACE:-$ROOT/vice_full_trace.log}"
if [[ ! -f "$VICE" ]]; then
  echo "Missing VICE trace: $VICE (set VICE_TRACE=...)" >&2
  exit 2
fi
python3 scripts/compare_loader_branches.py \
  --c64py-log "$LOG" \
  --vice-trace "$VICE" \
  --max-diff 10 --inject-hint
