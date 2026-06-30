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

from orca.iface.cli.app import OrcaApp
from orca.iface.cli.commands import main

__all__ = ["main", "OrcaApp"]
