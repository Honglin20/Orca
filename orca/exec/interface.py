"""interface.py —— Executor 抽象基类（Layer 1，后端无关契约）。

回答「执行单个 node 的契约是什么？」：``async exec(node, ctx) -> AsyncIterator[Event]``。
executor 产出事件流，**不写 tape**（写 tape 归 phase 5 orchestrator，依赖单向铁律 2）。

事件生命周期契约（所有 Executor 必须遵守，SPEC §4.2）::

    node_started(node, session_id)
      ├── agent_message / agent_thinking（agent kind，流式增量）
      ├── agent_tool_call / agent_tool_result（agent kind，每回合）
      ├── agent_usage（agent kind，仅 result 时一次）
      └── script/set 无中间事件
    node_completed(node, session_id, data={output, elapsed})
      —— 或 ——
    node_failed(node, session_id, data={error_type, message, phase})

session_id 由 executor 在 ``exec()`` 入口生成（``uuid4().hex``），全程复用（铁律 5）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from orca.exec.context import RunContext
from orca.schema import Event, Node


class Executor(ABC):
    """执行单个 node，产出事件流。后端无关契约（SPEC §4.2）。

    子类实现 ``exec``：``yield node_started`` → 流式事件 → ``yield node_completed``
    或 ``yield node_failed``（+ ``error``）。所有事件顶层带同一 ``session_id``。

    产出的事件流由上层（phase 5 orchestrator）逐个 ``bus.emit(..., session_id=...)``；
    executor 自身**不写 tape、不调 bus**（依赖单向铁律 2）。
    """

    @abstractmethod
    async def exec(self, node: Node, ctx: RunContext) -> AsyncIterator[Event]:
        """执行 ``node``，yield 事件（完整生命周期）。

        必须保证：
          - 第一个事件是 ``node_started``（顶层 node + session_id）
          - 成功 → 末事件 ``node_completed``（data 含 output / elapsed）
          - 失败 → ``node_failed`` + ``error`` 双发（fail loud，铁律 4）
          - 单次调用内所有 Event.session_id 一致（铁律 5）

        ``node`` 类型为基类 ``Node``；具体子类（AgentNode/ScriptNode/SetNode）由
        ``make_executor`` 分派时绑定。子类实现按自己期望的 kind 解析。
        """
        ...
        # abstractmethod + ... ：抽象方法体不 yield（类型上声明 AsyncIterator，
        # 实现由子类提供）。``...`` 让本行成为合法函数体。
        if False:  # pragma: no cover  - 仅满足 AsyncIterator 类型注解的语法
            yield  # type: ignore[unreachable]
