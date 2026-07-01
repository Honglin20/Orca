"""orca.exec.mcp_tools —— 给被编排的 claude -p 暴露的内嵌 MCP server（phase 11 §5）。

回答「被 orca spawn 的 claude agent 怎么主动问用户？」：Orca 进程内嵌一个 socket SSE
MCP server（``AgentToolsMcpServer``），监听 loopback port，注册 ``ask_user`` 工具。
claude -p 经 ``--mcp-config <json>`` 连上，调 ``ask_user`` → 触发 ``HumanGate`` → 等壳答。

与 phase 10 ``OrcaMcpServer`` 的边界（SPEC §5.3）：
  - phase 10 ``orca.iface.mcp.server.OrcaMcpServer``（**stdio**）：给**外部** CC 主对话用，
    暴露 ``start_workflow`` / ``get_task_status`` / ``resolve_gate`` / ``cancel_task``。
  - 本模块 ``AgentToolsMcpServer``（**socket SSE**）：给 orca **内部 spawn** 的 claude 用，
    暴露 ``ask_user``（agent 主动问）。

依赖单向（铁律）：本模块依赖 ``mcp.server.fastmcp`` + ``orca.gates``（ask_user / HumanGateHandler /
SessionContextRegistry）+ ``orca.exec.context``（RunContext，仅 TYPE_CHECKING）。**不依赖**
``iface/``（iface 是唯一 textual-UI 层）。

路由设计（SPEC §5.3 / §5.5，review item4 + 决策 D4）：**确定性 tool-params 路由**——
``ask_user`` 工具入参强制带 ``_orca_run_id`` / ``_orca_node``（hidden params），由
``render_prompt`` 在 agent prompt 里 instruct claude 调用时必填。**不依赖** MCP session
反查（claude -p 不主动报 MCP session，假设不成立——review item4 修正）。
"""

from orca.exec.mcp_tools.server import AgentToolsMcpServer

__all__ = ["AgentToolsMcpServer"]
