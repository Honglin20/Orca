#!/usr/bin/env python3
"""emit_result.py —— pz_build_library 最终 JSON（**唯一产出节点 JSON 的时刻**）。

status 推导优先级（互斥，先命中先定）：
1. run_bld.sh 不存在 → skipped（viability self-gate）
2. BLD **真正完成**（.bld_rc==0 且进程已退出 且 ckpt 存在 + torch.load 可读）→ executed
3. 否则 → failed（尝试预算耗尽 / self-heal 耗尽；附 last attempt log tail）

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


# Resolve BLD ckpt path: status.sh 的 marker（确定性解析结果）优先，与 status.sh 共用
# 同一解析（避免 status.sh 判定完成的 ckpt 与 emit_result.py artifacts 字段漂移）。
# marker 缺则回落契约路径 runs/bld/bld_complete.pt（pz_build_library agent.md 生成时强制）。
ckpt_marker = os.path.join(ad, ".pz_build_library_ckpt_resolved.txt")
ckpt = read_text(ckpt_marker, "")
if not (ckpt and os.path.exists(ckpt)):
    ckpt = os.path.join(ad, "runs", "bld", "bld_complete.pt")

script_path = os.path.join(ad, "run_bld.sh")
rc = read_text(os.path.join(ad, "runs", "bld", ".bld_rc"), "missing")
# 进程存活排除（与 status.sh 一致）：前次 attempt 可能留 rc=0（续训场景），在跑进程时必须
# 视为未完成，防 stale rc 误判提前 executed。
alive = pid_alive(os.path.join(ad, "runs", "bld", ".bld_pid"))

if not os.path.exists(script_path):
    status, artifacts, max_retries_hit = "skipped", [], False
elif not alive and rc == "0" and os.path.exists(ckpt) and ckpt_valid(ckpt):
    status, artifacts, max_retries_hit = "executed", [ckpt], False
else:
    status, artifacts, max_retries_hit = "failed", [], True
    logs = sorted(glob.glob(os.path.join(ad, "runs", "bld", "bld.attempt*.log")))
    log_tail = tail(logs[-1]) if logs else ""
    if log_tail:
        prev = read_text(os.path.join(ad, ".pz_build_library_assessment.txt"), "")
        with open(os.path.join(ad, ".pz_build_library_assessment.txt"), "w", encoding="utf-8") as fh:
            fh.write((prev + "\n" if prev else "") + "last_error:\n" + log_tail)

healed_files = read_lines(os.path.join(ad, ".pz_build_library_healed.txt"))
fidelity_retriggered = read_text(os.path.join(ad, ".pz_build_library_fidelity.flag"), "false") == "true"
assessment = read_text(os.path.join(ad, ".pz_build_library_assessment.txt"), "no assessment recorded")

print(json.dumps({
    "status": status,
    "artifacts": artifacts,
    "assessment": assessment,
    "max_retries_hit": max_retries_hit,
    "healed_files": healed_files,
    "fidelity_retriggered": fidelity_retriggered,
}))
