"""env.py —— 子进程 env overlay 构造（profile 前缀透传，DRY 共享）。

回答「spawn claude 子进程时，哪些 env 变量要透传？」：``CliProfile.env_overlay_prefixes`` 声明
前缀（如 ``("ANTHROPIC_", "CLAUDE_")``），本模块从 ``os.environ`` 取匹配的变量，作为子进程
overlay（SPEC §2.6）。

**为何抽出来**：原 ``orca.exec.claude.executor._build_env_overlay`` /
``orca.exec.validator._build_env_overlay`` / ``orca.gates.dialog._build_env_overlay`` 三处实现
完全相同（遍历 ``os.environ`` + 前缀匹配）。Rule 6（DRY：禁止三处以上重复）明确触发抽象——
本模块是第四处出现前的抽象点，三处改 import 即可。

依赖单向：本模块只依赖 stdlib（``os``），不依赖 schema/run/events/iface——是 exec/ 层最底层的
工具函数，被 exec/ 内部子模块 + gates/dialog.py（gates → exec 允许方向）复用。
"""

from __future__ import annotations

import os


def build_env_overlay(prefixes: tuple[str, ...]) -> dict[str, str]:
    """从 ``os.environ`` 取前缀匹配的 env 变量，作为子进程 overlay（SPEC §2.6）。

    Args:
        prefixes: ``CliProfile.env_overlay_prefixes`` 声明的前缀元组
            （如 ``("ANTHROPIC_", "CLAUDE_")``）。

    Returns:
        ``{key: value}`` dict，传入 ``SpawnConfig.env_overlay``。子进程继承这些变量（如
        ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` for ccr 中转）。
    """
    overlay: dict[str, str] = {}
    for key, value in os.environ.items():
        if any(key.startswith(prefix) for prefix in prefixes):
            overlay[key] = value
    return overlay
