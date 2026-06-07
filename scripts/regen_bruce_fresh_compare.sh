#!/usr/bin/env bash
# Previously: regenerate c64py Bruce debug log and run compare_loader_branches.
# The C64PY_BRUCELEE_* / C64PY_LOADER_* hooks were removed from c64py; this script
# is kept only to fail fast with a clear message. Use an older git revision to
# reproduce the old workflow, or compare using VICE traces only.
set -euo pipefail
echo "regen_bruce_fresh_compare.sh: c64py Bruce/loader debug env hooks were removed." >&2
echo "See docs/bruce_lee_loader_investigation.md (historical note at top)." >&2
exit 1
