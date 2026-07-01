"""orca.iface.mcp —— MCP 壳（外部 MCP 服务，stdio JSON-RPC，SPEC phase-10）。

回答「Claude Code / opencode / Cursor 等外部 MCP 客户端怎么把 Orca workflow 当工具调？」：
单进程单 ``RunManager`` + ``FastMCP`` + HandleId 三件套工具（start_workflow /
get_task_status / resolve_gate）+ cancel_task，stdio transport。

核心铁律（SPEC §0.1）：
  - **单进程单 RunManager**：``run_mcp_server`` 构造后 assert 单例；多壳（MCP / Web）
    同进程共享 ``_runs`` / ``_sem`` / ``_registry``。
  - **tape-only query path**：get_task_status 全部数据来自 ``manager.run_summary``
    （派生 tape），不读 handler._pending / _gates_meta。
  - **HandleId pattern**：每个 tool 秒级返回，不阻塞等 gate / 等事件。
  - **stdio flush 兜底**：transport.FlushingStdoutWriter（SDK 已 flush，本类作纵深防御）。
  - **source="mcp" resolve**：resolve_gate 调 handler.resolve 第三参数写死。
  - **不依赖客户端能力**：禁 elicitation / progress notification / 长轮询。

依赖单向：本包是渲染/转发壳，依赖 ``orca.iface.web.run_manager``（RunManager，D1 提供）+
``orca.gates``（HumanGate / handler.resolve）+ mcp SDK。不含编排/gate 决策逻辑。
"""

from orca.iface.mcp.server import OrcaMcpServer, run_mcp_server
from orca.iface.mcp.transport import FlushingStdoutWriter

__all__ = ["OrcaMcpServer", "run_mcp_server", "FlushingStdoutWriter"]
