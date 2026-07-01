"""hints.py —— ``_hint`` 按 status 分支（SPEC phase-10 §2.3）。

跨客户端通用引导：tool 返回值里的 ``_hint`` 字段是「傻瓜也能跟着调」的下一步指令。
按 SPEC §2.3 表逐字实现。**running 显式建议 Claude 结束 turn**，规避 CC 循环检测（草稿 §5.5）。

为何把 hints 单独成模块（DRY）：tool 返回值在多个地方加 _hint（start_workflow /
get_task_status / resolve_gate / cancel_task），文案必须一致，集中一处避免漂移。

依赖单向：本模块**零 Orca 依赖**（纯函数），任何模块可 import。
"""

from __future__ import annotations


def after_start(task_id: str) -> str:
    """start_workflow 返回的 _hint（SPEC §2.3 running 行）。"""
    return (
        f"Workflow started in background. Call get_task_status(task_id={task_id!r}) "
        "to poll progress."
    )


def by_status(status: str) -> str:
    """get_task_status 返回的 _hint（按 status 分支，SPEC §2.3 表）。

    status 取值：running / needs_decision / completed / failed / cancelled / unknown。
    """
    if status == "running":
        return (
            "Workflow still running. End your turn; the user can ask 'how is it "
            "going?' or you can poll again later."
        )
    if status == "needs_decision":
        return (
            "Gate awaiting human decision. Ask the user, then call "
            "resolve_gate(task_id=..., gate_id=..., decision=...)."
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


def after_resolve(ok: bool) -> str:
    """resolve_gate 返回的 _hint（SPEC §2.3 resolve_gate 行）。"""
    if ok:
        return "Decision accepted. Call get_task_status to continue polling."
    return "Gate already resolved by another channel. Call get_task_status to see current state."


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
    """未知 task_id 的 _hint（get_task_status / resolve_gate 通用）。"""
    return (
        "Unknown task_id. Use get_task_status to list known runs, "
        "or start_workflow to start a new one."
    )
