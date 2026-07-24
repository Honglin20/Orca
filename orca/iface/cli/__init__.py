"""orca.iface.cli —— CLI 壳（Textual TUI，phase 7）。

回答「用户在终端怎么跑一个 workflow？」：``orca run/validate/list``（typer 命令）+
Textual TUI（DAG 进度 / 日志流 / gate ModalScreen）。

5 条铁律（SPEC §6.0）：
  - 壳无业务真相（所有 UI 状态是事件流派生物，tape 是唯一真相）
  - gate 走 phase 6 handler.resolve（壳不存 gate 状态）
  - @work + push_screen_wait（gate 阻塞 worker 不阻塞 UI）
  - 依赖单向：iface/cli → run + gates + events + compile + schema（不被任何模块 import）
  - Textual（非 Rich Live）：gate prompt 能在渲染期输入

模块：
  - commands.py    : orca run/validate/list 命令绑定 + 参数解析（纯函数）
  - app.py         : OrcaApp（Textual App）—— compose + @work worker + gate 桥
  - widgets/       : DagTree / ActiveNode / LogStream / Header
  - screens/       : GateModal（ModalScreen）
"""

from __future__ import annotations

# **__init__ 故意轻量（PEP 562 ``__getattr__`` lazy）**：``app.py``（OrcaApp，Textual TUI）
# + ``commands.py`` 是重依赖，eager import 会使任何 ``import orca.iface.cli.<子模块>``（如
# config / executor_cmds）被迫加载整个 TUI 壳（~1s+，拖慢 sidechain 守护等 import 链 → pidfile
# 迟写 / liveness 误判）。console_scripts 直指 ``commands:main`` / ``in_session.cli:main``，不经本 __init__。
__all__ = ["main", "OrcaApp"]


def __getattr__(name: str):
    """``main`` / ``OrcaApp`` 按需加载（无人经包顶层引用，见上方说明）。"""
    if name == "main":
        from orca.iface.cli.commands import main
        return main
    if name == "OrcaApp":
        from orca.iface.cli.app import OrcaApp
        return OrcaApp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
