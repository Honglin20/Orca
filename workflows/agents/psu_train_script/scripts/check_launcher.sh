#!/usr/bin/env bash
# check_launcher.sh — line-anchored grep gate for single-device launcher defaults.
# Fail loud: AMP not false / torchrun present / NUM_WORKERS != 0 / no python entry / no PRETRAINED_CKPT wiring.
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
grep -E '^[[:space:]]*AMP=false([[:space:]]|$)' "$LAUNCHER" || {
  echo "FAIL: AMP default not false (assignment line AMP=false missing)"
  exit 1
}

# Launcher must NOT actively invoke torchrun (default single-process); full-line comments excluded
if grep -v '^[[:space:]]*#' "$LAUNCHER" | grep -q 'torchrun'; then
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

# PRETRAINED_CKPT 必须定义为变量并下传（teacher 构建 + 权重继承的统一来源）
grep -E '^[[:space:]]*PRETRAINED_CKPT=' "$LAUNCHER" || {
  echo "FAIL: PRETRAINED_CKPT assignment line missing"
  exit 1
}
grep -q -- '--pretrained_ckpt' "$LAUNCHER" || {
  echo "FAIL: launcher does not pass --pretrained_ckpt to train_supernet.py"
  exit 1
}

echo "PASS: check_launcher ($LAUNCHER)"
