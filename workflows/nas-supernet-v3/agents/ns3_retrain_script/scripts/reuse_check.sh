#!/usr/bin/env bash
# reuse_check.sh — ns3_retrain_script Step 0 reuse gate.
# REUSE iff retrain.py + run_retrain.sh exist, retrain.py parses, and the launcher references it.
set -euo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

if [ -s retrain.py ] && [ -s run_retrain.sh ]; then
  if python3 -c "import ast; ast.parse(open('retrain.py').read())" 2>/dev/null \
     && grep -q "retrain" run_retrain.sh; then
    echo "REUSE: retrain.py + run_retrain.sh already exist and pass validation → skip generation, go straight to emitting the output JSON"
    exit 0
  fi
fi

echo "NO_REUSE: retrain.py / run_retrain.sh missing or below the bar" >&2
exit 1
