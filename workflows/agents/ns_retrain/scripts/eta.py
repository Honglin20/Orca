#!/usr/bin/env python3
"""eta.py —— 估时（信息用，落 runs/retrain/.retrain_eta.json，不决定 CRON 时机）。

从 log 抓当前 epoch 数 + run_retrain.sh 的 --epochs（或 retrain.py / finetune.py 的
argparse 默认）→ 每 epoch 耗时（pidfile mtime / 当前 epoch）→ 剩余分钟。stdout 单行 JSON。
依赖：ORCA_ARTIFACTS_DIR（orca spawn / orca_env.sh 注入）。
"""

import glob
import json
import os
import re
import sys
import time

ad = os.environ.get("ORCA_ARTIFACTS_DIR")
if not ad:
    print(json.dumps({"error": "ORCA_ARTIFACTS_DIR unset"}))
    sys.exit(1)

logs = sorted(glob.glob(os.path.join(ad, "runs", "retrain", "retrain.attempt*.log")))
log = logs[-1] if logs else os.path.join(ad, "runs", "retrain", "retrain.attempt1.log")

total = None
sh = os.path.join(ad, "run_retrain.sh")
if os.path.exists(sh):
    txt = open(sh, encoding="utf-8", errors="replace").read()
    m = re.search(r"--epochs\s+(\d+)", txt)
    if not m:
        m = re.search(r"^EPOCHS=(\d+)", txt, re.M)  # launcher 变量形态：EPOCHS=100 + --epochs "$EPOCHS"
    if m:
        total = int(m.group(1))
    else:
        m = re.search(r"--max_steps\s+(\d+)", txt)
        if not m:
            m = re.search(r"^MAX_STEPS=(\d+)", txt, re.M)
        if m:
            total = int(m.group(1))
if total is None:
    # 回落：扫 retrain.py / finetune.py 的 argparse default（兼容
    # `--epochs=30` / `default=30` / `add_argument("--epochs", type=int, default=30)` 形态）
    for cand in ("retrain.py", "finetune.py"):
        p = os.path.join(ad, cand)
        if os.path.exists(p):
            txt = open(p, encoding="utf-8", errors="replace").read()
            m = re.search(r"--epochs\s*=\s*(\d+)", txt)
            if not m:
                m = re.search(r"--epochs\s*\(\s*\"[^\"]*\",\s*[^)]*?default\s*=\s*(\d+)", txt)
            if not m:
                m = re.search(r"--epochs[\"'\\s]+[^\n]*?default\s*=\s*(\d+)", txt, re.M)
            if m:
                total = int(m.group(1))
                break

epochs = set()
try:
    for ln in open(log, encoding="utf-8", errors="replace"):
        m = re.search(r"(?:epoch|step)[^0-9]*([0-9]+)", ln, re.IGNORECASE)
        if m:
            epochs.add(int(m.group(1)))
except FileNotFoundError:
    pass
cur = max(epochs) if epochs else 0

per_epoch = None
pid_path = os.path.join(ad, "runs", "retrain", ".retrain_pid")
if cur >= 2 and os.path.exists(pid_path):
    elapsed = time.time() - os.path.getmtime(pid_path)
    per_epoch = max(elapsed / cur, 1)

out = {
    "total_epochs": total or 0,
    "current_epoch": cur,
    "per_epoch_seconds": per_epoch,
    "eta_minutes": int((total - cur) * per_epoch / 60)
    if (total and cur is not None and per_epoch)
    else None,
}
with open(os.path.join(ad, "runs", "retrain", ".retrain_eta.json"), "w", encoding="utf-8") as f:
    json.dump(out, f)
print(json.dumps(out))
