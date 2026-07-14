"""templates —— 宿主侧哑传输模板（opencode plugin）。

**架构守门**（D-v7-1）：模板里的宿主侧代码**零 Orca 业务逻辑**——只 spawn CLI 子进程
+ parse JSON 顶层字段。advance/router/replay/tape 路径/``<task_result>`` 解析（plugin
侧）一律禁止（CI grep 守门）。

v5 §8 step 2b：``cc_hooks.py``（CC 路 A 的 Stop/PostToolUse hook 脚本生成）已删——
A 路径退场，B 路径（主 session 自调 ``orca next``）统一。``start`` 命令同 commit 删除
（cc_hooks 仅被它调用，删后即死代码）。

模板由项目 ``.opencode/`` 落地（plugin），不在 Python 运行时加载（仅 ``_constants`` 的
常量被引用）。``orca.ts`` plugin 的 transform marker 派发已禁用（step 2b early return），
整个 plugin 文件 step 4 删。
"""

from __future__ import annotations

from orca.iface.in_session.templates._constants import MARKER_LITERAL, MARKER_REGEX

__all__ = ["MARKER_LITERAL", "MARKER_REGEX"]
