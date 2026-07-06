"""_icons.py —— 节点状态图标常量（SPEC §4.1）。

单独文件避免 widgets/__init__.py 与各 widget 子模块的循环 import（``__init__``
``from .dag_tree import DagTree``，``dag_tree`` 又要拿 ``NODE_STATUS_ICONS``）。
"""

from __future__ import annotations

# 节点状态图标（SPEC §4.1，仿 claude agent view 行状态编码）。
# 全覆盖 canonical ``Status`` Literal（ADR §8.1 守门：icon 表 key 必须与 Status 完全一致）。
# pending=未开始 / running=执行中 / done=完成 / failed=失败 / skipped=跳过 / blocked=被 gate 拦。
NODE_STATUS_ICONS: dict[str, str] = {
    "pending": "○",
    "running": "✽",
    "done": "✓",
    "failed": "!",
    "skipped": "⊘",
    "blocked": "⏸",
}
