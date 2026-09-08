#!/bin/bash
# update_status_md.sh —— 写 $ORCA_ARTIFACTS_DIR/retrain_status.md（跨唤醒真相源；与上游节点
# 的 supernet_summary.md / project_manifest.md 同落 artifacts 根下）。
# 用法：update_status_md.sh [status] [ckpt]
#   $1 = status 覆盖（默认 running；假死路径传 stuck；status.sh 判定 RETRAIN_COMPLETE 时传
#        completed —— 终态行含 log 尾部确定性 best 指标，防止完成后残留 running 文本被
#        psu_report 的 final_metrics 读走，真实 E2E 事故）。
#   $2 = completed 终态时的 ckpt 路径（写入 ckpt 行；可省略）。
# epoch 从最新 log **重算**（真相源，不读 stale 估时文件的 epoch——Step 2 假死判定依赖
# MD 的 epoch 反映 log 最新进度）；total/per_epoch 参考 .retrain_eta.json 作 ETA。
# 依赖：ORCA_ARTIFACTS_DIR（orca spawn / orca_env.sh 注入）。
set -e
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }
STATUS="${1:-running}"
CKPT_ARG="${2:-}"
mkdir -p runs/retrain
PID="$(cat runs/retrain/.retrain_pid 2>/dev/null || echo none)"
ATTEMPT="$(cat runs/retrain/.retrain_attempt 2>/dev/null || echo 1)"
EPOCH_SUMMARY="$(python3 - <<'PY'
import json, os, re, glob

ad = os.environ["ORCA_ARTIFACTS_DIR"]
logs = sorted(glob.glob(os.path.join(ad, "runs", "retrain", "retrain.attempt*.log")))
epochs = set()
if logs:
    for ln in open(logs[-1], encoding="utf-8", errors="replace"):
        m = re.search(r'(?:epoch|step)[^0-9]*([0-9]+)', ln, re.IGNORECASE)
        if m:
            epochs.add(int(m.group(1)))
cur = max(epochs) if epochs else 0
total, per_epoch = None, None
eta_path = os.path.join(ad, "runs", "retrain", ".retrain_eta.json")
if os.path.exists(eta_path):
    try:
        d = json.load(open(eta_path, encoding="utf-8"))
        total, per_epoch = d.get("total_epochs"), d.get("per_epoch_seconds")
    except Exception:
        pass
eta = None
if total and per_epoch:
    eta = int((total - cur) * per_epoch / 60)
print(f"epoch: {cur}/{total if total else '?'}, per_epoch: {per_epoch if per_epoch else '?'}s, "
      f"eta: {'~'+str(eta)+'min' if eta is not None else 'unknown'}")
PY
)"

if [ "$STATUS" = "completed" ]; then
  # 终态：log 尾部确定性指标行（生成契约 §3(c) 固定）——`[eval] unit N <metric> <v>` /
  # `done best <metric> <v>`（容忍 `[retrain]`/`[train]` 类前缀）。解析不到则 best 行省略
  # （emit_report 侧仍直接解析 log，双保险）。
  BEST_LINE="$(python3 - <<'PY'
import glob, os, re

ad = os.environ["ORCA_ARTIFACTS_DIR"]
logs = sorted(glob.glob(os.path.join(ad, "runs", "retrain", "retrain.attempt*.log")))
num = r"(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
for path in reversed(logs):
    try:
        lines = [l.strip() for l in open(path, encoding="utf-8", errors="replace") if l.strip()]
    except OSError:
        continue
    for ln in reversed(lines):
        m = re.search(r"\bdone best\s+(\S+)\s+" + num, ln)
        if m:
            print(f"best: {m.group(1)} {m.group(2)}")
            raise SystemExit(0)
    for ln in reversed(lines):
        m = re.match(r"\[eval\]\s+unit\s+\d+\s+(\S+)\s+" + num, ln)
        if m:
            print(f"last eval: {m.group(1)} {m.group(2)}")
            raise SystemExit(0)
raise SystemExit(0)
PY
)"
  cat > retrain_status.md <<EOF
# retrain_status.md — psu_retrain 训练状态（本节点维护，跨唤醒真相源）

- status: completed
- attempt: $ATTEMPT
- pid: $PID (exited)
- ckpt: $CKPT_ARG
- log: runs/retrain/retrain.attempt$ATTEMPT.log
- $EPOCH_SUMMARY
- $BEST_LINE
- last_check_at: $(date -Is 2>/dev/null || date)
- note: retrain completed (rc=0, process exited, ckpt valid); 终态行由 status.sh 判定 RETRAIN_COMPLETE 时刷新
EOF
  echo "MD_UPDATED"
  exit 0
fi

cat > retrain_status.md <<EOF
# retrain_status.md — psu_retrain 训练状态（本节点维护，跨唤醒真相源）

- status: $STATUS
- attempt: $ATTEMPT
- pid: $PID
- log: runs/retrain/retrain.attempt$ATTEMPT.log
- $EPOCH_SUMMARY
- last_check_at: $(date -Is 2>/dev/null || date)
- next_check: monitor 轮询/turn 到顶换 sub-agent
- note: 训练完成前本节点不产出 JSON；有界轮询 + 无上限自愈，完成则输出 executed
EOF
echo "MD_UPDATED"
