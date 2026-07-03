"""accumulator.py —— RunAccumulator（跨后端共享的终态累积器）。

回答「executor 怎么从后端流里收集「最终答案 + usage + cost + 错误」，且不被绑死在
某一种后端协议上？」：把两种终态信号模式收敛到一个累积器：

  - ``result_line`` 模式（claude/ccr）：CLIRunner 检测到 ``type==result`` 终止行 → 回调
    ``make_on_result_hook()`` 返回的 5 参闭包，一次性填满所有字段（行为**逐字同**重构前
    executor.py:144-169 的 ``result_holder`` + ``on_result`` 闭包）。
  - ``events`` 模式（opencode）：无终止行。executor 把每条翻译后的 Orca Event 既 ``yield``
    又喂 ``consume_event(ev)``——``agent_message`` 追加文本、``agent_usage`` 存 usage/cost、
    ``error`` 置 is_error + 抓 api_error_status。EOF 后字段已累积好。

两种模式共用同一组字段 + 同一个 ``diagnose(stderr)``（搬自 executor.py:188-200 的
``_result_diag``），executor 的 EOF 后有序错误判定 / node_completed 构造因此对模式无感，
只读 ``accumulator.*``。

为什么放 exec/claude/ 而非 exec/ 根：当前唯一消费者是 ClaudeExecutor（已按 profile.terminal.mode
分派）。它是「claude executor 路线的共享工具」，不是 exec 层通用基础设施（events 模式的
消费语义和 translator 产出强绑定，仍属 claude executor 视角）。若未来出现非 claude 路线
的 executor 复用它，再上提。

依赖单向：本模块只依赖 ``orca.schema.Event``（consume_event 的入参类型），不依赖
runner/profiles/run/compile —— 它是纯数据容器 + 闭包工厂，无 I/O。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orca.schema import Event


@dataclass
class RunAccumulator:
    """跨后端的终态累积器（mutable，一次 run 一个实例）。

    字段语义对齐重构前 ``result_holder`` dict 的 5 个 key：
      - ``result_text``：最终答案文本（result_line 模式来自 result.result；events 模式
        来自所有 agent_message.data["text"] 拼接）。
      - ``usage`` / ``cost``：token usage dict + 美元成本。
      - ``is_error``：后端自报错误（result_line 模式来自 result.is_error；events 模式
        来自 error 事件）。
      - ``api_error_status``：HTTP 错误码（如 529；result_line 模式来自 result 行顶层，
        events 模式目前 None——opencode error 事件无结构化 HTTP 码字段）。

    非 frozen：累积就是写。一次 run 一个实例，不复用。
    """

    result_text: str | None = None
    usage: dict[str, Any] | None = None
    cost: float = 0.0
    is_error: bool = False
    api_error_status: int | None = None
    # events 模式专用：error 事件的自报消息（让 diagnose 能带具体失败原因，否则用户看不到）。
    error_message: str | None = None
    # events 模式专用：累积 agent_message 文本片段。result_line 模式不用（直接覆盖 result_text）。
    _text_parts: list[str] = field(default_factory=list)

    # ── result_line 模式：on_result 回调工厂（行为逐字同重构前闭包）─────────────

    def make_on_result_hook(self):
        """返回 CLIRunner 的 on_result 5 参回调（result_line 模式）。

        签名对齐 ``runner.OnResult``：``(raw_result, usage, cost, is_error, api_error_status)``。
        行为**逐字等同**重构前 executor.py:152-160 的闭包：把 5 个字段一次性写入累积器。
        """

        def on_result(
            raw_result: str,
            usage: dict,
            cost: float,
            is_error: bool,
            api_error_status: int | None = None,
        ) -> None:
            self.result_text = raw_result
            self.usage = usage
            self.cost = cost
            self.is_error = is_error
            self.api_error_status = api_error_status

        return on_result

    # ── events 模式：逐事件累积 ──────────────────────────────────────────────────

    def consume_event(self, ev: Event) -> None:
        """events 模式：把一条翻译后的 Orca Event 喂进累积器（与 yield 并行调用）。

        映射（与 opencode_translator 产出对齐）：
          - ``agent_message``：追加 ``data["text"]`` 到 ``_text_parts``（最终拼成 result_text）。
          - ``agent_usage``：存 usage dict + cost（最后一条 step_finish 的为准）。
          - ``error``：置 ``is_error=True``，抓 ``data.get("api_error_status")``（若有）。

        其余事件类型（agent_tool_call / agent_tool_result / agent_thinking / node_*）不累积
        终态——它们已经 yield 给了 tape/订阅者，累积器只关心「最终答案 + usage + 错误」。
        """
        if ev.type == "agent_message":
            text = ev.data.get("text")
            if text:
                self._text_parts.append(text)
        elif ev.type == "agent_usage":
            # usage dict 整体存（executor 的 _normalize_usage 读具体 key）；cost 单独存。
            self.usage = dict(ev.data)
            # cost_usd 在 agent_usage.data 里（opencode_translator 从 step_finish.cost 带过来）。
            self.cost = float(ev.data.get("cost_usd", 0.0))
        elif ev.type == "error":
            self.is_error = True
            # error 事件的自报消息（opencode 把失败原因放这）。diagnose 带上它，否则 stderr 空
            # 时（典型 events 模式早退）用户完全看不到失败原因（与 claude result 文本进诊断同理由）。
            msg = ev.data.get("message")
            if isinstance(msg, str) and msg:
                self.error_message = msg
            status = ev.data.get("api_error_status")
            if status is not None:
                try:
                    self.api_error_status = int(status)
                except (TypeError, ValueError):
                    self.api_error_status = None

    @property
    def events_result_text(self) -> str | None:
        """events 模式：所有 agent_message 片段拼接成的最终答案文本。

        无任何 agent_message → None（让 executor 的「无 result」错误判定生效，与其他模式一致）。
        executor 在 events 模式 EOF 后把此值赋给 ``result_text``（统一后续读路径）。
        """
        if not self._text_parts:
            return None
        return "".join(self._text_parts)

    # ── 错误诊断（搬自 executor.py:188-200 的 _result_diag，DRY：两模式共用）────

    def diagnose(self, stderr: str) -> str:
        """构造错误诊断摘要（SPEC §6 可观测性）。

        行为逐字同重构前 ``_result_diag``：HTTP 码 / is_error / result 文本（截 300）/ stderr
        末尾（截 300）。executor 在每个 ExecError 分支的 message 里带它，否则 stderr 空 + API
        错误（如 529 早退）时用户完全看不到失败原因。
        """
        parts: list[str] = []
        if self.api_error_status is not None:
            parts.append(f"HTTP {self.api_error_status}")
        if self.is_error:
            parts.append("result.is_error=true")
        # events 模式 error 事件的自报消息（带具体失败原因，如 "Model not found"）。
        if self.error_message:
            parts.append(f"error={self.error_message[:300]!r}")
        rt = self.result_text
        if rt:
            parts.append(f"result={str(rt)[:300]!r}")
        if stderr:
            parts.append(f"stderr末尾={stderr[-300:]!r}")
        return "；".join(parts) if parts else "（无 stderr / result 详情）"
