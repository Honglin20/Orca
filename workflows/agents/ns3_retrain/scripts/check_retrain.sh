#!/usr/bin/env bash
# check_retrain.sh — deterministic gate for ns3_retrain artifacts.
# Checks: retrain.py/finetune.py py_compile + run_retrain.sh launcher hygiene + conditional DDP.
set -euo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"
FAIL=0

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }
echo "[check_retrain] artifacts_dir=$ARTIFACTS_DIR"

# ── 1. py_compile generated scripts (if exist) ──────────────────────────
for f in retrain.py finetune.py; do
  [ -s "$f" ] || continue
  python3 -m py_compile "$f" || {
    echo "FAIL: $f py_compile failed"
    FAIL=1
  }
done

# ── 2. run_retrain.sh launcher hygiene ──────────────────────────────────
if [ -s "run_retrain.sh" ]; then
  # AMP=false (v2 default)
  grep -E '^[[:space:]]*AMP=false[[:space:]]' run_retrain.sh || {
    echo "FAIL: run_retrain.sh AMP default not false"
    FAIL=1
  }
  # No torchrun in default launcher
  if grep -q 'torchrun' run_retrain.sh; then
    echo "FAIL: run_retrain.sh contains torchrun (default must be plain python3)"
    FAIL=1
  fi
  # NUM_WORKERS=0
  grep -E '^[[:space:]]*NUM_WORKERS=0[[:space:]]*' run_retrain.sh || {
    echo "FAIL: run_retrain.sh NUM_WORKERS=0 missing"
    FAIL=1
  }
  echo "[check_retrain] run_retrain.sh launcher hygiene OK"
fi

# ── 3. Conditional DDP in retrain.py (if exists) ────────────────────────
if [ -s "retrain.py" ]; then
  if grep -q 'DistributedDataParallel' retrain.py 2>/dev/null; then
    if ! grep -B5 'DistributedDataParallel' retrain.py | grep -q 'is_distributed'; then
      echo "FAIL: retrain.py DDP not guarded by is_distributed()"
      FAIL=1
    fi
  fi
  # Guarded sync_random_seed (if function exists)
  if grep -q 'def sync_random_seed' retrain.py 2>/dev/null; then
    if ! grep -A3 'def sync_random_seed' retrain.py | grep -q 'is_distributed'; then
      echo "FAIL: retrain.py sync_random_seed not guarded"
      FAIL=1
    fi
  fi
fi

# ── 4. Progress JSONL write contract (chart feed, agent.md Step 3a) ─────
# progress.jsonl 是 progress_watcher 的 chart feed。漏写 = retrain executed 但无实时图。
# 静态早期信号；运行时强校验在 warmup_poll.sh 的 check_progress_contract.py。
# retrain 可能生成 retrain.py 或 finetune.py，至少其一写 progress.jsonl 即可。
if ls retrain.py finetune.py 2>/dev/null | grep -q .; then
  PROGRESS_WRITER=0
  for f in retrain.py finetune.py; do
    [ -s "$f" ] || continue
    if grep -q 'progress\.jsonl' "$f" 2>/dev/null && grep -q 'json\.dumps' "$f" 2>/dev/null; then
      PROGRESS_WRITER=1
      break
    fi
  done
  if [ "$PROGRESS_WRITER" -eq 0 ]; then
    echo "FAIL: retrain.py/finetune.py 均不写 progress.jsonl（缺 chart feed，需 json.dumps({\"step\":..,\"metrics\":..})）"
    FAIL=1
  else
    echo "[check_retrain] progress.jsonl write contract OK"
  fi
fi

# ── Result ──────────────────────────────────────────────────────────────
if [ "$FAIL" -ne 0 ]; then
  echo "FAIL: check_retrain failed"
  exit 1
fi
echo "PASS: check_retrain"
