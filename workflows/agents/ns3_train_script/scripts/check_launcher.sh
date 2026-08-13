#!/usr/bin/env bash
# check_launcher.sh — line-anchored grep gate for single-device launcher defaults.
# Fail loud: AMP not false / torchrun present / NUM_WORKERS != 0 / no python entry.
set -euo pipefail

LAUNCHER="${1:-run_train_supernet.sh}"
ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }

if [ ! -s "$LAUNCHER" ]; then
  echo "SKIP: $LAUNCHER not found (viable=false or not yet generated)"
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

# python3 entry point must exist
grep -q 'python3.*train_supernet.py' "$LAUNCHER" || {
  echo "FAIL: no 'python3 train_supernet.py' main call"
  exit 1
}

# KD warmup variables (when KD enabled): nonzero defaults. Only checked when the
# variable is actually present (retrain launchers may legitimately omit KD vars).
for v in KD_WARMUP_START KD_WARMUP_LENGTH; do
  if grep -qE "^[[:space:]]*${v}=0[[:space:]]*(#.*)?$" "$LAUNCHER"; then
    echo "FAIL: $v=0 in launcher (delayed start + nonzero ramp required per §8 KD Weight Warmup)"
    exit 1
  fi
done

echo "PASS: check_launcher ($LAUNCHER)"
