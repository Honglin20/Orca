"""templates —— 宿主侧哑传输模板（CC hook 脚本 / opencode plugin / slash command）。

**架构守门**（D-v7-1）：模板里的宿主侧代码**零 Orca 业务逻辑**——只 spawn CLI 子进程
+ parse JSON 顶层字段。advance/router/replay/tape 路径/``<task_result>`` 解析（plugin
侧）一律禁止（CI grep 守门）。

模板由 CLI ``start``（CC 路）/项目 ``.opencode/``（opencode 路）落地，不在 Python 运行
时加载（仅 ``render_cc_settings_fragment`` 等渲染函数被 CLI 调用）。
"""

from __future__ import annotations

from orca.iface.in_session.templates.cc_hooks import render_cc_settings_fragment

__all__ = ["render_cc_settings_fragment"]

