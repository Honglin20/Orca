#!/usr/bin/env bash
# reuse_check.sh — ns3_train_script Step 0 reuse gate.
# REUSE iff train_supernet.py + run_train_supernet.sh exist, train_supernet.py parses, and the launcher references it.
set -euo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

if [ -s train_supernet.py ] && [ -s run_train_supernet.sh ]; then
  if python3 -c "import ast; ast.parse(open('train_supernet.py').read())" 2>/dev/null \
     && grep -q "train_supernet" run_train_supernet.sh; then
    echo "REUSE: train_supernet.py + run_train_supernet.sh already exist and pass validation → skip Step 1-3, go straight to emitting the output JSON"
    exit 0
  fi
fi

echo "NO_REUSE: train_supernet.py / run_train_supernet.sh missing or below the bar" >&2
exit 1
