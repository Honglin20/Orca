"""executor_adapter.py —— 把 executor.exec() 的 AsyncIterator[Event] 桥接到 bus.emit。

回答「executor 产出的事件流如何落 Tape？」：逐个 ``await bus.emit(type, data, node,
session_id)``（**bus.emit 吃拆解后的字段，不是预构造的 Event** —— seq 由 tape.append
内部分配，executor 产出的占位 seq=0 会被覆盖）。

桥接规则（SPEC §4.3 / 计划 R3）：
  - 对 executor yield 的每个 Event：拆成 ``(event.type, event.data, event.node,
    event.session_id)`` 调 ``bus.emit``。run/ 是唯一 ``bus.emit`` 写 Tape 处（铁律 1）。
  - ``node_completed``：从 data 取 output，记为本次返回值。
  - ``node_failed``：raise ``ExecError``（透传 executor 已构造的 phase / error_type），
    触发上层 orchestrator 的 workflow_failed 分支。

返回值约定：``execute_and_emit`` 返回 **raw output**（未 ``{"output": raw}`` 包装）；
包装由 orchestrator 在 ``ctx.outputs[current]`` 时做（render 模板约定，见 render.py
``_namespace``：``{{ node.output.field }}`` 从 ``outputs[node]["output"]`` 取）。

依赖单向：本模块依赖 ``orca.exec.{interface,error}``（Executor 类型 / ExecError）+
``orca.events.bus``（EventBus）；不依赖 schema.workflow（node 是基类，泛型）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from orca.exec.error import ExecError
from orca.exec.interface import Executor

if TYPE_CHECKING:
    from orca.events.bus import EventBus
    from orca.exec.context import RunContext
    from orca.schema import Event, Node

logger = logging.getLogger(__name__)


async def execute_and_emit(
    executor: Executor,
    node: Node,
    ctx: RunContext,
    bus: EventBus,
) -> Any:
    """跑 executor.exec() 的事件流，逐个 bus.emit 落 Tape，返回 raw output。

    Args:
        executor: ``make_executor(node)`` 产出的 executor 实例。
        node: 被执行的 node（透传给 executor.exec）。
        ctx: 当前 RunContext（执行快照）。
        bus: EventBus（唯一写 Tape 入口）。

    Returns:
        raw output（executor 在 ``node_completed.data["output"]`` 给出的值；
        未 ``{"output": raw}`` 包装 —— orchestrator 在累加 ctx.outputs 时包）。

    Raises:
        ExecError: executor yield 了 ``node_failed``（透传其 phase / error_type / message）。
    """
    output: Any = None
    seen_completed = False
    async for event in executor.exec(node, ctx):
        # bus.emit 吃拆解后的字段（type, data, node, session_id），seq 由 tape.append 重分配。
        # executor 产出的 Event.seq=0 是占位（见 script.py / set_node.py / claude/executor.py）。
        await bus.emit(
            event.type,
            event.data,
            node=event.node,
            session_id=event.session_id,
        )
        if event.type == "node_completed":
            output = event.data.get("output")
            seen_completed = True
        elif event.type == "node_failed":
            # fail loud：透传 executor 已构造的 error_type / phase / message + node 名。
            # 构造走 ExecError.from_failed_data（DRY：与 run.retry.execute_with_retry 共享）。
            raise ExecError.from_failed_data(
                event.data, node=getattr(node, "name", None),  # 注入失败 node 名（SPEC §3.4）
            )

    if not seen_completed:
        # executor 既没 node_completed 也没 node_failed（违约生命周期，见 interface.py 契约）
        # —— fail loud（不应发生，记 error + raise）。
        logger.error(
            "executor 未按生命周期契约产出 node_completed/node_failed（node=%s）",
            getattr(node, "name", "?"),
        )
        raise ExecError(
            phase="node_failed",
            message=(
                f"executor 执行 node {getattr(node, 'name', '?')!r} 未产出 "
                "node_completed（生命周期违约）"
            ),
            node=getattr(node, "name", None),
        )
    return output
