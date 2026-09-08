#!/usr/bin/env bash
# reuse_check.sh — ns3_expand_supernet Step 0 reuse gate.
# REUSE iff both supernet.py and supernet_summary.md exist AND supernet.py passes the bar.
# Always clears the stale unsupported marker first (cross-run/attempt residue).
# On REUSE, also pushes the SearchSpace table chart (fail-soft sidecar, never blocks).
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

rm -f .ns_expand_unsupported.flag

MISSING=""
for f in supernet.py supernet_summary.md; do
  [ -s "$f" ] || MISSING="$MISSING $f"
done

if [ -n "$MISSING" ]; then
  echo "NO_REUSE: missing:$MISSING" >&2
  exit 1
fi

if bash "$SCRIPTS_DIR/supernet_bar.sh" supernet.py; then
  echo "REUSE: supernet.py + summary already exist and pass the bar → skip Step 1-4, go straight to output JSON"
  python3 "$SCRIPTS_DIR/search_space_table.py" --artifacts-dir "$ARTIFACTS_DIR" > /dev/null || true
  exit 0
fi

echo "NO_REUSE: supernet.py below the bar" >&2
exit 1
