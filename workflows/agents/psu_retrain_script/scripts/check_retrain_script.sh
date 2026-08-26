#!/usr/bin/env bash
# check_retrain_script.sh — deterministic gate for psu_retrain_script artifacts.
# Checks: py_compile retrain.py + finetune.py (both mandatory — the strategy is the
#         constant finetune-from-supernet; finetune.py is the weight-inheritance +
#         frozen-teacher KD seam) + conditional DDP + guarded sync_random_seed +
#         launcher hygiene (delegated) + progress.jsonl write contract + teacher/KD
#         static gates.
set -euo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"
FAIL=0

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }
echo "[check_retrain_script] artifacts_dir=$ARTIFACTS_DIR"

# ── 1. py_compile retrain.py + finetune.py (both required) ──────────────────────
if [ ! -s "retrain.py" ]; then
  echo "FAIL: retrain.py missing (the main entry must always be generated)"
  exit 1
fi
python3 -m py_compile retrain.py || {
  echo "FAIL: retrain.py py_compile failed"
  exit 1
}
echo "[check_retrain_script] py_compile retrain.py OK"

if [ ! -s "finetune.py" ]; then
  echo "FAIL: finetune.py missing (the constant finetune-from-supernet strategy always generates it — it owns the weight-inheritance + frozen-teacher/KD seam)"
  exit 1
fi
python3 -m py_compile finetune.py || {
  echo "FAIL: finetune.py py_compile failed"
  exit 1
}
echo "[check_retrain_script] py_compile finetune.py OK"

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
# Static early signal; runtime content validation is in psu_retrain's warmup_poll.sh
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

# ── 4b. progress.jsonl 写入粒度（每 N 步 + progress unit 末必写）──────────
# 契约 §3(b)：feed 粒度 = 步级（--progress-every 默认 50，unit 末必写）。仅按
# progress unit 写 = 曲线过稀（真实 E2E：1 epoch → 1 点）。静态早期信号：
# --progress-every 参数或等价 step-取模周期写条件，二者其一（任一文件命中即可）。
GRANULARITY_OK=0
for f in retrain.py finetune.py; do
  [ -s "$f" ] || continue
  if grep -qE 'progress[-_]every' "$f" 2>/dev/null \
     || grep -qE '(global_)?step[[:space:]]*%' "$f" 2>/dev/null; then
    GRANULARITY_OK=1
    break
  fi
done
if [ "$GRANULARITY_OK" -eq 0 ]; then
  echo "FAIL: progress.jsonl 写粒度不符契约（需 --progress-every（默认 50，可覆盖）或等价 step-取模周期写；仅按 progress unit 写 = 曲线过稀）"
  FAIL=1
fi
[ "$FAIL" -eq 0 ] && echo "[check_retrain_script] progress.jsonl write granularity OK"

# ── 4c. 确定性终态指标行（final_metrics 数据源）────────────────────────────
# 契约 §3(c)：`[eval] unit N <metric> <v>`（每个评估点）+ `done best <metric> <v>`
# （训练结束）。psu_retrain 终态 retrain_status.md 刷新与 psu_report 的 final_metrics
# 都按此结构解析；缺失 = final 报告拿不到真实数字（真实 E2E：final_metrics 读到 running 残留）。
if ! grep -qF '[eval] unit' retrain.py finetune.py 2>/dev/null; then
  echo "FAIL: retrain.py/finetune.py 缺 '[eval] unit <N> <metric> <value>' 确定性评估指标行（psu_report final_metrics 的数据源）"
  FAIL=1
fi
if ! grep -qF 'done best' retrain.py finetune.py 2>/dev/null; then
  echo "FAIL: retrain.py/finetune.py 缺 'done best <metric> <value>' 确定性终态指标行（终态 best 指标的数据源）"
  FAIL=1
fi
[ "$FAIL" -eq 0 ] && echo "[check_retrain_script] terminal metric lines OK"

# ── 5. Frozen-teacher KD + weight-inheritance static gates ──────────────────────
# The KD finetune premise is checkable mechanically: teacher constructed via
# load_pretrained.py + frozen (requires_grad_(False) + eval + no_grad forward),
# KD loss composed in finetune.py, freeze continuation (trainable-filtered optimizer).
KD_HOST=0
for f in retrain.py finetune.py; do
  if grep -Eq 'load_pretrained|teacher' "$f" 2>/dev/null; then
    KD_HOST=1
    break
  fi
done
if [ "$KD_HOST" -eq 0 ]; then
  echo "FAIL: neither retrain.py nor finetune.py references the teacher / load_pretrained.py (missing frozen-teacher KD seam)"
  FAIL=1
fi
if ! grep -Eq 'requires_grad_\(False\)' retrain.py finetune.py 2>/dev/null; then
  echo "FAIL: freeze grouping not found (no requires_grad_(False) in retrain.py/finetune.py — freeze continuation + teacher freeze missing)"
  FAIL=1
fi
if ! grep -Eq 'no_grad' retrain.py finetune.py 2>/dev/null; then
  echo "FAIL: teacher no_grad forward not found in retrain.py/finetune.py"
  FAIL=1
fi
if ! grep -Eq 'kd_loss|kd[-_]hidden|kd[-_]logits|cosine.*kl|hidden.*cosine' retrain.py finetune.py 2>/dev/null; then
  echo "FAIL: KD loss composition not found in retrain.py/finetune.py (expected the hidden-cosine + logits-KL helpers mirrored from train_supernet.py)"
  FAIL=1
fi
[ "$FAIL" -eq 0 ] && echo "[check_retrain_script] frozen-teacher KD + weight-inheritance gates OK"

# ── Result ──────────────────────────────────────────────────────────────────────
if [ "$FAIL" -ne 0 ]; then
  echo "FAIL: check_retrain_script failed"
  exit 1
fi
echo "PASS: check_retrain_script"
