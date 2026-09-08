#!/usr/bin/env python3
"""emit_result.py —— ns_retrain 最终 JSON（**唯一产出节点 JSON 的时刻**）。

status 推导优先级（互斥，先命中先定）：
1. retrain.py 或 run_retrain.sh 不存在 → failed（上游 ns3_retrain_script 未产出脚本——前置错误）
2. 训练**真正完成**（.retrain_rc==0 且训练进程已退出 且 ckpt 存在 + torch.load 可读）→ executed
3. 否则 → failed（尝试预算耗尽 / 训练逻辑或禁碰-blocked fail loud；
   附 last attempt log tail）

max_retries_hit = 尝试次数 ≥3（从 .retrain_attempt 推导；前置失败未 launch → false，
与 yaml"true=3 次尝试耗尽仍失败"语义一致）。

deterministic 部分从真实文件系统判；healed_files / assessment 从 marker 文件读。
fidelity_retriggered 恒 false（执行节点不重触 fidelity——归上游 ns3_retrain_script）。
stdout 单行 JSON = 节点最终回复。
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


# Resolve final retrain ckpt path: status.sh 的 marker（确定性解析结果）优先，与 status.sh
# 共用同一解析（避免 status.sh 判定完成的 ckpt 与 emit_result.py artifacts 字段漂移）。
# marker 缺则回落契约路径 runs/retrain/retrain_best.pth（agent.md Step 2 生成时强制）。
ckpt_marker = os.path.join(ad, ".ns_retrain_ckpt_resolved.txt")
ckpt = read_text(ckpt_marker, "")
if not (ckpt and os.path.exists(ckpt)):
    ckpt = os.path.join(ad, "runs", "retrain", "retrain_best.pth")

retrain_py = os.path.join(ad, "retrain.py")
run_retrain_sh = os.path.join(ad, "run_retrain.sh")
rc = read_text(os.path.join(ad, "runs", "retrain", ".retrain_rc"), "missing")
# 进程存活排除（与 status.sh 一致）：前次 attempt 可能留 rc=0（续训场景），在跑进程时必须
# 视为未完成，防 stale rc 误判提前 executed。
alive = pid_alive(os.path.join(ad, "runs", "retrain", ".retrain_pid"))

if not (os.path.exists(retrain_py) and os.path.getsize(retrain_py) > 0
        and os.path.exists(run_retrain_sh) and os.path.getsize(run_retrain_sh) > 0):
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
        prev = read_text(os.path.join(ad, ".ns_retrain_assessment.txt"), "")
        with open(os.path.join(ad, ".ns_retrain_assessment.txt"), "w", encoding="utf-8") as fh:
            fh.write((prev + "\n" if prev else "") + "last_error:\n" + log_tail)

healed_files = read_lines(os.path.join(ad, ".ns_retrain_exec_healed.txt"))
# 执行节点不重触 fidelity（归上游 ns3_retrain_script）；此字段恒 false。
fidelity_retriggered = False
assessment = read_text(os.path.join(ad, ".ns_retrain_assessment.txt"), "no assessment recorded")

print(json.dumps({
    "status": status,
    "artifacts": artifacts,
    "assessment": assessment,
    "max_retries_hit": max_retries_hit,
    "healed_files": healed_files,
    "fidelity_retriggered": fidelity_retriggered,
}))
