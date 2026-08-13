#!/usr/bin/env bash
# resume_check.sh — ns3_expand_supernet Step 2 resume gate.
# RESUME iff supernet.py exists AND passes the bar (skip re-generation).
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

if [ -s supernet.py ] && bash "$SCRIPTS_DIR/supernet_bar.sh" supernet.py; then
  echo "RESUME: supernet.py already exists and passes the bar → skip Step 2 generation, go straight to evaluator loop"
  exit 0
fi

echo "NO_RESUME: supernet.py missing or below the bar" >&2
exit 1
