"""factory.py —— make_executor(node) → Executor（按 node.kind 分派）。

回答「给定 node，用哪个 executor 跑？」：单一分派入口（SPEC §4.1 / §7.8）。

分派规则（SPEC §7.8）：
  - ``AgentNode``   → ``ClaudeExecutor(get_profile(node.executor), agent_tools_server)``
  - ``ScriptNode``  → ``ScriptExecutor()``
  - ``SetNode``     → ``SetExecutor()``
  - ``ForeachNode`` → ``raise NotImplementedError``（编排归 phase 5，本阶段不做）

OCP：加新 kind / 新 backend 不改本函数核心（agent backend 切换靠 profiles 注册表，
新叶子 kind 靠新增 Executor 子类 + 这里的分派项）。

phase 11 §5.4（review C4）：``make_executor`` 加可选第二参 ``agent_tools_server``，仅
agent 分支透传给 ``ClaudeExecutor``（用于 ``--mcp-config`` ask_user 挂载）；script/set/
foreach 分支忽略它（None == 既有行为，向后兼容）。

依赖单向：本模块依赖 ``orca.profiles``（agent backend 解析）+ exec 内部子模块；
不依赖 run/compile。``AgentToolsMcpServer`` 仅用于类型注解（``TYPE_CHECKING``）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orca.exec.interface import Executor
from orca.schema import AgentNode, ForeachNode, Node, ScriptNode, SetNode

if TYPE_CHECKING:
    from orca.exec.mcp_tools.server import AgentToolsMcpServer


def make_executor(
    node: Node, agent_tools_server: AgentToolsMcpServer | None = None
) -> Executor:
    """按 ``node.kind`` 分派到对应 Executor 实例（SPEC §7.8 / phase 11 §5.4）。

    AgentNode 经 ``get_profile(node.executor)`` 解析 backend（默认 "claude"）；
    不存在 / 被 disable 的 executor → ``get_profile`` 抛 ``ValueError``（透传，fail loud）。

    ``agent_tools_server``（phase 11 §5.4）：仅 agent 分支透传给 ``ClaudeExecutor``——
    非空时 spawn claude 带 ``--mcp-config``（暴露 ask_user）；None == 既有行为（向后兼容）。
    script/set/foreach 分支忽略此参。

    ForeachNode 本阶段 ``raise NotImplementedError``（foreach 分批 / 并行归 phase 5 编排层）。
    """
    # 惰性导入：避免 ``orca.exec.__init__`` import 时拉起 claude/script/set 全链
    # （jinja2 / jsonschema / asyncio subprocess），factory 仅在被实际调用时才加载。
    if isinstance(node, AgentNode):
        from orca.exec.claude.executor import ClaudeExecutor
        from orca.profiles import get_profile

        return ClaudeExecutor(get_profile(node.executor), agent_tools_server)

    if isinstance(node, ScriptNode):
        from orca.exec.script import ScriptExecutor

        return ScriptExecutor()

    if isinstance(node, SetNode):
        from orca.exec.set_node import SetExecutor

        return SetExecutor()

    if isinstance(node, ForeachNode):
        raise NotImplementedError(
            "foreach 归 phase 5 编排层（分批 / 并行 / 失败策略），本阶段 exec/ 不实现"
        )

    # 不该到这里：node.kind 是 Literal 联合，4 选 1 之外是 schema 层漏校验。
    raise TypeError(
        f"make_executor 不支持 node kind {node.kind!r}（type={type(node).__name__}）"
    )
