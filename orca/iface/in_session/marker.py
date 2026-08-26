"""marker.py —— in-session shell 的激活标记（SPEC v3 §7.2，m11 精简）。

回答「宿主怎么知道本 session 当前有没有活跃 Orca run？run_id / model / 合规计数
怎么跨 per-call CLI 传递？」：靠**一个 run 作用域的 JSON 标记文件**
（`<rundir>/orca-<run_id>.json`），CLI ``bootstrap`` 写、``next`` 读改、``stop`` 清。

v3 精简（§7.2，删 desync 向量）：
  - **只留 ``{run_id, model, no_output_count}``**。删 ``tape_path`` / ``yaml`` /
    ``session_id`` / ``owner``——run_id 可派生 tape_path（``<rundir>/<run_id>.jsonl``），
    yaml 运行时从 tape 的 ``workflow_started.data.workflow_name`` 反查 catalog；留着这些
    字段只会制造多真相源（tape 被移 → marker 悬空 → desync）。
  - **文件名固定 ``orca-<run_id>.json``**：``next``/``stop`` 用 ``marker_path(rundir, run_id)``
    O(1) 定位（删 ``find_marker_by_run_id`` 扫描）。
  - **原子写**（F13）：``write(tmp) + os.replace(tmp, final)``；读容忍半写
    （``try/except JSONDecodeError → warn + None``，半写态不崩）。
  - **RMW 在 tape flock 临界区内**（N2）：marker 无独立锁，``next`` 持 tape flock 期间
    读改写 marker，串行化保证 ``no_output_count`` 不丢更新。

并发安全：run_id 全局唯一 → 每 run 独立 marker+tape，多 session / 一 session 多 wf 天然隔离。

依赖单向：仅依赖标准库，不反向调 run/events/schema。
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
    """一条激活标记（run_id / model / 合规计数）。

    v3 §7.2：仅这 3 字段。``tape_path`` / ``yaml`` / ``session_id`` / ``owner`` 已删——
    它们是 desync 向量（run_id 是唯一句柄，其余运行时派生）。``no_output_count`` 是
    subagent 合规计数（D-v7-6，F11 闭环）：连续 N 次 next 无 output → workflow_failed。
    """

    run_id: str
    model: str | None = None
    no_output_count: int = 0


def marker_path(rundir: Path | str, run_id: str) -> Path:
    """标记文件路径：``<rundir>/orca-<run_id>.json``（v3 §7.2 固定命名，O(1) 定位）。

    ``run_id`` 是唯一句柄：bootstrap 用它建 marker，next/stop 据它 O(1) 直定位（不再扫描）。
    run_id 含 ``-``/字母数字，安全作文件名；非字母数字字符替换为 ``_`` 兜底（防御性）。
    """
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in run_id)
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

    返 None 表示「无标记 / 半写 / 损坏」——调用方按「本 run 无活跃 marker」处理
    （passthrough），不崩。字段多余/缺失容忍：只取 ``ActivationMarker`` 声明的 3 字段，
    忽略历史残留（旧版 marker 含 tape_path/yaml 等），避免加字段即破读。
    """
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        # 半写态 / 损坏：warn + passthrough（SPEC §2.4 容忍半写）
        logger.warning("激活标记 %s 读失败（可能半写）：%s", path, e)
        return None
    if not isinstance(obj, dict):
        logger.warning("激活标记 %s 顶层非 object：%r", path, type(obj).__name__)
        return None
    # 只取已知字段（容忍历史残留 / 字段缺失）。缺 run_id → 视为损坏。
    run_id = obj.get("run_id")
    if not run_id:
        logger.warning("激活标记 %s 缺 run_id 字段", path)
        return None
    try:
        return ActivationMarker(
            run_id=str(run_id),
            model=obj.get("model"),
            no_output_count=int(obj.get("no_output_count", 0)),
        )
    except (TypeError, ValueError) as e:
        logger.warning("激活标记 %s 字段类型异常：%s", path, e)
        return None


def clear_marker(path: Path) -> None:
    """清标记（``stop`` / workflow 终态后调用）。幂等：不存在不报错。"""
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("清激活标记 %s 失败：%s", path, e)
