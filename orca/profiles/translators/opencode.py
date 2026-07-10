"""translators/opencode.py —— opencode JSON-L 流 → Orca Event（纯函数）。

回答「opencode ``--format json`` 一行，怎么变成 Orca 事件？」：按真实 opencode v1.14.22
NDJSON 协议（抓取校准，见 ``tests/profiles/fixtures/opencode_sample.jsonl``）逐字映射。

opencode 协议与 claude stream-json 完全不同：
  - 每行是一个 ``part``（信封），顶层 ``type`` 标事件类型，``part`` 是 payload。
  - **无 result 终止行**——最终答案 = 所有 ``text`` 事件的 ``part.text`` 拼接；usage 在
    ``step_finish`` 的 ``part.tokens`` / ``part.cost``；错误是单独的 ``error`` 事件。
    故 opencode 走 ``TerminalContract(mode="events")``，executor 用 RunAccumulator 累积。

映射（按真实抓取字段，web-shell-v2 §3.2 lossless 扩展）：
  1. ``step_start``（``part.type="step-start"``，``part.reason`` 可选）→ ``agent_step_started``
     （``{step_reason?: reason}``，web-shell-v2 §3.2 B1 / §6 liveness 心跳）。
  2. ``reasoning``（``part.text`` 整块，``--thinking`` on 时发）→ ``agent_thinking{text}``
     （与 claude thinking_delta 同 canonical 事件，前端琥珀折叠渲染）。
  3. ``text``（``part.text`` 整块）→ ``agent_message{text}``（**整块**，非增量——opencode
     一次发完整文本段，不是 token-by-token）。
  4. ``tool_use``（``part.tool`` / ``part.callID`` / ``part.state``）→ 完成时（``state.status
     =="completed"``）一次发 ``agent_tool_call`` + ``agent_tool_result``。opencode 把调用与
     结果合在一条事件里（state 同时带 input + output），不像 claude 分 assistant/user 两行。
  5. ``step_finish``（``part.tokens`` / ``part.cost``）→ ``agent_usage``（input/output/cache/
     cost/reasoning_tokens；tokens 在该 step 是累积值）。**注意**：opencode 每个 reasoning step
     发一条 ``agent_usage``（多步 = 多条），与 claude「result 行只发一次」语义不同。下游聚合
     cost 时取 ``node_completed.data.usage``（RunAccumulator 存的最后一条 step_finish）
     为准，不要对 tape 里的 per-step agent_usage 求和（会重复计费）。
     ``reasoning_tokens`` 来自 ``tokens.reasoning``（无该字段 = 0；旧 tape 默认 0，
     ``data.get('reasoning_tokens', 0)`` 消费侧兜底，**不破坏旧 tape replay**）。
  6. ``error``（``error.data.message`` / ``error.name``）→ ``error`` 事件（带 message；opencode
     的 error.data 无结构化 HTTP 码字段，故 ``api_error_status`` 不设——RunAccumulator 抓不到
     时为 None，executor 的错误诊断照样能带 message）。
  7. 未知 envelope type → ``unknown_event{raw, source:"opencode"}``（web-shell-v2 §3.2 / D8：
     tape 级 escape hatch，**绝不静默丢**；reducer MUST no-op，仅 LogStream 渲染）。

纯函数（铁律 3）：``opencode_translator(line, session_id) -> list[Event]``，无 self / 无 I/O /
无副作用。fixture 驱动测试，不 spawn opencode。

session_id 归属（同 claude_translator）：translator 接收入参 session_id（executor 入口生成），
透传到产出 Event，不复用 opencode 流里的 ``sessionID`` 字段（那是 opencode 内部会话 id）。

依赖单向：本模块只依赖 ``orca.schema``（Event），不依赖 exec/events.bus/run/compile。
"""

from __future__ import annotations

import json
import time
from typing import Any

from orca.schema import Event

# agent_tool_result 的 result 文本截断上限（与 claude_translator 一致，防异常工具输出喷爆）。
_TOOL_RESULT_MAX_CHARS = 4096


