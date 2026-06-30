"""types.py —— HumanGate 原语（统一两个决策来源）。

回答「人机决策的数据模型是什么？」：两个决策来源（Claude 想调危险工具 / agent
主动问用户）**本质同源**——都是「暂停引擎、等人决策、继续」。统一为 ``HumanGate``
frozen dataclass，用 ``source`` 字段区分壳的渲染分支（权限弹窗 vs 问答弹窗），
**不在数据流上分化**（避免「两套机制」，SPEC §1.2）。

字段语义（SPEC §1.1）：
  - ``id``：uuid4 hex，gate 唯一标识（壳 resolve 用它定位）。
  - ``prompt``：给人看的问题。
  - ``options``：固定选项（``None`` = 自由文本输入；hook 桥固定 ``["allow","deny"]``）。
  - ``context``：哪个 node / 哪个工具 / 什么参数（壳渲染上下文详情）。
  - ``source``：``"tool_permission"``（PreToolUse hook 触发）/ ``"agent_ask"``
    （ask_user MCP 工具触发）—— 仅驱动壳的 UI 渲染分支。
  - ``run_id`` / ``node``：哪个 run / 哪个 node 触发的（广播定位 + 日志）。
  - ``session_id``：claude 的 session_id（phase 3 §3.3 身份模型——emit 透传到 event 顶层，
    让壳 reducer 按 session 分组关联 gate 事件到具体 claude 会话；None = workflow 级 gate）。
  - ``timeout_hint``：给壳的 UI 提示（**非强制**；gate 本身无限等，SPEC §2.2 决策 3）。

设计原则：
  - **frozen**：gate 构造后不可变（多壳并发读同一份，无 race）。
  - **source 仅是渲染依据**：两个 source 共用同一模型，仅 ``context`` 内容不同。
  - 零逻辑：纯数据，不含行为（行为在 ``HumanGateHandler``）。

依赖单向：本模块零依赖（仅 stdlib + typing），不依赖任何 orca 子包。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# 决策来源（仅驱动壳的渲染分支；data 流统一）。
GateSource = Literal["tool_permission", "agent_ask"]


@dataclass(frozen=True)
class HumanGate:
    """统一的人机决策原语（SPEC §1.1）。

    两个来源（工具权限 / agent 主动问）共用此模型，用 ``source`` 字段区分渲染。
    frozen 保证多壳并发读同一份 gate 不发生 race。
    """

    id: str
    prompt: str
    context: dict
    source: GateSource
    run_id: str
    node: str | None
    session_id: str | None = None
    options: list[str] | None = None
    timeout_hint: float | None = None
