"""orca.iface.mcp —— MCP 壳 v4（外部 MCP 服务，stdio JSON-RPC，SPEC phase-10）。

回答「Claude Code / opencode / Cursor 等外部 MCP 客户端怎么把 Orca workflow 当工具调？」：
单进程单 ``RunManager`` + ``FastMCP`` + 8 工具（Discovery 3 + Lifecycle 3 + History 2），
stdio transport。

v4 核心设计（2026-07-07 重写）+ in-session v5 §6.2 精简：
  - **8 工具**：Discovery（list_workflows / describe_workflow / list_agents）+
    Lifecycle（start_workflow / get_task_status / cancel_task）+
    History（get_task_history / get_agent）。
  - **execute phase agent 不配 ask_user/gate**（compile validator 强制，铁律 7）。
  - **Result 信封**（ADR §4.1）：所有 tool 返 ``{ok, data?, error?, _hint?}``；
    ``error.kind`` 是 ``ErrorKind`` 值（**无 layer 字段**）。
  - **execute phase 永不中断**：删 ``resolve_gate``（v4 铁律 7）。
  - **in-session v5 §6.2**：删 ``get_agent_prompt`` + ``start_workflow`` 删 ``setup_outputs``
    （setup phase 全栈删除）。

核心铁律（SPEC §0.1）：
  - **单进程单 RunManager**：``run_mcp_server`` 构造后 assert 单例。
  - **tape-only query path**：get_task_status / get_task_history 数据来自 tape。
  - **HandleId pattern**：每个 tool 秒级返回，不阻塞等 gate / 等事件。
  - **stdio flush 兜底**：transport.FlushingStdoutWriter（SDK 已 flush，本类作纵深防御）。
  - **不依赖客户端能力**：禁 elicitation / progress notification / 长轮询。

依赖单向：本包是渲染/转发壳，依赖 ``orca.iface.web.run_manager``（RunManager）+
``orca.exec.result``（Result/Error/ErrorKind）+ mcp SDK。不含编排/gate 决策逻辑。
"""

from orca.iface.mcp.server import OrcaMcpServer, run_mcp_server
from orca.iface.mcp.transport import FlushingStdoutWriter

__all__ = ["OrcaMcpServer", "run_mcp_server", "FlushingStdoutWriter"]
