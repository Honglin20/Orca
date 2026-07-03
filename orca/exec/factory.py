"""factory.py —— make_executor(node) → Executor（按 node.kind 分派）。

回答「给定 node，用哪个 executor 跑？」：单一分派入口（SPEC §4.1 / §7.8）。

分派规则（SPEC §7.8）：
  - ``AgentNode``     → ``ClaudeExecutor(get_profile(node.executor), agent_tools_server, runs_dir)``
  - ``ScriptNode``    → ``ScriptExecutor(runs_dir=runs_dir)``（phase-13 §11 #9 executor-agnostic）
  - ``SetNode``       → ``SetExecutor()``
  - ``WaitNode``      → ``WaitExecutor(bus)``（phase 11 §9.7，需 bus 注册 wait handle）
  - ``TerminateNode`` → ``TerminateExecutor()``（业务级显式终止节点）
  - ``ForeachNode``   → ``raise NotImplementedError``（编排归 phase 5，本阶段不做）

OCP：加新 kind / 新 backend 不改本函数核心（agent backend 切换靠 profiles 注册表，
新叶子 kind 靠新增 Executor 子类 + 这里的分派项）。

phase 11 §5.4（review C4）：``make_executor`` 加可选第二参 ``agent_tools_server``，仅
agent 分支透传给 ``ClaudeExecutor``（用于 ``--mcp-config`` ask_user 挂载）；script/set/
foreach 分支忽略它（None == 既有行为，向后兼容）。

phase 11 §9.7.4：``make_executor`` 加可选第三参 ``bus``，仅 wait 分支透传给
``WaitExecutor``（``interruptible=True`` 时注册 wait handle，InterruptHandler 经
``bus.notify_all_waits()`` 打断）；None == script/set/foreach 既有行为（向后兼容）。

phase-13 §2：``make_executor`` 加可选 keyword ``runs_dir``，**agent + script 分支**对称透传给
对应 executor（用于 spawn env 注入 ``ORCA_CHART_SOCK``）。None == 既有行为（向后兼容，
env 不注 chart 路由，script 端 render_chart 会 fail loud）。orchestrator 从
``self.bus.tape.path.parent`` 推导传入。phase-13 §11 #9（executor-agnostic）：script 与
agent 两路径同样需要 chart 路由（agent spawn 的 Bash 工具会再 spawn script，沿 env 链继承）。

依赖单向：本模块依赖 ``orca.profiles``（agent backend 解析）+ exec 内部子模块；
不依赖 run/compile。``AgentToolsMcpServer`` / ``WaitHandleRegistry`` 仅用于类型注解
（``TYPE_CHECKING``）；wait 路径的 bus 由 orchestrator 透传（结构化满足 Protocol）。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from orca.exec.interface import Executor
from orca.schema import (
    AgentNode,
    ForeachNode,
    Node,
    ScriptNode,
    SetNode,
    TerminateNode,
    WaitNode,
)

if TYPE_CHECKING:
    from orca.exec.mcp_tools.server import AgentToolsMcpServer
    from orca.exec.wait import WaitHandleRegistry


def make_executor(
    node: Node,
    agent_tools_server: AgentToolsMcpServer | None = None,
    bus: WaitHandleRegistry | None = None,
    *,
    runs_dir: Path | None = None,
) -> Executor:
    """按 ``node.kind`` 分派到对应 Executor 实例（SPEC §7.8 / phase 11 §5.4 / §9.7.4 / phase-13 §2）。

    AgentNode 经 ``get_profile(node.executor)`` 解析 backend（默认 "claude"）；
    不存在 / 被 disable 的 executor → ``get_profile`` 抛 ``ValueError``（透传，fail loud）。

    ``agent_tools_server``（phase 11 §5.4）：仅 agent 分支透传给 ``ClaudeExecutor``——
    非空时 spawn claude 带 ``--mcp-config``（暴露 ask_user）；None == 既有行为（向后兼容）。
    script/set/foreach/terminate 分支忽略此参。

    ``bus``（phase 11 §9.7.4）：仅 wait 分支透传给 ``WaitExecutor``——wait node 的
    ``interruptible=True`` 路径要 ``register_wait_handle``。None 时遇到 WaitNode →
    fail loud（``ValueError``）：interruptible wait 没 bus 无法注册 handle，静默跳过会
    让 Ctrl+G 打断契约失效（SPEC §9.7.6）。script/set/foreach/terminate 分支忽略此参。

    ``runs_dir``（phase-13 §2）：**agent + script 分支对称透传**——ClaudeExecutor /
    ScriptExecutor spawn 时把它和 ``run_id`` 拼成 ``runs/<run_id>.sock`` 注入
    ``ORCA_CHART_SOCK`` env。None == 不注（向后兼容，env 缺 → script 端 render_chart fail loud）。
    orchestrator 从 ``self.bus.tape.path.parent`` 推导传入（同 tape 父目录）。
    phase-13 §11 #9（executor-agnostic）：script 与 agent 两路径对称需要 chart 路由
    （agent spawn 的 Bash 工具会再 spawn script，沿 env 链继承）。

    TerminateNode 是纯渲染节点（无子进程 / 无 wait handle），不需要 bus / agent_tools_server；
    orchestrator 据 ``node_completed.data.status`` 分发 workflow 级终态事件。

    ForeachNode 本阶段 ``raise NotImplementedError``（foreach 分批 / 并行归 phase 5 编排层）。
    """
    # 惰性导入：避免 ``orca.exec.__init__`` import 时拉起 claude/script/set 全链
    # （jinja2 / jsonschema / asyncio subprocess），factory 仅在被实际调用时才加载。
    if isinstance(node, AgentNode):
        from orca.exec.claude.executor import ClaudeExecutor
        from orca.profiles import get_profile

        return ClaudeExecutor(
            get_profile(node.executor),
            agent_tools_server,
            runs_dir=runs_dir,
        )

    if isinstance(node, ScriptNode):
        from orca.exec.script import ScriptExecutor

        # phase-13 §11 #9（executor-agnostic）：与 ClaudeExecutor 路径对称——script 子进程
        # 也接 ``runs_dir``，spawn 时合入 chart env overlay，让 script 内
        # ``orca.chart.render_chart`` 推图到正确 run 的 ingestor。None == 向后兼容。
        return ScriptExecutor(runs_dir=runs_dir)

    if isinstance(node, SetNode):
        from orca.exec.set_node import SetExecutor

        return SetExecutor()

    if isinstance(node, WaitNode):
        from orca.exec.wait import WaitExecutor

        if bus is None:
            # interruptible wait 没 bus 无法注册 handle → fail loud（打断契约不能静默失效）。
            # interruptible=False 理论上不需 bus，但保持单一构造路径（避免按字段分叉构造）：
            # 调用方一律传 bus，WaitExecutor 仅 interruptible 分支才用。
            raise ValueError(
                "WaitExecutor 需要 bus（wait handle 注册），make_executor 未传入 bus"
            )
        return WaitExecutor(bus)

    if isinstance(node, TerminateNode):
        from orca.exec.terminate import TerminateExecutor

        return TerminateExecutor()

    if isinstance(node, ForeachNode):
        raise NotImplementedError(
            "foreach 归 phase 5 编排层（分批 / 并行 / 失败策略），本阶段 exec/ 不实现"
        )

    # 不该到这里：node.kind 是 Literal 联合，6 选 1 之外是 schema 层漏校验。
    raise TypeError(
        f"make_executor 不支持 node kind {node.kind!r}（type={type(node).__name__}）"
    )
