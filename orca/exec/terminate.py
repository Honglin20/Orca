"""terminate.py —— TerminateExecutor（显式工作流终止节点）。

回答「触达 terminate node 时 executor 做什么？」：渲染 ``reason`` + ``outputs``，
emit ``node_completed``，**不**判断终态。终态事件（``workflow_completed`` /
``workflow_failed{WorkflowTerminated}``）由 orchestrator 在 ``_drive_from`` 里看到
``kind=terminate`` 时分发（依赖单向铁律：executor 不 emit workflow 级事件）。

执行流程（仿 ``set_node.py`` 模板）：
  1. ``session_id = uuid4().hex``
  2. ``yield node_started({"kind": "terminate"})``
  3. 渲染 ``reason`` + ``outputs``（任一失败 → ExecError(phase="render")）
  4. ``yield node_completed({"status": ..., "reason": ..., "outputs": ...})``

关键约束：
  - executor 自身**不**据 ``status`` 决定 workflow 终态。它只 emit ``node_completed``
    （按 Executor 标准契约），让 orchestrator 据此分发。
  - ``status="failed"`` 在 executor 视角下**不是失败**——它是 terminate 节点的业务声明，
    executor 正常完成（渲染成功就 node_completed）。render 失败才是 executor 失败
    （走 ExecError → node_failed + error 双发，fail loud）。

依赖单向：本模块依赖 ``orca.exec.{interface,context,error,render}`` + ``orca.schema``；
不依赖 events.bus / run / compile（铁律）。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from orca.exec.context import RunContext
from orca.exec.error import ExecError
from orca.exec.interface import Executor
from orca.exec.render import render_template
from orca.schema import Event, TerminateNode


class TerminateExecutor(Executor):
    """渲染 terminate 节点的 reason/outputs 并 emit node_completed。

    不判断终态——orchestrator 据 ``node_completed.data.status`` 分发 workflow 级事件。
    """

    async def exec(self, node: TerminateNode, ctx: RunContext) -> AsyncIterator[Event]:
        session_id = uuid.uuid4().hex
        start = time.monotonic()

        def _ev(event_type: str, data: dict[str, Any]) -> Event:
            return Event(
                seq=0,  # 占位：orchestrator 在 tape.append 时重分配（与 set_node 同约定）
                type=event_type,  # type: ignore[arg-type]
                timestamp=time.time(),
                node=node.name,
                session_id=session_id,
                data=data,
            )

        yield _ev("node_started", {"kind": "terminate", "status": node.status})

        try:
            # 渲染 reason（空串直通，无 Jinja2 求值开销 / 也兼容纯字面量）
            reason = render_template(node.reason, ctx) if node.reason else ""
            # 逐 key 渲染 outputs（与 SetExecutor 同形：任一失败 → ExecError(phase=render)）
            outputs: dict[str, Any] = {}
            for key, template in node.outputs.items():
                outputs[key] = render_template(template, ctx)

            elapsed = time.monotonic() - start
            yield _ev(
                "node_completed",
                {
                    "output": {  # 兼容 reducer / ctx.outputs 的 ``{"output": raw}`` 约定
                        "status": node.status,
                        "reason": reason,
                        "outputs": outputs,
                    },
                    "elapsed": elapsed,
                    # 顶层冗余字段：orchestrator 直接读 data.status / data.reason / data.outputs，
                    # 无需穿透 .output 二级。reducer 不消费这些字段（terminate 终态由 orchestrator
                    # 直接 emit workflow_completed/failed，不进 ctx.outputs 推进路径）。
                    "status": node.status,
                    "reason": reason,
                    "outputs": outputs,
                },
            )

        except ExecError as e:
            elapsed = time.monotonic() - start
            err_data = {"error_type": e.error_type, "message": e.message, "phase": e.phase}
            yield _ev("node_failed", err_data)
            yield _ev("error", err_data)