def opencode_translator(line: str, session_id: str) -> list[Event]:
    """opencode JSON-L 一行 → list[Event]（纯函数）。

    按顶层 ``type`` 分派（见模块 docstring 映射）。非 JSON 行返回 ``[]``（CLIRunner 已对非
    JSON 行 debug log + 跳过；此处防御性 ``[]`` 不抛）。
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        # 正常不该到这（CLIRunner 已过滤），保持纯函数健壮性：非 JSON → []（不抛）。
        return []
    if not isinstance(obj, dict):
        return []

    top_type = obj.get("type")
    if top_type == "text":
        return _translate_text(obj, session_id)
    if top_type == "tool_use":
        return _translate_tool_use(obj, session_id)
    if top_type == "step_finish":
        return _translate_step_finish(obj, session_id)
    if top_type == "error":
        return _translate_error(obj, session_id)
    if top_type == "reasoning":
        return _translate_reasoning(obj, session_id)
    if top_type == "step_start":
        return _translate_step_start(obj, session_id)
    # web-shell-v2 §3.2 / D8：未知 envelope → unknown_event（tape 级 escape hatch）。
    # **不静默丢**：reducer MUST no-op（仅 LogStream 渲染），但 tape 留证据便于排查协议漂移。
    return [_event(
        "unknown_event", session_id, {"raw": obj, "source": "opencode"},
    )]


# ── 工具：构造占位 Event ─────────────────────────────────────────────────────


def _event(event_type: str, session_id: str, data: dict[str, Any]) -> Event:
    """构造 Event（seq=0 占位，timestamp=time.time() 真实；orchestrator 重分配 seq）。

    决策 2（同 claude_translator）：executor/translator 不写 tape，无法知道全局 seq；
    phase 5 ``tape.append`` 重分配。
    """
    return Event(
        seq=0,  # 占位：orchestrator 在 tape.append 时重分配
        type=event_type,  # type: ignore[arg-type]
        timestamp=time.time(),
        session_id=session_id,
        data=data,
    )


# ── reasoning → agent_thinking（整块，--thinking on 时发）────────────────────


def _translate_reasoning(obj: dict, session_id: str) -> list[Event]:
    """reasoning 事件：``part.text`` 整块 → agent_thinking（web-shell-v2 §3.2 B1）。

    opencode 在 ``--thinking`` on 时发 reasoning envelope，``part.text`` 是该 reasoning step
    的完整推理文本（非 token 增量，与 text envelope 同款块级语义）。映射到 claude
    thinking_delta 同款 canonical 事件 ``agent_thinking``，前端琥珀折叠渲染（D2 + §5.3）。

    空文本不发（与 _translate_text 同步，减少噪音）。
    """
    part = obj.get("part")
    if not isinstance(part, dict):
        return []
    text = part.get("text", "")
    if not isinstance(text, str) or text == "":
        return []
    return [_event("agent_thinking", session_id, {"text": text})]


# ── step_start → agent_step_started（liveness 心跳，web-shell-v2 §3.2 B1 / §6）


def _translate_step_start(obj: dict, session_id: str) -> list[Event]:
    """step_start 事件：``part.reason`` 可选 → agent_step_started（web-shell-v2 §3.2 B1）。

    opencode 每个 reasoning step 开头发一条 step_start；``part.reason`` 是上一步结束原因
    （如 "tool-calls" / "stop"），**首个 step_start 无 reason**（消息起点）。

    映射：
      - data = ``{"step_reason": <reason>}``（reason 存在时）
      - data = ``{}``（reason 缺失 / None —— 首个 step_start，无前置 reason）

    前端用作 liveness 心跳（web-shell-v2 §6：进度心跳 = agent_step_started）；reducer no-op
    （不改 RunState，agent 生命周期由 executor 管）。step_reason 透传到前端便于「第 N 步」标记。
    """
    part = obj.get("part")
    if not isinstance(part, dict):
        return []
    reason = part.get("reason")
    data: dict[str, Any] = {}
    if isinstance(reason, str) and reason:
        data["step_reason"] = reason
    return [_event("agent_step_started", session_id, data)]


# ── text → agent_message（整块）──────────────────────────────────────────────


def _translate_text(obj: dict, session_id: str) -> list[Event]:
    """text 事件：``part.text`` 整块 → agent_message。

    opencode 一次发完整文本段（非 token 增量），故整块发；RunAccumulator 在 events 模式下
    把多块拼接成最终答案（result_text）。
    """
    part = obj.get("part")
    if not isinstance(part, dict):
        return []
    text = part.get("text", "")
    if not isinstance(text, str) or text == "":
        return []  # 空文本不发
    return [_event("agent_message", session_id, {"text": text})]


# ── tool_use → agent_tool_call + agent_tool_result（完成时一次发）────────────


def _translate_tool_use(obj: dict, session_id: str) -> list[Event]:
    """tool_use 事件：``part.tool`` / ``part.callID`` / ``part.state``。

    opencode 把工具调用与结果合在一条事件：``state.status`` 从 ``running`` → ``completed``。
    只在 ``completed`` 时发（一次发 call + result），避免半成品调用进事件流。

    - ``agent_tool_call``：``{tool, args=state.input, tool_call_id=part.callID}``
    - ``agent_tool_result``：``{tool_call_id, result=state.output}``（截断到 _TOOL_RESULT_MAX_CHARS）
    """
    part = obj.get("part")
    if not isinstance(part, dict):
        return []
    state = part.get("state")
    if not isinstance(state, dict):
        return []
    if state.get("status") != "completed":
        return []  # 仅完成时发；running/pending 状态的半成品不发
    tool = part.get("tool", "")
    call_id = part.get("callID", "")
    args = state.get("input") or {}
    output = state.get("output")
    result_text = _normalize_tool_output(output)
    if len(result_text) > _TOOL_RESULT_MAX_CHARS:
        result_text = result_text[:_TOOL_RESULT_MAX_CHARS] + "…[truncated]"
    return [
        _event(
            "agent_tool_call",
            session_id,
            {"tool": tool, "args": args, "tool_call_id": call_id},
        ),
        _event(
            "agent_tool_result",
            session_id,
            {"tool_call_id": call_id, "result": result_text},
        ),
    ]


def _normalize_tool_output(raw: Any) -> str:
    """工具 output 归一成字符串（opencode 的 state.output 实测是 str）。

    防御性处理 str / None / 其他类型（与 claude_translator._normalize_tool_result_content 同构）。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    # 结构化 output（dict/list）→ JSON 串（保可读）；罕见，但 opencode 工具可能返回结构化。
    try:
        return json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(raw)


