"""set_node.py —— SetExecutor（纯计算存值节点，SPEC §4.6）。

回答「怎么求值一个 set node？」：Jinja2 渲染每个 value，存入 output dict。

执行流程（SPEC §4.6 / 计划 D.2）：
  1. ``session_id = uuid4().hex``
  2. ``yield node_started``
  3. ``output = {k: render_template(v, ctx) for k, v in node.values.items()}``
     （任一 value 渲染失败 → ExecError(phase=render)）
  4. ``yield node_completed({output})``

关键约束（SPEC §4.6 / §7.7）：
  - 纯计算，无子进程，无失败路径（除非 Jinja2 渲染错，那 fail loud）。
  - 用途：累积状态、算中间变量、存「当前最佳」（见 examples/nas.yaml 的 set 节点）。

依赖单向：本模块依赖 ``orca.exec.{interface,context,error,render}`` + ``orca.schema``；
不依赖 events.bus/run/compile。
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
from orca.schema import Event, SetNode


class SetExecutor(Executor):
    """Jinja2 求值存值的 executor（SPEC §4.6 / §7.7）。"""

    async def exec(self, node: SetNode, ctx: RunContext) -> AsyncIterator[Event]:
        session_id = uuid.uuid4().hex
        start = time.monotonic()

        def _ev(event_type: str, data: dict[str, Any]) -> Event:
            return Event(
                seq=0,  # 占位：orchestrator 在 tape.append 时重分配（决策 2）
                type=event_type,  # type: ignore[arg-type]
                timestamp=time.time(),
                node=node.name,
                session_id=session_id,
                data=data,
            )

        yield _ev("node_started", {"kind": "set", "values": dict(node.values)})

        try:
            # 3. 逐 key 渲染（任一失败 → ExecError(phase=render)）
            output: dict[str, Any] = {}
            for key, template in node.values.items():
                output[key] = render_template(template, ctx)

            elapsed = time.monotonic() - start
            yield _ev("node_completed", {"output": output, "elapsed": elapsed})

        except ExecError as e:
            elapsed = time.monotonic() - start
            err_data = {
                "kind": e.kind.value,
                "error_type": e.error_type,  # 读兼容期（写只 kind 为权威）
                "message": e.message,
                "phase": e.phase,
            }
            yield _ev("node_failed", err_data)
            yield _ev("error", err_data)
