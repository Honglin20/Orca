#!/usr/bin/env bash
# reuse_check.sh — psu_retrain_script Step 0 reuse gate.
# The strategy is the constant finetune-from-supernet → the authoritative product set
# is retrain.py + finetune.py + run_retrain.sh. REUSE iff all three exist, the python
# files parse, and the launcher references retrain.py. A set missing finetune.py is an
# incomplete product (no weight-inheritance / teacher-KD seam), never a reusable one.
set -euo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

if [ -s retrain.py ] && [ -s finetune.py ] && [ -s run_retrain.sh ]; then
  if python3 -c "import ast; ast.parse(open('retrain.py').read())" 2>/dev/null \
     && python3 -c "import ast; ast.parse(open('finetune.py').read())" 2>/dev/null \
     && grep -q "retrain" run_retrain.sh; then
    echo "REUSE: retrain.py + finetune.py + run_retrain.sh already exist and pass validation → skip generation, go straight to emitting the output JSON"
    exit 0
  fi
fi

echo "NO_REUSE: retrain.py / finetune.py / run_retrain.sh missing or below the bar" >&2
exit 1
