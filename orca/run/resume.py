"""resume.py —— Checkpoint Resume（崩溃续跑，SPEC §7 / phase-11-cli-enrichment）。

回答「workflow 跑到一半崩了怎么续？」：Orca 的 Tape 天然就是 checkpoint
（append-only JSONL 记录了全部历史）。``Orchestrator.from_tape`` 重放 Tape 到崩溃前
状态，``run_from_state`` emit ``workflow_resumed`` 后从崩溃点继续 drive loop。

设计（SPEC §1.4 / §7.1 Orca 优势）：
  - **Tape 是唯一 checkpoint**：不另起状态序列化系统（反 Conductor ``checkpoint.py``
    400+ 行独立系统）。``replay_state`` 已是纯 reducer fold，复用即可。
  - **fail loud**：每个失败模式对应一个 typed exception，CLI 层映射到明确 exit code。
  - **fail-soft 末尾残行**：崩溃时写一半的 JSON 行由 ``Tape(resume=True)`` 截断
    （warning 可见，不阻断 resume —— SPEC §7.3 review C6）。
  - **parallel group 中断不支持**：crash 在 parallel 组中间时拒绝 resume（exit 1），
    避免部分 branch 已跑、部分没跑的歧义状态被静默续跑（SPEC §7 risk / 计划 P2 简化）。

本模块只放 typed exceptions + 纯辅助函数（中段损坏检测 / outputs aggregate 重建 /
parallel mid-crash 检测）。``Orchestrator.from_tape`` / ``run_from_state`` 在
``orchestrator.py``（复用 drive loop，DRY）。

依赖单向：本模块依赖 ``orca.{schema, events}``（纯辅助，不依赖 orchestrator / iface）。
``orchestrator.py`` import 本模块的 exception / 辅助函数（单向，无环）。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from orca.schema import Event

if TYPE_CHECKING:
    from pathlib import Path

    from orca.schema import RunState, Workflow

logger = logging.getLogger(__name__)


# ── typed exceptions（CLI 层映射到 exit code，SPEC §7.3）──────────────────────


class ResumeError(Exception):
    """resume 失败的基类。子类对应不同 exit code。"""


class TapeNotFoundError(ResumeError):
    """Tape 文件不存在（CLI exit 2）。"""

    def __init__(self, path: "Path") -> None:
        super().__init__(f"Tape 文件不存在：{path}")
        self.path = path


class EmptyTapeError(ResumeError):
    """Tape 为空（0 字节 / 无事件），无状态可恢复（CLI exit 2）。"""

    def __init__(self, path: "Path") -> None:
        super().__init__(f"Tape 为空（无事件），无状态可恢复：{path}")
        self.path = path


class AlreadyCompletedError(ResumeError):
    """Tape 已是 workflow_completed 终态，无需 resume（CLI exit 0，非错误）。"""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"workflow 已完成（run_id={run_id}），无需 resume")
        self.run_id = run_id


class ParallelGroupMidCrashError(ResumeError):
    """崩溃点在 parallel group 中间（部分 branch 未完成），resume 不支持。

    SPEC §7 risk / 计划 P2 简化：parallel 组要求 atomic（全跑完才推进），中途崩溃
    状态歧义（哪些 branch 已跑、输出是否一致），phase 11 不支持 mid-group resume。
    CLI exit 1，提示用户手动处理。
    """

    def __init__(self, group_name: str, running_branches: list[str]) -> None:
        super().__init__(
            f"崩溃点在 parallel 组 {group_name!r} 中间（branch 未完成："
            f"{running_branches}），phase 11 不支持 mid-group resume。"
            "请手动检查 Tape 或重跑该 workflow。"
        )
        self.group_name = group_name
        self.running_branches = running_branches


class MidFileCorruptError(ResumeError):
    """Tape 中段损坏（合法行后跟不可解析行），无法安全重放（CLI exit 2）。"""

    def __init__(self, path: "Path", first_bad_lineno: int, line_preview: str) -> None:
        super().__init__(
            f"Tape {path} 第 {first_bad_lineno} 行不是合法 Event，"
            f"中段损坏无法 resume。行预览：{line_preview!r}"
        )
        self.path = path
        self.first_bad_lineno = first_bad_lineno
        self.line_preview = line_preview


# ── 辅助：扫描 Tape 找首个不可解析行的位置（中段损坏检测）─────────────────────


def _find_first_corrupt_line(
    tape_path: "Path",
) -> tuple[tuple[int, str] | None, int]:
    """扫描 Tape，返回 ``(corrupt_info, valid_event_count)``。

    - ``corrupt_info``：首个**中段**不可解析行的 ``(lineno, line_preview)``；全合法/仅
      末尾残行 → None。
    - ``valid_event_count``：合法事件行数（含末尾残行前的全部合法行）。

    与 ``Tape.replay`` 的 fail-soft 不同：replay 跳过坏行继续读（适合 live 增量），
    resume 场景必须**严格**——中段损坏意味着状态可能丢失，绝不能静默跳过（SPEC §7.3
    「合法行后跟乱码行 → exit 2」）。

    **末尾残行不算 corrupt**（SPEC §7.3 fail-soft，review C6）：崩溃时写一半的最后一行
    由 ``Tape(resume=True)`` 截断。即便调用方忘了用 resume=True，本函数也不把末尾残行
    报为 MidFileCorruptError —— 只报「合法行后跟乱码行」的真中段损坏。这让 from_tape
    对「tape 是否已 resume 截断」不敏感（鲁棒性，review §鲁棒性 建议）。

    返回 valid_event_count 让 from_tape 复用本次扫描结果作为 ``replayed_events`` 计数，
    避免 from_tape 再多读一遍 tape（review §冗余：原实现 3x 读 tape）。
    """
    if not tape_path.exists():
        return None, 0
    # 先读全部非空行（带行号），判断「最后一行是否合法」。
    lines: list[tuple[int, str]] = []
    with open(tape_path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            stripped = raw.strip()
            if stripped:
                lines.append((lineno, stripped))
    if not lines:
        return None, 0
    last_lineno, last_line = lines[-1]
    last_is_valid = _is_valid_event_line(last_line)
    # 扫描除最后一行外的所有行：任一不合法 → 中段 corrupt。
    # 若最后一行不合法（末尾残行），扫描到倒数第二行为止；若最后一行合法，扫全部。
    scan_limit = len(lines) - 1 if not last_is_valid else len(lines)
    valid_count = 0
    for lineno, stripped in lines[:scan_limit]:
        if _is_valid_event_line(stripped):
            valid_count += 1
        else:
            return (lineno, stripped[:80]), valid_count
    # 末尾残行前的最后一行若合法，已计入 valid_count（scan_limit 含它）；末尾残行本身不计。
    return None, valid_count


def _is_valid_event_line(stripped: str) -> bool:
    """单行是否可解析为合法 Event（JSON + Event schema 校验通过）。"""
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    try:
        Event(**obj)
        return True
    except Exception:
        return False


# ── resume orchestration 辅助（纯函数，由 Orchestrator.from_tape 调用）────────


def _outputs_acc_from_state(state: "RunState") -> dict[str, dict]:
    """从 ``replay_state`` 的 context 重建 ``_drive_loop`` 的 ``outputs_acc`` 形状。

    ``state.context[node]`` = raw output（reducer 直接存 ``node_completed.data.output``）；
    ``_drive_loop`` 的 ``outputs_acc[node]`` = ``{"output": raw}`` 包装（render._namespace
    约定，模板统一 ``{{ node.output.field }}``）。故此处对每个 done node 包一层壳。
    """
    return {node: {"output": raw} for node, raw in state.context.items()}


def _detect_parallel_mid_crash(
    state: "RunState", wf: "Workflow"
) -> ParallelGroupMidCrashError | None:
    """检测崩溃点是否在 parallel 组中间。

    信号：某个 parallel 组的 branch 在 ``node_status`` 里是 ``"running"``（崩溃时
    started 但未 completed/failed）。也覆盖 ``current_node`` 指向 parallel 组名且
    组内有未完成 branch 的情况。
    """
    parallel_by_name = {g.name: g for g in wf.parallel}
    # 1) 任一 parallel 组的 branch 处于 running（started 未终结）
    for group in wf.parallel:
        running = [
            b for b in group.branches if state.node_status.get(b) == "running"
        ]
        if running:
            return ParallelGroupMidCrashError(group.name, running)
    # 2) current_node 指向 parallel 组名 → 组刚 dispatch 未完成（crash 在 gather 中）
    if state.current_node and state.current_node in parallel_by_name:
        group = parallel_by_name[state.current_node]
        running = [
            b for b in group.branches if state.node_status.get(b) != "done"
        ]
        if running:
            return ParallelGroupMidCrashError(group.name, running)
    return None