# ── step_finish → agent_usage ────────────────────────────────────────────────


def _translate_step_finish(obj: dict, session_id: str) -> list[Event]:
    """step_finish 事件：``part.tokens`` / ``part.cost`` → agent_usage。

    opencode 每个 reasoning step 结束发一条 step_finish，``part.tokens`` 是该 step 的 token
    统计（实测为累积值），``part.cost`` 是该 step 美元成本。

    映射（对齐 Orca agent_usage 契约 + web-shell-v2 §3.2 B1 reasoning_tokens 扩展）：
      - input_tokens = tokens.input
      - output_tokens = tokens.output
      - cache_tokens = tokens.cache.read（无 cache.read 时 0）
      - cost_usd = part.cost
      - reasoning_tokens = tokens.reasoning（``--thinking`` on 时发；无该字段 = 0）

    旧 tape（无 ``reasoning_tokens`` 字段）由消费侧 ``data.get('reasoning_tokens', 0)`` 兜底，
    **不破坏旧 tape replay**（lossless 扩展只加字段，不改语义）。
    """
    part = obj.get("part")
    if not isinstance(part, dict):
        return []
    tokens = part.get("tokens")
    if not isinstance(tokens, dict):
        return []
    cache = tokens.get("cache") or {}
    return [
        _event(
            "agent_usage",
            session_id,
            {
                "input_tokens": tokens.get("input", 0),
                "output_tokens": tokens.get("output", 0),
                "cache_tokens": cache.get("read", 0),
                "cost_usd": part.get("cost", 0.0),
                "reasoning_tokens": tokens.get("reasoning", 0),
            },
        )
    ]


# ── error → error 事件 ───────────────────────────────────────────────────────


def _translate_error(obj: dict, session_id: str) -> list[Event]:
    """error 事件：``error.data.message`` / ``error.name`` → Orca error 事件。

    opencode 的 error 结构（真实抓取）::

        {"type":"error","error":{"name":"UnknownError","data":{"message":"..."}}}

    ``error.data`` 无结构化 HTTP 码字段（与 claude result 行的 ``api_error_status`` 不同），
    故不设 ``api_error_status``——RunAccumulator.consume_event 抓不到时为 None，executor 错误
    诊断仍能带 message（fail loud 不丢信息）。
    """
    err = obj.get("error")
    if not isinstance(err, dict):
        return []
    data = err.get("data")
    if not isinstance(data, dict):
        data = {}
    message = data.get("message") or err.get("name") or "opencode error"
    return [
        _event(
            "error",
            session_id,
            {
                "kind": "business_agent",
                "error_type": err.get("name") or "OpencodeError",
                "phase": "stream",
                "message": message,
            },
        )
    ]
