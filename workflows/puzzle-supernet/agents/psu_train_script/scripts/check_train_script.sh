#!/usr/bin/env bash
# check_train_script.sh — deterministic gate for psu_train_script artifacts.
# Checks: py_compile train_supernet.py + conditional DDP + guarded sync_random_seed + launcher hygiene
#         + progress.jsonl chart feed + 固定 KD 范式静态 gate ×6.
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

# ── 5. Progress JSONL write contract (chart feed, §3(b)) ────────────────
# progress.jsonl 是 progress_watcher 的 chart feed。漏写 = 训练 executed 但无实时图。
# 静态早期信号；运行时强校验在 psu_run_train 的 check_progress_contract.py。
if ! grep -q 'progress\.jsonl' train_supernet.py 2>/dev/null; then
  echo "FAIL: train_supernet.py 不写 progress.jsonl（缺 chart feed——progress_watcher 无数据可推）"
  FAIL=1
elif ! grep -q 'json\.dumps' train_supernet.py 2>/dev/null; then
  echo "FAIL: train_supernet.py 引用 progress.jsonl 但未见 json.dumps（契约: json.dumps({\"step\":..,\"metrics\":..})）"
  FAIL=1
fi
[ "$FAIL" -eq 0 ] && echo "[check_train_script] progress.jsonl write contract OK"

# ── 5b. progress.jsonl 写入粒度（每 N 步 + progress unit 末必写）──────────
# 契约 §3(b)：feed 粒度 = 步级（--progress-every 默认 50，unit 末必写）。仅按
# progress unit（每 epoch 一条）写 = 曲线过稀（真实 E2E：5 epoch → 5 点）。
# 静态早期信号：--progress-every 参数或等价 step-取模周期写条件，二者其一。
if ! grep -qE 'progress[-_]every' train_supernet.py 2>/dev/null \
   && ! grep -qE '(global_)?step[[:space:]]*%' train_supernet.py 2>/dev/null; then
  echo "FAIL: train_supernet.py progress.jsonl 写粒度不符契约（需 --progress-every（默认 50，可覆盖）或等价 step-取模周期写；仅按 progress unit 写 = 曲线过稀）"
  FAIL=1
fi
[ "$FAIL" -eq 0 ] && echo "[check_train_script] progress.jsonl write granularity OK"

# ── 6. PSU 固定 KD 范式静态 gate ×6 ──────────────────────────────────────
# 确定性早期信号；语义级校验（recipe 组成/断言覆盖面）在 workflow-verifier checklist。

# 6a. --pretrained_ckpt 必须在 argparse 定义（teacher 构建 + 权重继承统一来源）
# python 正则而非 grep：\s 跨行，容忍 ruff format 的 add_argument( 换行风格。
python3 -c "
import re, sys
src = open('train_supernet.py').read()
if not re.search(r\"add_argument\(\s*['\\\"]--pretrained_ckpt\", src):
    print('FAIL: train_supernet.py 未在 argparse 定义 --pretrained_ckpt（teacher 构建与权重继承的统一来源）')
    sys.exit(1)
print('PRETRAINED_CKPT_ARG_OK')
" || FAIL=1

# 6b. freeze 分组：requires_grad_(False) 必须存在（original 分支 + 非 slot 模块冻结，只训变体分支）
if ! grep -q 'requires_grad_(False)' train_supernet.py 2>/dev/null; then
  echo "FAIL: train_supernet.py 缺 requires_grad_(False)（freeze 分组缺失——只训变体分支参数的前提）"
  FAIL=1
fi

# 6c. teacher 冻结前向：teacher 实例 + no_grad + .eval() 三要素齐备
if ! grep -q 'teacher' train_supernet.py 2>/dev/null \
   || ! grep -q 'no_grad' train_supernet.py 2>/dev/null \
   || ! grep -q '\.eval()' train_supernet.py 2>/dev/null; then
  echo "FAIL: teacher 冻结契约不完整（需 teacher 实例 + no_grad 前向 + .eval()）"
  FAIL=1
fi

# 6d. optimizer 只接收 trainable 过滤后的参数——禁裸 model.parameters() 进 optimizer
python3 -c "
import re, sys
src = open('train_supernet.py').read()
bad = re.search(r'optim\.[A-Za-z_]+\(\s*model\.parameters\(\)', src) or re.search(
    r'\b(?:AdamW?|SGD|RMSprop|Adagrad|Adadelta)\w*\(\s*model\.parameters\(\)', src)
if bad:
    print('FAIL: optimizer 直接接收裸 model.parameters()（必须传入 requires_grad 过滤后的变体分支参数）')
    sys.exit(1)
print('OPTIMIZER_TRAINABLE_ONLY_OK')
" || FAIL=1

# 6e. 全量保存契约：save_checkpoint_ddp 存在 + 禁 requires_grad 过滤 state_dict（下游 evaluator strict 加载）
if ! grep -q 'save_checkpoint_ddp' train_supernet.py 2>/dev/null; then
  echo "FAIL: train_supernet.py 未使用 save_checkpoint_ddp（全模块 state_dict 保存的载体）"
  FAIL=1
fi
if grep -n 'state_dict' train_supernet.py 2>/dev/null | grep -vE '^[0-9]+:[[:space:]]*#' | grep -q 'requires_grad'; then
  echo "FAIL: state_dict 保存被 requires_grad 过滤（契约: 全模块保存含冻结参数——下游 evaluator strict 加载）"
  FAIL=1
fi

# 6f. 启动期确定性断言：权重继承抽查（allclose 比对）+ fail-loud 载体（assert/raise）
if ! grep -q 'allclose' train_supernet.py 2>/dev/null; then
  echo "FAIL: 缺启动期权重继承抽查断言（original 分支参数 vs teacher 参数的 torch.allclose 比对）"
  FAIL=1
fi
if ! grep -qE '\bassert\b|\braise\b' train_supernet.py 2>/dev/null; then
  echo "FAIL: 启动期断言无 fail-loud 载体（assert / raise 均未见）"
  FAIL=1
fi

[ "$FAIL" -eq 0 ] && echo "[check_train_script] PSU KD contract gates OK"

# ── Result ──────────────────────────────────────────────────────────────
if [ "$FAIL" -ne 0 ]; then
  echo "FAIL: check_train_script failed"
  exit 1
fi
echo "PASS: check_train_script"
