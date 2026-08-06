#!/usr/bin/env bash
# 小测试：验证 ns_run_train deferred-training-cron 行为序列。
# 忠实复制 ns_run_train/agent.md 的 Step 2 (2a/2b/2c/2d/2e) bash 块，两处测试适配：
#   1. warmup sleep 240s -> 3s（fake 训练每 epoch ~2s，加速测试）
#   2. Jinja ({{ inputs.project_root }} / {{ inputs | tojson }}) 预渲染成 fake 值
# fake 训练脚本：3 epoch ×~2s，时间戳 epoch 行 + loss，末尾写 ckpt；含字面 `--epochs 3`（2c 解析）。
# 通过判据（对齐目标）：warmup 确认能跑通 -> 估时 -> 设 cron -> park(detached)。
set -u
TESTDIR="$(mktemp -d)"
export ORCA_ARTIFACTS_DIR="$TESTDIR/artifacts"
PROJROOT="$TESTDIR/project"
mkdir -p "$ORCA_ARTIFACTS_DIR" "$PROJROOT/runs/train"
cd "$ORCA_ARTIFACTS_DIR"

echo "### TESTDIR=$TESTDIR"
echo "### ORCA_ARTIFACTS_DIR=$ORCA_ARTIFACTS_DIR"

# ── fake 训练脚本 ──
cat > run_train_supernet.sh <<'SHELL'
#!/bin/bash
# mock training --epochs 3
for ep in 1 2 3; do
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] epoch $ep/3  loss=0.$((50-ep*5))  acc=0.8$ep"
  sleep 2
done
mkdir -p runs/train
python3 -c "import torch; torch.save({'state_dict':{'w':torch.zeros(1)}}, 'runs/train/supernet_best.pth')" 2>/dev/null || echo "ckpt-write-skipped"
echo "TRAINING_DONE rc=0"
SHELL
chmod +x run_train_supernet.sh

N=1

# ── 2a detach（忠实）──
mkdir -p runs/train
rm -f runs/train/.train_pid runs/train/.train_rc .train_eta.txt .cron_rerun.sh .cron_rerun_inputs.json .cron_registered.flag
nohup bash -c 'bash run_train_supernet.sh > "runs/train/train.attempt'"$N"'.log" 2>&1; echo $? > runs/train/.train_rc' >/dev/null 2>&1 &
echo $! > runs/train/.train_pid
echo "2a: DETACHED pid=$(cat runs/train/.train_pid)"

# ── 2b warmup（sleep 240->3；忠实检测逻辑）──
LOG="runs/train/train.attempt${N}.log"
EPOCH_CNT=0
for i in 1 2 3 4 5 6; do
  PID="$(cat runs/train/.train_pid 2>/dev/null)"
  if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
    echo "2b: WARMUP_FAIL (process exited early) rc=$(cat runs/train/.train_rc 2>/dev/null)"; tail -20 "$LOG"; exit 1
  fi
  sleep 3
  EPOCH_LINES="$(grep -iE 'epoch[^0-9]*[0-9]+' "$LOG" 2>/dev/null | tail -5)"
  LOSS_LINE="$(grep -iE 'loss[^0-9-]*[0-9]' "$LOG" 2>/dev/null | tail -1)"
  if printf '%s' "$LOSS_LINE" | grep -iE 'loss[^0-9-]*(nan|inf)' >/dev/null; then
    echo "2b: WARMUP_FAIL (loss diverged)"; exit 1
  fi
  EPOCH_CNT="$(printf '%s\n' "$EPOCH_LINES" | grep -oiE 'epoch[^0-9]*[0-9]+' | grep -oiE '[0-9]+' | sort -u | wc -l)"
  if [ "$EPOCH_CNT" -ge 2 ]; then echo "2b: WARMUP_OK epoch_cnt=$EPOCH_CNT (poll $i)"; break; fi
  echo "2b: WARMUP_RUNNING epoch_cnt=$EPOCH_CNT (poll $i)"
done
if [ "$EPOCH_CNT" -lt 2 ]; then echo "FAIL: warmup 未在 6 次轮询内见到 ≥2 epoch（未确认能跑通）"; exit 1; fi

