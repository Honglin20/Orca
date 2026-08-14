#!/usr/bin/env python3
"""emit_result.py —— pz_retrain 最终 JSON（**唯一产出节点 JSON 的时刻**）。

status 推导优先级（互斥，先命中先定）：
1. block_map.json 不存在 → failed（上游 pz_baseline 缺失——前置错误）
2. GKD **真正完成**（.retrain_rc==0 且进程已退出 且 ckpt 存在 + torch.load 可读）→ executed
3. 否则 → failed（尝试预算耗尽 / self-heal 耗尽；附 last attempt log tail）

max_retries_hit = 尝试次数 ≥3（从 .retrain_attempt 推导；前置失败未 launch → false）。

deterministic 部分从真实文件系统判；行为痕迹部分（healed_files / fidelity_retriggered /
assessment）从 marker 文件读。stdout 单行 JSON = 节点最终回复。
依赖：ORCA_ARTIFACTS_DIR（orca spawn / orca_env.sh 注入）。
"""

import glob
import json
import os
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


# Resolve final GKD ckpt path: status.sh 的 marker（确定性解析结果）优先，与 status.sh
# 共用同一解析（避免 status.sh 判定完成的 ckpt 与 emit_result.py artifacts 字段漂移）。
# marker 缺则回落契约路径 runs/retrain/final_model.pt（pz_retrain agent.md 生成时强制）。
ckpt_marker = os.path.join(ad, ".pz_retrain_ckpt_resolved.txt")
ckpt = read_text(ckpt_marker, "")
if not (ckpt and os.path.exists(ckpt)):
    ckpt = os.path.join(ad, "runs", "retrain", "final_model.pt")

block_map = os.path.join(ad, "block_map.json")
rc = read_text(os.path.join(ad, "runs", "retrain", ".retrain_rc"), "missing")
alive = pid_alive(os.path.join(ad, "runs", "retrain", ".retrain_pid"))

if not os.path.exists(block_map):
    status, artifacts, max_retries_hit = "failed", [], False
elif not alive and rc == "0" and os.path.exists(ckpt) and ckpt_valid(ckpt):
    status, artifacts, max_retries_hit = "executed", [ckpt], False
else:
    status, artifacts = "failed", []
    try:
        with open(os.path.join(ad, "runs", "retrain", ".retrain_attempt"), encoding="utf-8") as fh:
            attempt = int(fh.read().strip() or "0")
    except (FileNotFoundError, ValueError):
        attempt = 0
    max_retries_hit = attempt >= 3
    logs = sorted(glob.glob(os.path.join(ad, "runs", "retrain", "retrain.attempt*.log")))
    log_tail = tail(logs[-1]) if logs else ""
    if log_tail:
        prev = read_text(os.path.join(ad, ".pz_retrain_assessment.txt"), "")
        with open(os.path.join(ad, ".pz_retrain_assessment.txt"), "w", encoding="utf-8") as fh:
            fh.write((prev + "\n" if prev else "") + "last_error:\n" + log_tail)

healed_files = read_lines(os.path.join(ad, ".pz_retrain_healed.txt"))
fidelity_retriggered = read_text(os.path.join(ad, ".pz_retrain_fidelity.flag"), "false") == "true"
assessment = read_text(os.path.join(ad, ".pz_retrain_assessment.txt"), "no assessment recorded")

print(json.dumps({
    "status": status,
    "artifacts": artifacts,
    "assessment": assessment,
    "max_retries_hit": max_retries_hit,
    "healed_files": healed_files,
    "fidelity_retriggered": fidelity_retriggered,
}))
