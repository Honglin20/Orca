#!/usr/bin/env bash
# E2E launch script: headless tars run of workflows/kd-nas.yaml against
# examples/kd-nas-demo. Isolates state (backs up prior artifacts/kd-nas/) so
# every node does real work (no ledger-driven skip). Reproducible.
set -euo pipefail

REPO="/mnt/d/Projects/Orca"
cd "$REPO"

# Activate orca venv (tars / opencode on PATH)
source ~/miniconda3/etc/profile.d/conda.sh
conda activate orca

# KB dir -> demo KB (setup/gate enumerate $ORCA_KB_DIR/families/receiver)
export ORCA_KB_DIR="$REPO/examples/kd-nas-demo/knowledge_base"

# Isolate state: back up prior artifacts so train node really distills (no skip).
if [ -d "$REPO/examples/kd-nas-demo/artifacts/kd-nas" ]; then
  BAK="$REPO/examples/kd-nas-demo/artifacts/kd-nas.prior$(date -u +%Y%m%d-%H%M%S)-bak"
  mv "$REPO/examples/kd-nas-demo/artifacts/kd-nas" "$BAK"
  echo "[launch] backed up prior artifacts/kd-nas -> $BAK"
fi

# Launch headless background run.
tars run workflows/kd-nas.yaml \
  -i user_train_script="$REPO/examples/kd-nas-demo/train.py" \
  -i latency_provider="$REPO/examples/kd-nas-demo/latency_provider.py::measure" \
  -i baseline_model_path="$REPO/examples/kd-nas-demo/baseline_model.py" \
  -i target_latency_ms=5.0 \
  -i accuracy_baseline=1.5 \
  -i accuracy_baseline_kind=nmse \
  -i device=cpu \
  -i full_epochs=1 \
  --background