# ── 2c 估时（忠实 python）──
export LOG_PATH="runs/train/train.attempt${N}.log"
python3 - <<'PY'
import os, re, sys, json
ad = os.environ["ORCA_ARTIFACTS_DIR"]
log_rel = os.environ.get("LOG_PATH", "runs/train/train.attempt1.log")
log_path = os.path.join(ad, log_rel) if not os.path.isabs(log_rel) else log_rel
total_epochs = None
sh = os.path.join(ad, "run_train_supernet.sh")
if os.path.exists(sh):
    txt = open(sh, encoding="utf-8", errors="replace").read()
    m = re.search(r'--epochs\s+(\d+)', txt)
    if m: total_epochs = int(m.group(1))
if total_epochs is None or total_epochs < 1:
    print(json.dumps({"error": f"cannot parse total epochs (got {total_epochs})"})); sys.exit(1)
lines = open(log_path, encoding="utf-8", errors="replace").read().splitlines()
ts_of = {}
for ln in lines:
    m_epoch = re.search(r'epoch[^0-9]*([0-9]+)', ln, re.IGNORECASE)
    if not m_epoch: continue
    ep = int(m_epoch.group(1))
    m_ts = re.search(r'(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})', ln)
    if m_ts and ep not in ts_of: ts_of[ep] = m_ts.group(1)
per_epoch = None
if len(ts_of) >= 2:
    eps = sorted(ts_of)
    from datetime import datetime
    d0 = datetime.strptime(ts_of[eps[0]], "%Y-%m-%d %H:%M:%S")
    d1 = datetime.strptime(ts_of[eps[1]], "%Y-%m-%d %H:%M:%S")
    per_epoch = (d1 - d0).total_seconds()
if per_epoch is None or per_epoch < 1: per_epoch = 60
cur_epoch = max(ts_of) if ts_of else 0
remaining_epochs = max(total_epochs - cur_epoch, 1)
remaining_sec = remaining_epochs * per_epoch
remaining_min = max(int(remaining_sec / 60), 1)
out = {"total_epochs": total_epochs, "current_epoch": cur_epoch, "per_epoch_seconds": per_epoch,
       "remaining_epochs": remaining_epochs, "remaining_seconds": remaining_sec, "remaining_minutes": remaining_min}
with open(os.path.join(ad, ".train_eta.txt"), "w", encoding="utf-8") as f: json.dump(out, f)
print(json.dumps(out))
PY
echo "2c: estimate -> $(cat .train_eta.txt)"

# ── 2d cron 注册（忠实；Jinja 预渲染：project_root + inputs json 直接写）──
MARKER="ORCA_CRON_NS_SUPERNET_TRAIN"
SCRIPT="$ORCA_ARTIFACTS_DIR/.cron_rerun.sh"
INPUTS_JSON="$ORCA_ARTIFACTS_DIR/.cron_rerun_inputs.json"
FLAG="$ORCA_ARTIFACTS_DIR/.cron_registered.flag"
T_MIN="$(python3 -c 'import json,os; print(json.load(open(os.path.join(os.environ["ORCA_ARTIFACTS_DIR"],".train_eta.txt")))["remaining_minutes"])')"
[ -n "$T_MIN" ] && [ "$T_MIN" -gt 0 ] 2>/dev/null || { echo "2d: FATAL T_MIN invalid '$T_MIN'"; exit 1; }
# 预渲染 inputs json（agent 里是 {{ inputs | tojson }}）
cat > "$INPUTS_JSON" <<EOF
{"project_root":"$PROJROOT","model_path":"model.py","target_latency_ms":5,"seed":0}
EOF
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$INPUTS_JSON" || { echo "2d: FATAL inputs json invalid"; exit 1; }
# 自包含重跑脚本（agent 里 cd "{{ inputs.project_root }}"）
cat > "$SCRIPT" <<EOF
#!/bin/bash
set -e
export PATH="\$PATH:$HOME/.local/bin"
cd "$PROJROOT"
orca nas-supernet --inputs "\$(cat '$INPUTS_JSON')"
EOF
chmod +x "$SCRIPT"
bash -n "$SCRIPT" || { echo "2d: FATAL .cron_rerun.sh syntax invalid"; exit 1; }
if command -v crontab >/dev/null 2>&1; then
    crontab -l 2>/dev/null | grep -v "$MARKER" | crontab - 2>/dev/null || true
