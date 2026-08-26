#!/bin/bash
# update_status_md.sh —— 写 $ORCA_ARTIFACTS_DIR/bld_status.md（跨唤醒真相源；与上游节点
# 的 project_manifest.md 同落 artifacts 根下）。
# 用法：update_status_md.sh [status]——$1 覆盖 status 字段（默认 running；假死路径传 stuck）。
# epoch 从最新 log **重算**（真相源，不读 stale 估时文件的 epoch——Step 2 假死判定依赖
# MD 的 epoch 反映 log 最新进度）；total/per_epoch 参考 .bld_eta.json 作 ETA。
# 依赖：ORCA_ARTIFACTS_DIR（orca spawn / orca_env.sh 注入）。
set -e
cd "$ORCA_ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable"; exit 1; }
STATUS="${1:-running}"
mkdir -p runs/bld
PID="$(cat runs/bld/.bld_pid 2>/dev/null || echo none)"
ATTEMPT="$(cat runs/bld/.bld_attempt 2>/dev/null || echo 1)"
EPOCH_SUMMARY="$(python3 - <<'PY'
import json, os, re, glob

ad = os.environ["ORCA_ARTIFACTS_DIR"]
logs = sorted(glob.glob(os.path.join(ad, "runs", "bld", "bld.attempt*.log")))
epochs = set()
if logs:
    for ln in open(logs[-1], encoding="utf-8", errors="replace"):
        m = re.search(r'(?:epoch|step)[^0-9]*([0-9]+)', ln, re.IGNORECASE)
        if m:
            epochs.add(int(m.group(1)))
cur = max(epochs) if epochs else 0
total, per_epoch = None, None
eta_path = os.path.join(ad, "runs", "bld", ".bld_eta.json")
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
cat > bld_status.md <<EOF
# bld_status.md — pz_build_library 训练状态（本节点维护，跨唤醒真相源）

- status: $STATUS
- attempt: $ATTEMPT
- pid: $PID
- log: runs/bld/bld.attempt$ATTEMPT.log
- $EPOCH_SUMMARY
- last_check_at: $(date -Is 2>/dev/null || date)
- next_check: monitor 轮询/turn 到顶换 sub-agent
- note: BLD 完成前本节点不产出 JSON；有界轮询 + 无上限自愈，完成则输出 executed
EOF
echo "MD_UPDATED"
