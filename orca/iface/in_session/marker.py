"""marker.py —— in-session shell 的激活标记（SPEC §5 / §2.4）。

回答「宿主怎么知道本 session 当前有没有活跃 Orca run？以及 run_id / tape_path /
yaml / model 怎么跨 hook/plugin 调用传递？」：靠**一个 session/run 作用域的 JSON
标记文件**（`<rundir>/orca-<key>.json`），CLI ``bootstrap`` 写、``next`` 读改、
``stop`` 清，hook/plugin 透传。

设计（SPEC §5 + spec-review r2 N1/N2 + F13/F14）：
  - **owner key**：opencode = sessionID（plugin 据此过滤子 session idle，D-v7-5）；
    CC = run_id（CC hook 脚本不感知 sessionID，以 run_id 为锚）。``bootstrap`` 的
    ``--owner`` 决定文件名，默认 = run_id（CC 路兼容）。
  - **realpath canonical**（N1）：bootstrap 的幂等键 = ``(owner, os.path.realpath(yaml))``；
    同 owner + 同 realpath(yaml) 视为同一 run，复用 run_id 不重发 ``workflow_started``。
  - **原子写**（F13）：``write(tmp) + os.replace(tmp, final)``；读容忍半写
    （``try/except JSONDecodeError → warn + None``，半写态不崩）。
  - **RMW 在 tape flock 临界区内**（N2）：marker 无独立锁，``next`` 持 tape flock 期间
    读改写 marker，串行化保证 ``no_output_count`` 不丢更新。
  - **字段**：``{run_id, tape_path, yaml, model, session_id, no_output_count}``。

依赖单向：仅依赖标准库 + ``orca.schema``（无），不反向调 run/events。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ActivationMarker:
    """一条激活标记（run_id / tape_path / yaml canonical / model / session_id / 合规计数）。

    ``yaml`` / ``tape_path`` 存 **canonical realpath** 字符串（N1：软链 / 相对路径
    归一，幂等键稳定）。``no_output_count`` 是 subagent 合规计数（D-v7-6，F11 闭环）。
    """

    run_id: str
    tape_path: str          # canonical realpath
    yaml: str               # canonical realpath
    owner: str              # 文件名 key（opencode=sessionID / CC=run_id）
    model: str | None = None
    session_id: str | None = None
    no_output_count: int = 0


def marker_path(rundir: Path | str, owner: str) -> Path:
    """标记文件路径：``<rundir>/orca-<owner>.json``。

    ``owner`` 由调用方决定：opencode 传 sessionID（plugin 据此过滤子 session idle），
    CC 传 run_id（hook 脚本不感知 sessionID）。
    """
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in owner)
    return Path(rundir) / f"orca-{safe}.json"


def write_marker(path: Path, marker: ActivationMarker) -> None:
    """原子写标记（F13）：write tmp + ``os.replace``。

    ``path.parent`` 必须已存在（调用方 ``mkdir(parents=True, exist_ok=True)``）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    payload = asdict(marker)
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_marker(path: Path) -> ActivationMarker | None:
    """读标记；容忍半写（F13）：try/except JSONDecodeError → warn + 返 None。

    返 None 表示「无标记 / 半写 / 损坏」——调用方按「本 session 无活跃 run」处理
    （passthrough），不崩。
    """
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        # 半写态 / 损坏：warn + passthrough（SPEC §2.4 容忍半写）
        logger.warning("激活标记 %s 读失败（可能半写）：%s", path, e)
        return None
    try:
        return ActivationMarker(**obj)
    except TypeError as e:
        # 字段缺失（旧版标记 / 手改坏）：warn + passthrough
        logger.warning("激活标记 %s 字段不匹配：%s", path, e)
        return None


def find_marker_by_run_id(rundir: Path | str, run_id: str) -> Path | None:
    """在 ``rundir`` 内线性扫 ``orca-*.json``，返回第一条 ``run_id`` 匹配的标记路径。

    用于 ``next``/``stop`` 仅凭 ``--run-id`` 定位标记（SPEC §2.1 next 契约只含
    ``--tape --run-id``，无 marker key）。一个 run 通常只有一个标记，扫描成本 O(1)。

    返 None 表示无匹配（调用方降级为「无标记合规计数」/ warn，不崩）。
    """
    base = Path(rundir)
    if not base.exists():
        return None
    for p in sorted(base.glob("orca-*.json")):
        m = read_marker(p)
        if m is not None and m.run_id == run_id:
            return p
    return None


def clear_marker(path: Path) -> None:
    """清标记（``stop`` / workflow 终态后调用）。幂等：不存在不报错。"""
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("清激活标记 %s 失败：%s", path, e)
