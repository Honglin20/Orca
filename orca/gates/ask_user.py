"""ask_user.py —— agent 主动问用户（HumanGate 的第二个来源）。

回答「agent 执行中想主动问用户（如要数据库连接串）怎么办？」：claude 调一个 MCP 工具
``ask_user`` → 触发 ``HumanGate(source=agent_ask)`` → 等任一壳 resolve → 返回 answer。

phase 6 范围（SPEC §5.2）：提供 ``ask_user(handler, ...)`` 函数（接 handler）+ 测试。
**MCP 工具注册**（让 claude 能调 ``ask_user``）归 phase 10 MCP 壳——本函数是壳的
resolve 路径占位，phase 10 在 MCP server 里 wrap 它成 tool。

与 hook 桥的对比（SPEC §1.2）：
  - hook 桥（``source=tool_permission``）：Claude 想调危险工具，PreToolUse hook 触发。
  - ask_user（``source=agent_ask``）：agent 主动问，MCP 工具触发。
  两者共用 ``HumanGate`` 模型 + ``HumanGateHandler`` 暂停/竞速/广播，仅 ``source``
  + ``context`` 不同（壳据此选渲染分支：权限弹窗 vs 问答弹窗）。

依赖单向：本模块依赖 ``orca.gates.{types,handler}``；不依赖 run/exec/iface。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orca.gates.types import HumanGate
from uuid import uuid4

if TYPE_CHECKING:
    from orca.gates.handler import HumanGateHandler


async def ask_user(
    handler: HumanGateHandler,
    prompt: str,
    options: list[str] | None = None,
    context: dict | None = None,
    run_id: str = "",
    node: str | None = None,
    session_id: str | None = None,
) -> str:
    """agent 主动问用户。触发 ``HumanGate(source=agent_ask)``，等任一壳答。

    Args:
        handler: HumanGateHandler（共享给 request）。
        prompt: 给人看的问题。
        options: 固定选项（``None`` = 自由文本输入，SPEC §1.1）。
        context: 上下文详情（壳渲染，如 ``{"suggested": [...]}``）。
        run_id / node: 哪个 run / node 触发的（广播定位 + 日志）。
        session_id: claude 的 session_id（透传到 event 顶层，phase 3 §3.3 身份模型；
            ``None`` = workflow 级 / 未关联 claude 会话）。

    Returns:
        answer（壳喂回的答案；若是固定选项则是选项值，若是自由文本则是用户输入）。

    阻塞至任一壳 resolve（gate 无限等，SPEC §2.2 决策 3）。
    """
    gate = HumanGate(
        id=uuid4().hex,
        prompt=prompt,
        options=options,
        context=context or {},
        source="agent_ask",
        run_id=run_id,
        node=node,
        session_id=session_id,
    )
    answer, _source = await handler.request(gate)
    return answer