fi
CRON_REGISTERED="none"
if command -v at >/dev/null 2>&1 && atq >/dev/null 2>&1; then
    echo "bash '$SCRIPT' && rm -f '$SCRIPT' '$INPUTS_JSON'" | at "now + $T_MIN minutes" 2>/dev/null && CRON_REGISTERED="at"
fi
if [ "$CRON_REGISTERED" = "none" ] && command -v crontab >/dev/null 2>&1; then
    FIRE="$(date -d "+$T_MIN minutes" "+%M %H %d %m" 2>/dev/null)"
    if [ -n "$FIRE" ]; then
        CRON_LINE="$FIRE * bash '$SCRIPT' && (crontab -l 2>/dev/null | grep -v '$MARKER' | crontab -) && rm -f '$SCRIPT' '$INPUTS_JSON' # $MARKER"
        (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab - 2>/dev/null && CRON_REGISTERED="crontab"
    fi
fi
if [ "$CRON_REGISTERED" = "none" ]; then
    rm -f "$ORCA_ARTIFACTS_DIR/.train_eta.txt"; echo "2d: FATAL no at/crontab"; exit 1
fi
printf 'true' > "$FLAG"
echo "2d: CRON_REGISTERED=$CRON_REGISTERED t_min=$T_MIN"

# ── 2e park（忠实 assessment）──
SUMMARY="$(python3 - <<'PY'
import json, os
ad = os.environ["ORCA_ARTIFACTS_DIR"]
try:
    with open(os.path.join(ad, ".train_eta.txt"), encoding="utf-8") as f: d = json.load(f)
except Exception: d = {}
print(f"training detached, ~{d.get('remaining_minutes','?')}min remaining (per_epoch={d.get('per_epoch_seconds','?')}s, epoch={d.get('current_epoch','?')}/{d.get('total_epochs','?')}), cron re-registered")
PY
)"
printf '%s' "$SUMMARY" > .ns_run_train_assessment.txt
echo "2e: park -> $SUMMARY"

# ── 断言（目标判据）──
echo ""
echo "===== ASSERTIONS (目标：warmup 确认跑通 -> 估时 -> 设 cron -> park detached) ====="
PASS=0; FAIL=0
assert() { if eval "$1"; then echo "PASS: $2"; PASS=$((PASS+1)); else echo "FAIL: $2"; FAIL=$((FAIL+1)); fi; }
assert '[ "$EPOCH_CNT" -ge 2 ]' "warmup 确认能跑通（≥2 epoch 标记）"
assert '[ -f .train_eta.txt ]' "估时落 .train_eta.txt"
assert 'python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d[\"remaining_minutes\"]>=1 and d[\"per_epoch_seconds\"]>0" .train_eta.txt' "估时值合理（remaining≥1min, per_epoch>0）"
assert '[ -f .cron_rerun.sh ]' "cron 重跑脚本 .cron_rerun.sh 存在"
assert 'grep -q "orca nas-supernet" .cron_rerun.sh' "重跑脚本用 orca nas-supernet（驱动 workflow 的 CLI）"
assert '[ -f .cron_registered.flag ]' "cron 已注册（.cron_registered.flag）"
assert 'crontab -l 2>/dev/null | grep -q "$MARKER"' "crontab 里有自清理 one-shot 条目"
assert 'grep -q "detached" .ns_run_train_assessment.txt' "park 落 detached assessment"

echo ""
echo "===== RESULT: $PASS passed, $FAIL failed ====="
# 清 crontab 测试条目（测试完即清，不污染用户 crontab）
crontab -l 2>/dev/null | grep -v "$MARKER" | crontab - 2>/dev/null || true
# 杀残留训练进程（若有）
[ -f runs/train/.train_pid ] && kill "$(cat runs/train/.train_pid)" 2>/dev/null || true
rm -rf "$TESTDIR"
[ "$FAIL" -eq 0 ]
