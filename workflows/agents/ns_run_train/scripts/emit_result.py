#!/usr/bin/env python3
"""emit_result.py —— ns_run_train 最终 JSON（**唯一产出节点 JSON 的时刻**）。

status 推导优先级（互斥，先命中先定）：
1. run_train_supernet.sh 不存在 → skipped（viability self-gate）
2. 训练**真正完成**（.train_rc==0 且训练进程已退出 且 ckpt 存在 + torch.load 可读）→ executed
3. 否则 → failed（尝试预算耗尽 / self-heal 耗尽；附 last attempt log tail）

deterministic 部分从真实文件系统判；行为痕迹部分（healed_files / fidelity_retriggered /
assessment）从 marker 文件读。stdout 单行 JSON = 节点最终回复。
依赖：ORCA_ARTIFACTS_DIR（orca spawn / orca_env.sh 注入）。
"""

import glob
import json
import os
import re
import sys

ad = os.environ.get("ORCA_ARTIFACTS_DIR")
if not ad:
    print(json.dumps({"status": "failed", "artifacts": [], "assessment": "ORCA_ARTIFACTS_DIR unset",
                      "max_retries_hit": True, "healed_files": [], "fidelity_retriggered": False}))
    sys.exit(0)


def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except FileNotFoundError:
        return default


def read_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [ln.strip() for ln in f if ln.strip()]
    except FileNotFoundError:
        return []


def tail(path, n=20):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return "\n".join(lines[-n:])
    except FileNotFoundError:
        return ""


def ckpt_valid(path):
    try:
        import torch
        sd = torch.load(path, map_location="cpu", weights_only=False)
        state = sd.get("state_dict", sd) if isinstance(sd, dict) else sd
        return bool(state)
    except Exception:
        return False


def pid_alive(pid_path):
    try:
        with open(pid_path, "r", encoding="utf-8", errors="replace") as fh:
            pid = int(fh.read().strip())
    except (FileNotFoundError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# Resolve supernet ckpt path: status.sh 的 marker（确定性解析结果）优先，与 status.sh 共用
# 同一解析（避免 status.sh 判定完成的 ckpt 与 emit_result.py artifacts 字段漂移）。
# marker 缺则回落到 search_config.yaml::supernet_ckpt_path，再回落默认 runs/train/supernet_best.pth。
ckpt_marker = os.path.join(ad, ".ns_run_train_ckpt_resolved.txt")
ckpt = read_text(ckpt_marker, "")
if not (ckpt and os.path.exists(ckpt)):
    ckpt_rel = "runs/train/supernet_best.pth"
    cfg_path = os.path.join(ad, "search_config.yaml")
    try:
        with open(cfg_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.search(r'supernet_ckpt_path:\s*"?([^\s"#]+)"?', line)
                if m:
                    ckpt_rel = m.group(1)
                    break
    except FileNotFoundError:
        pass
    ckpt = ckpt_rel if os.path.isabs(ckpt_rel) else os.path.join(ad, ckpt_rel)

script_path = os.path.join(ad, "run_train_supernet.sh")
rc = read_text(os.path.join(ad, "runs", "train", ".train_rc"), "missing")
# 进程存活排除（与 status.sh 一致）：前次 attempt 可能留 rc=0（续训场景），在跑进程时必须
# 视为未完成，防 stale rc 误判提前 executed。
alive = pid_alive(os.path.join(ad, "runs", "train", ".train_pid"))

if not os.path.exists(script_path):
    status, artifacts, max_retries_hit = "skipped", [], False
elif not alive and rc == "0" and os.path.exists(ckpt) and ckpt_valid(ckpt):
    status, artifacts, max_retries_hit = "executed", [ckpt], False
else:
    status, artifacts, max_retries_hit = "failed", [], True
    logs = sorted(glob.glob(os.path.join(ad, "runs", "train", "train.attempt*.log")))
    log_tail = tail(logs[-1]) if logs else ""
    if log_tail:
        prev = read_text(os.path.join(ad, ".ns_run_train_assessment.txt"), "")
        with open(os.path.join(ad, ".ns_run_train_assessment.txt"), "w", encoding="utf-8") as fh:
            fh.write((prev + "\n" if prev else "") + "last_error:\n" + log_tail)

healed_files = read_lines(os.path.join(ad, ".ns_run_train_healed.txt"))
fidelity_retriggered = read_text(os.path.join(ad, ".ns_run_train_fidelity.flag"), "false") == "true"
assessment = read_text(os.path.join(ad, ".ns_run_train_assessment.txt"), "no assessment recorded")

print(json.dumps({
    "status": status,
    "artifacts": artifacts,
    "assessment": assessment,
    "max_retries_hit": max_retries_hit,
    "healed_files": healed_files,
    "fidelity_retriggered": fidelity_retriggered,
}))
