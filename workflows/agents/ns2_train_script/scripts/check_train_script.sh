#!/usr/bin/env bash
# check_train_script.sh — deterministic gate for ns2_train_script artifacts.
# Checks: py_compile train_supernet.py + launcher hygiene + conditional DDP + guarded sync_random_seed.
set -euo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"
FAIL=0

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }
echo "[check_train_script] artifacts_dir=$ARTIFACTS_DIR"

# ── 1. py_compile train_supernet.py ─────────────────────────────────────
if [ -s "train_supernet.py" ]; then
  python3 -m py_compile train_supernet.py || {
    echo "FAIL: train_supernet.py py_compile failed"
    exit 1
  }
  echo "[check_train_script] py_compile train_supernet.py OK"
else
  echo "SKIP: train_supernet.py not found (viable=false)"
  exit 0
fi

# ── 2. Conditional DDP wrap ─────────────────────────────────────────────
if ! grep -q 'is_distributed()' train_supernet.py 2>/dev/null; then
  echo "FAIL: train_supernet.py missing is_distributed() guard"
  FAIL=1
fi
if grep -q 'DistributedDataParallel' train_supernet.py 2>/dev/null; then
  # DDP present — must be inside if is_distributed() block
  if ! grep -B5 'DistributedDataParallel' train_supernet.py | grep -q 'is_distributed'; then
    echo "FAIL: DistributedDataParallel not guarded by is_distributed()"
    FAIL=1
  fi
fi
[ "$FAIL" -eq 0 ] && echo "[check_train_script] conditional DDP wrap OK"

# ── 3. Guarded sync_random_seed ──────────────────────────────────────────
if grep -q 'sync_random_seed' train_supernet.py 2>/dev/null; then
  if ! grep -A3 'def sync_random_seed' train_supernet.py | grep -q 'is_distributed'; then
    echo "FAIL: sync_random_seed not guarded (missing 'if not is_distributed()' early return)"
    FAIL=1
  fi
  echo "[check_train_script] guarded sync_random_seed OK"
fi

# ── 4. Launcher hygiene (check_launcher.sh) ──────────────────────────────
if [ -s "run_train_supernet.sh" ]; then
  bash "$ORCA_AGENT_RESOURCES/scripts/check_launcher.sh" run_train_supernet.sh || FAIL=1
fi

# ── Result ──────────────────────────────────────────────────────────────
if [ "$FAIL" -ne 0 ]; then
  echo "FAIL: check_train_script failed"
  exit 1
fi
echo "PASS: check_train_script"
