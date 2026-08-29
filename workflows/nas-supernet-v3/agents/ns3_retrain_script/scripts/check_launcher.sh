#!/usr/bin/env bash
# check_launcher.sh — line-anchored grep gate for single-device launcher defaults.
# Fail loud: AMP not false / torchrun present / NUM_WORKERS != 0 / no python entry.
# Parameterized: LAUNCHER="${1:-run_retrain.sh}".
set -euo pipefail

LAUNCHER="${1:-run_retrain.sh}"
ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }

if [ ! -s "$LAUNCHER" ]; then
  echo "SKIP: $LAUNCHER not found (not yet generated)"
  exit 0
fi

echo "[check_launcher] checking $LAUNCHER"

# AMP=false must be an assignment line (not comment)
grep -E '^[[:space:]]*AMP=false[[:space:]]' "$LAUNCHER" || {
  echo "FAIL: AMP default not false (assignment line AMP=false missing)"
  exit 1
}

# Launcher must NOT contain torchrun (default single-process)
if grep -q 'torchrun' "$LAUNCHER"; then
  echo "FAIL: torchrun present in launcher (default must be plain python3)"
  exit 1
fi

# NUM_WORKERS=0 assignment line
grep -E '^[[:space:]]*NUM_WORKERS=0[[:space:]]*' "$LAUNCHER" || {
  echo "FAIL: NUM_WORKERS=0 assignment line missing"
  exit 1
}

# python3 entry point must exist (retrain.py is the always-present main entry)
grep -Eq 'python3.*retrain\.py' "$LAUNCHER" || {
  echo "FAIL: no 'python3 retrain.py' main call"
  exit 1
}

echo "PASS: check_launcher ($LAUNCHER)"
