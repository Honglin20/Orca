"""orca.iface —— 入口壳层（CLI / Web / MCP）。

回答「用户怎么和 Orca 交互？」：三壳各自渲染同一个事件流 + 各自的 gate resolve 路径，
**壳不持有业务真相**（事件流是唯一真相，UI 只是事件流的派生物，SPEC §6.0 铁律 1）。

依赖铁律：iface 是最上层消费者，依赖 ``orca.{run, gates, events, compile, schema}``，
**不被任何模块 import**（grep ``from orca.iface`` 应只在 orca/iface/ 内部命中）。

子包：
  - cli/  : Textual TUI 壳（phase 7）—— 终端跑 workflow + gate ModalScreen
  - web/  : FastAPI + SPA 壳（phase 9，未实现）
  - mcp/  : MCP server 壳（phase 10，未实现）
"""

from __future__ import annotations

__all__: list[str] = []
