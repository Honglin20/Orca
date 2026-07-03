"""terminal.py —— TerminalContract（后端「如何信号 done + 最终答案 + usage + 错误」契约）。

回答「这个后端跑完一轮，executor 怎么知道结束了、最终答案是什么、用了多少 token、是否报错？」：
不同后端协议差异极大，归纳成两种模式：

  - ``result_line``（claude / ccr）：流末尾有一行 ``type==result`` 的终止行，里面同时带最终
    文本 + usage + cost + is_error + api_error_status。CLIRunner 检测到这行就回调 ``on_result``
    （5 参）把全部终态一次性交 executor。
  - ``events``（opencode）：**没有**终止行。最终答案是所有 ``text`` 事件的拼接；usage 在
    ``step_finish`` 事件里；错误是一个 ``error`` 事件。executor 用 ``RunAccumulator.consume_event``
    边流边累积，EOF 后从累积器取终态。

为什么是 profile 层契约而非 exec 层 if/else：加新后端 = 选一个模式 + 写 translator，
executor 只按 ``profile.terminal.mode`` 走对应分支（OCP：加后端不改 executor 主体）。
模式本身是 backend 专属知识（与 profile 同居），不属于通用 exec/。

依赖单向：本模块只依赖 stdlib（dataclasses / typing），不依赖 schema/exec/run —— 它是纯
契约描述，无逻辑、无副作用，被 ``base.CliProfile`` 持有。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TerminalContract:
    """后端的终态信号契约（profile 层，纯描述）。

    ``mode`` 是唯一字段——两种模式的语义差异由 executor + RunAccumulator 实现，本类只是
    让 executor 能按 mode 分派的「标记」。frozen：契约不可变（与 CliProfile 一致）。
    """

    mode: Literal["result_line", "events"]


# 内置常用契约实例（profile 直接引用，避免每个 profile 重复构造字面量）。
# claude / ccr 用 RESULT_LINE；opencode 用 EVENTS。
RESULT_LINE = TerminalContract(mode="result_line")
EVENTS = TerminalContract(mode="events")
