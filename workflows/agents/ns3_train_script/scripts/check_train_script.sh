#!/usr/bin/env bash
# check_train_script.sh — deterministic gate for ns3_train_script artifacts.
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

# ── 5. Progress JSONL write contract (chart feed, §3(b)) ────────────────
# progress.jsonl 是 progress_watcher 的 chart feed。漏写 = 训练 executed 但无实时图。
# 静态早期信号；运行时强校验在 warmup_poll.sh 的 check_progress_contract.py。
if ! grep -q 'progress\.jsonl' train_supernet.py 2>/dev/null; then
  echo "FAIL: train_supernet.py 不写 progress.jsonl（缺 chart feed——progress_watcher 无数据可推）"
  FAIL=1
elif ! grep -q 'json\.dumps' train_supernet.py 2>/dev/null; then
  echo "FAIL: train_supernet.py 引用 progress.jsonl 但未见 json.dumps（契约: json.dumps({\"step\":..,\"metrics\":..})）"
  FAIL=1
fi
[ "$FAIL" -eq 0 ] && echo "[check_train_script] progress.jsonl write contract OK"

# ── 6. KD warmup defaults nonzero (when KD enabled) ──────────────────────
# KD enabled ⟺ train_supernet.py exposes --kd_warmup_start / --kd_warmup_length.
# Both argparse defaults must not be literal 0 (delayed start + nonzero ramp, §8 KD Weight Warmup).
# "≈ 1/4 of budget" is a semantic judgment → workflow-verifier checklist; this gate only catches the
# deterministic bad default (0).
if grep -q 'kd_warmup_start\|kd_warmup_length' train_supernet.py 2>/dev/null; then
  python3 -c "
import re, sys
src = open('train_supernet.py').read()
bad = []
for arg in ('kd_warmup_start', 'kd_warmup_length'):
    m = re.search(rf\"add_argument\(\s*['\\\"]--{arg}['\\\"].*?default\s*=\s*([^,\n\)]+)\", src, re.DOTALL)
    if m and re.fullmatch(r'0(\.0)?', m.group(1).strip()):
        bad.append('--' + arg)
if bad:
    print('FAIL: KD warmup argparse default is literal 0 for: ' + ', '.join(bad))
    sys.exit(1)
print('KD_WARMUP_DEFAULTS_OK')
" || FAIL=1
  [ "$FAIL" -eq 0 ] && echo "[check_train_script] KD warmup defaults nonzero OK"
fi

# ── Result ──────────────────────────────────────────────────────────────
if [ "$FAIL" -ne 0 ]; then
  echo "FAIL: check_train_script failed"
  exit 1
fi
echo "PASS: check_train_script"
