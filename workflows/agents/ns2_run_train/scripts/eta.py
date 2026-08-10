#!/usr/bin/env python3
"""eta.py —— 估时（信息用，落 .train_eta.json，不决定 CRON 时机）。

从 log 抓当前 epoch 数 + run_train_supernet.sh 的 --epochs（或 search_config.yaml 的
epochs 字段）→ 每 epoch 耗时（pidfile mtime / 当前 epoch）→ 剩余分钟。stdout 单行 JSON。
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

logs = sorted(glob.glob(os.path.join(ad, "runs", "train", "train.attempt*.log")))
log = logs[-1] if logs else os.path.join(ad, "runs", "train", "train.attempt1.log")

total = None
sh = os.path.join(ad, "run_train_supernet.sh")
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
    cfg = os.path.join(ad, "search_config.yaml")
    if os.path.exists(cfg):
        for ln in open(cfg, encoding="utf-8", errors="replace"):
            m = re.search(r"^\s*epochs:\s*(\d+)", ln)
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
pid_path = os.path.join(ad, "runs", "train", ".train_pid")
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
with open(os.path.join(ad, "runs", "train", ".train_eta.json"), "w", encoding="utf-8") as f:
    json.dump(out, f)
print(json.dumps(out))
