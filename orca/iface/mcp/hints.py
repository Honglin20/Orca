"""hints.py —— ``_hint`` 按 status / 工具分支（SPEC phase-10 v4 §2.5）。

跨客户端通用引导：tool 返回值里的 ``_hint`` 字段是「傻瓜也能跟着调」的下一步指令。
按 SPEC §2.5 表逐字实现。**running 显式建议 Claude 结束 turn**，规避 CC 循环检测（草稿 §5.5）。

v4 变化（2026-07-07）+ in-session v5 §6.2 精简：
  - 删 ``needs_decision`` / ``after_resolve`` 分支（v4 删 resolve_gate，execute 永不中断）。
  - Discovery 组分支（list_workflows / describe_workflow）。
  - in-session v5 §6.2：删 setup_required / setup_outputs_mismatch / get_agent_prompt 分支
    （setup phase 全栈删除）。

为何把 hints 单独成模块（DRY）：tool 返回值在多个地方加 _hint，文案必须一致，
集中一处避免漂移。

依赖单向：本模块**零 Orca 依赖**（纯函数），任何模块可 import。
"""

from __future__ import annotations


# ── Lifecycle 组 ──────────────────────────────────────────────────────────────


def after_start(task_id: str) -> str:
    """start_workflow 返回的 _hint（SPEC §2.5 start_workflow 成功行）。"""
    return (
        f"Workflow started. Call get_task_status(task_id={task_id!r}) "
        "to poll progress."
    )


def by_status(status: str) -> str:
    """get_task_status 返回的 _hint（按 status 分支，SPEC §2.5 表）。

    v4 status 取值：running / completed / failed / cancelled / unknown（**无 needs_decision**，
    execute phase 永不中断）。
    """
    if status == "running":
        return (
            "Workflow still running. **End your turn**; the user can ask 'how "
            "is it going?' or you can poll again later."
        )
    if status == "completed":
        return "Workflow completed. Output is in the `output` field."
    if status == "failed":
        return "Workflow failed. Error is in the `error` field."
    if status == "cancelled":
        return "Workflow cancelled."
    # unknown / 其它：引导重新确认 task_id。
    return (
        "Unknown task_id. Verify the task_id from start_workflow's return value."
    )


def after_cancel(ok: bool) -> str:
    """cancel_task 返回的 _hint。

    ok=True：cancel 成功，引导确认 status。
    ok=False：已终态（completed/failed/cancelled），无法 cancel。
    """
    if ok:
        return "Cancellation requested. Call get_task_status to confirm status='cancelled'."
    return (
        "Run already in terminal state (completed/failed/cancelled); "
        "cannot cancel. Call get_task_status to see current state."
    )


def unknown_task() -> str:
    """未知 task_id 的 _hint（get_task_status / cancel_task / get_task_history 通用）。"""
    return (
        "Unknown task_id. Use list_workflows to discover workflows, "
        "or start_workflow to start a new one."
    )


# ── Discovery 组 ─────────────────────────────────────────────────────────────


def for_list_workflows() -> str:
    """list_workflows 返回的 _hint（SPEC §2.5）。"""
    return (
        "Pick a workflow. Call describe_workflow(name=...) for full input "
        "metadata, then ask the user for any missing inputs."
    )


def for_describe_workflow(
    *, inputs_complete: bool, name: str
) -> str:
    """describe_workflow 返回的 _hint（按 inputs 完整性分支，SPEC §2.5）。"""
    if inputs_complete:
        return f"Inputs complete. Call start_workflow(name={name!r}, inputs=...)."
    return (
        "Inputs incomplete. Ask the user for missing fields, "
        f"then start_workflow(name={name!r}, inputs=...)."
    )


def for_get_task_history() -> str:
    """get_task_history 返回的 _hint（SPEC §2.5）。"""
    return (
        "History shown (most recent events last). For current status, "
        "call get_task_status."
    )
