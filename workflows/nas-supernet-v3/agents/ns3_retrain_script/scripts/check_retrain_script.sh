#!/usr/bin/env bash
# check_retrain_script.sh — deterministic gate for ns3_retrain_script artifacts.
# Checks: py_compile retrain.py(+finetune.py if present) + conditional DDP +
#         guarded sync_random_seed + launcher hygiene (delegated) + progress.jsonl write contract.
# retrain.py is the always-present main entry; finetune.py is conditionally generated
# (finetune-from-supernet strategy only) and may be absent.
set -euo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"
FAIL=0

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }
echo "[check_retrain_script] artifacts_dir=$ARTIFACTS_DIR"

# ── 1. py_compile retrain.py (required main entry) + finetune.py (optional) ─────
if [ ! -s "retrain.py" ]; then
  echo "FAIL: retrain.py missing (the main entry must always be generated)"
  exit 1
fi
python3 -m py_compile retrain.py || {
  echo "FAIL: retrain.py py_compile failed"
  exit 1
}
echo "[check_retrain_script] py_compile retrain.py OK"

if [ -s "finetune.py" ]; then
  python3 -m py_compile finetune.py || {
    echo "FAIL: finetune.py py_compile failed"
    exit 1
  }
  echo "[check_retrain_script] py_compile finetune.py OK"
fi

# ── 2. Conditional DDP wrap + guarded sync_random_seed ──────────────────────────
# retrain.py is the main entry with the training loop → bare is_distributed() guard required.
if ! grep -q 'is_distributed()' retrain.py 2>/dev/null; then
  echo "FAIL: retrain.py missing is_distributed() guard"
  FAIL=1
fi
# Both files: if DDP / sync_random_seed symbols appear, they must be guarded.
# (finetune.py is a weight-inheritance module and may legitimately avoid DDP symbols entirely.)
for f in retrain.py finetune.py; do
  [ -s "$f" ] || continue
  if grep -q 'DistributedDataParallel' "$f" 2>/dev/null; then
    if ! grep -B5 'DistributedDataParallel' "$f" | grep -q 'is_distributed'; then
      echo "FAIL: $f DistributedDataParallel not guarded by is_distributed()"
      FAIL=1
    fi
  fi
  if grep -q 'def sync_random_seed' "$f" 2>/dev/null; then
    if ! grep -A3 'def sync_random_seed' "$f" | grep -q 'is_distributed'; then
      echo "FAIL: $f sync_random_seed not guarded (missing 'if not is_distributed()' early return)"
      FAIL=1
    fi
  fi
done
[ "$FAIL" -eq 0 ] && echo "[check_retrain_script] conditional DDP + guarded sync_random_seed OK"

# ── 3. Launcher hygiene (delegated to check_launcher.sh) ─────────────────────────
if [ -s "run_retrain.sh" ]; then
  bash "$ORCA_AGENT_RESOURCES/scripts/check_launcher.sh" run_retrain.sh || FAIL=1
fi

# ── 4. Progress JSONL write contract (chart feed) ───────────────────────────────
# progress.jsonl is the progress_watcher chart feed. Missing = retrain executes but no live chart.
# Static early signal; runtime content validation is in ns3_retrain's warmup_poll.sh
# (check_progress_contract.py). Either retrain.py or finetune.py writing progress.jsonl
# satisfies the contract.
PROGRESS_WRITER=0
for f in retrain.py finetune.py; do
  [ -s "$f" ] || continue
  if grep -q 'progress\.jsonl' "$f" 2>/dev/null && grep -q 'json\.dumps' "$f" 2>/dev/null; then
    PROGRESS_WRITER=1
    break
  fi
done
if [ "$PROGRESS_WRITER" -eq 0 ]; then
  echo "FAIL: neither retrain.py nor finetune.py writes progress.jsonl (missing chart feed — need json.dumps({\"step\":..,\"metrics\":..}))"
  FAIL=1
else
  echo "[check_retrain_script] progress.jsonl write contract OK"
fi

# ── Result ──────────────────────────────────────────────────────────────────────
if [ "$FAIL" -ne 0 ]; then
  echo "FAIL: check_retrain_script failed"
  exit 1
fi
echo "PASS: check_retrain_script"
