"""env.py —— 子进程 env overlay 构造（profile 前缀透传 + chart 路由 ORCA_* 注入，DRY 共享）。

回答「spawn claude 子进程时，哪些 env 变量要透传？」：两部分：

1. **profile 前缀透传**：``CliProfile.env_overlay_prefixes`` 声明前缀（如
   ``("ANTHROPIC_", "CLAUDE_")``），本模块从 ``os.environ`` 取匹配的变量，作为子进程
   overlay（SPEC phase-4 §2.6）。
2. **chart 路由 ORCA_* 注入**（phase-13 SPEC §2）：``run_id`` / ``node`` / ``session_id``
   / ``chart_sock`` 4 个 keyword 参数，缺省空串 → 不注 → backward compat（既有调用方
   ``build_env_overlay(prefixes)`` 不破）。ClaudeExecutor spawn 时显式传入，沿 subprocess
   链自然继承到 script；script 的 ``orca.chart.render_chart`` 从 env 读身份（铁律 #2）。

**为何抽出来**：原 ``orca.exec.claude.executor._build_env_overlay`` /
``orca.exec.validator._build_env_overlay`` / ``orca.gates.dialog._build_env_overlay`` 三处实现
完全相同（遍历 ``os.environ`` + 前缀匹配）。Rule 6（DRY：禁止三处以上重复）明确触发抽象——
本模块是第四处出现前的抽象点，三处改 import 即可。

依赖单向：本模块只依赖 stdlib（``os``），不依赖 schema/run/events/iface——是 exec/ 层最底层的
工具函数，被 exec/ 内部子模块 + gates/dialog.py（gates → exec 允许方向）复用。
"""

from __future__ import annotations

import os


def build_env_overlay(
    prefixes: tuple[str, ...],
    *,
    run_id: str = "",
    node: str = "",
    session_id: str = "",
    chart_sock: str = "",
    agent_resources: str = "",
) -> dict[str, str]:
    """从 ``os.environ`` 取前缀匹配的 env 变量，作为子进程 overlay（SPEC phase-4 §2.6）。

    Args:
        prefixes: ``CliProfile.env_overlay_prefixes`` 声明的前缀元组
            （如 ``("ANTHROPIC_", "CLAUDE_")``）。
        run_id: phase-13 §2 chart 路由用，``ctx.run_id``。空串 → 不注（backward compat）。
        node: phase-13 §2 chart 路由用，``node.name``。空串 → 不注。
        session_id: phase-13 §2 chart 路由用，executor 入口生成的 uuid。空串 → 不注。
        chart_sock: phase-13 §2 chart 路由用，``runs_dir / f"{run_id}.sock"`` 绝对路径。
            空串 → 不注。
        agent_resources: phase-14 agent 资源目录绝对路径（``node.resources_root``，文件夹
            agent 的根目录，含 scripts/refs）。空串 → 不注；非空 → 子进程 ``ORCA_AGENT_RESOURCES``，
            agent 的 Bash 工具据此 ``$ORCA_AGENT_RESOURCES/scripts/x.sh`` 引用 agent 自带资源。

    Returns:
        ``{key: value}`` dict，传入 ``SpawnConfig.env_overlay``。子进程继承这些变量（如
        ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` for ccr 中转；``ORCA_RUN_ID`` 等
        for phase-13 chart 路由）。

    phase-13 backward compat：4 个 ORCA_* keyword 全部缺省（空串）时，行为与重构前完全
    一致（仅 prefix 透传）。ClaudeExecutor 显式传入时启用 chart 路由。
    """
    overlay: dict[str, str] = {}
    for key, value in os.environ.items():
        if any(key.startswith(prefix) for prefix in prefixes):
            overlay[key] = value
    # phase-13 §2：chart 路由 ORCA_* 注入（缺省空串 → 不注，backward compat）。
    if run_id:
        overlay["ORCA_RUN_ID"] = run_id
    if node:
        overlay["ORCA_NODE"] = node
    if session_id:
        overlay["ORCA_SESSION_ID"] = session_id
    if chart_sock:
        overlay["ORCA_CHART_SOCK"] = chart_sock
    # phase-14：agent 资源目录（文件夹 agent 的根），agent Bash 工具据 $ORCA_AGENT_RESOURCES 引用 scripts/refs。
    if agent_resources:
        overlay["ORCA_AGENT_RESOURCES"] = agent_resources
    return overlay
