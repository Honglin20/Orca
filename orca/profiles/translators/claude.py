"""profiles/translators/claude.py —— claude stream-json 行 → Orca Event（纯函数）。

回答「claude 的 stream-json 一行，怎么变成 Orca 的事件？」：SPEC §3.1 映射表的逐字实现。

**纯函数**（SPEC §7.0 铁律 3）：``claude_translator(line, session_id) -> list[Event]``
  - 无 ``self``、无 I/O、无全局状态、无副作用
  - 同输入两次调用结果相同
  - 不 spawn claude（fixture 驱动测试）

映射决策（SPEC §3.1 / §10，已拍板）：
  1. text / thinking **增量片段**（不拼接），靠 phase 3 reducer 幂等 text@seq 累积。
  2. ``input_json_delta`` **不翻译**（返回 ``[]``）——工具调用只在完整 assistant 消息发，
     避免 JSON 片段拼接出错。
  3. usage **只在 result 发一次**（``agent_usage``，累积值），避免重复计数。
  4. ``result`` 行的最终文本**不在此 emit**——CLIRunner 的 ``on_result`` 钩子把 ``result.result``
     交 executor 做结构化提取（保持 translator 纯函数，SPEC §4.4 关键约束）。
  5. ``result`` + ``is_error=true`` → ``[]``（executor 层据 on_result/退出码处理错误路径）。
  6. 未知 type → ``[]``（debug log 由 CLIRunner 的 json_decode 跳过兜底；translator 这里静默 ``[]``）。

session_id 归属（SPEC §3.2）：translator **接收** session_id（由 ClaudeExecutor 在 exec 入口
生成 ``uuid4().hex``），透传到产出 Event 顶层。不复用 claude 流里的 ``session_id`` 字段
（那是 claude 内部会话 id，避免外部 id 注入 Orca 身份层）。

seq/timestamp 占位（决策 2）：Event 的 ``seq`` 是全局单调递增、由 ``Tape.append`` 写时分配
（见 ``orca.events.bus.emit``）。translator/executor 不写 tape，故**无法知道全局 seq**。
约定：``seq=0`` 占位 + ``timestamp=time.time()`` 真实；**phase 5 orchestrator 在
``tape.append`` 时重分配 seq**——与 phase 3 emit 语义完全一致。

依赖单向：本模块只依赖 ``orca.schema``（Event），不依赖 exec/events.bus/run/compile。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from orca.schema import Event

logger = logging.getLogger(__name__)

# agent_tool_result 的 result 文本截断上限（防异常工具输出喷爆事件流）。
_TOOL_RESULT_MAX_CHARS = 4096


def claude_translator(line: str, session_id: str) -> list[Event]:
    """claude stream-json 一行 → list[Event]（纯函数，SPEC §4.5 / §3.1）。

    按顶层 ``type`` 分派（见模块 docstring 映射决策）。非 JSON 行返回 ``[]``
    （CLIRunner 已对非 JSON 心跳行做 debug log + 跳过；此处防御性 ``[]`` 不抛）。
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        # 正常不该到这（CLIRunner 已过滤），但保持纯函数健壮性：非 JSON → []（不抛）。
        return []
    if not isinstance(obj, dict):
        return []

    top_type = obj.get("type")
    if top_type == "stream_event":
        return _translate_stream_event(obj, session_id)
    if top_type == "assistant":
        return _translate_assistant(obj, session_id)
    if top_type == "user":
        return _translate_user(obj, session_id)
    if top_type == "result":
        return _translate_result(obj, session_id)
    if top_type == "system":
        return _translate_system(obj, session_id)
    # 未知 type（content_block_stop / message_stop 等）→ []（不抛，SPEC §3.1 末行）
    return []


# ── 工具：构造占位 Event ─────────────────────────────────────────────────────


def _event(event_type: str, session_id: str, data: dict[str, Any]) -> Event:
    """构造 Event（seq=0 占位，timestamp=time.time() 真实；orchestrator 重分配 seq）。

    决策 2：executor/translator 不写 tape，无法知道全局 seq；phase 5 ``tape.append`` 重分配。
    """
    return Event(
        seq=0,  # 占位：orchestrator 在 tape.append 时重分配（与 events.bus.emit 一致）
        type=event_type,  # type: ignore[arg-type]
        timestamp=time.time(),
        session_id=session_id,
        data=data,
    )


# ── stream_event 分派（content_block_delta 的三种 delta）──────────────────────


def _translate_stream_event(obj: dict, session_id: str) -> list[Event]:
    """stream_event：按 ``event.type`` + ``delta.type`` 分派（SPEC §3.1）。

    - content_block_delta + text_delta → agent_message（增量片段）
    - content_block_delta + thinking_delta → agent_thinking（增量片段）
    - content_block_delta + input_json_delta → []（不翻译，SPEC §10 决策 2）
    - 其余 stream_event（message_start/stop/content_block_start/stop 等）→ []
    """
    event = obj.get("event")
    if not isinstance(event, dict):
        return []
    if event.get("type") != "content_block_delta":
        return []  # 只翻译 delta；其余 stream_event 子类型不产事件
    delta = event.get("delta")
    if not isinstance(delta, dict):
        return []
    delta_type = delta.get("type")
    if delta_type == "text_delta":
        text = delta.get("text", "")
        if text == "":
            return []  # 空片段不发（减少噪音）
        return [_event("agent_message", session_id, {"text": text})]
    if delta_type == "thinking_delta":
        text = delta.get("thinking", "")
        if text == "":
            return []
        return [_event("agent_thinking", session_id, {"text": text})]
    if delta_type == "input_json_delta":
        # 不翻译（SPEC §10 决策 2）：工具参数增量不拼接，等完整 assistant 消息发 tool_call。
        return []
    return []


# ── assistant（完整回合消息：text / thinking / tool_use）──────────────────────


def _translate_assistant(obj: dict, session_id: str) -> list[Event]:
    """assistant 行：完整 message，遍历 content[]。

    - tool_use block → agent_tool_call（含完整 input，SPEC §3.1）
    - text / thinking block → 不发（增量片段已在 stream_event 发过；完整 block 是冗余回声）

    SPEC §3.1 映射表只把 tool_use 列为 assistant 的产出。完整 text/thinking 是 claude 对
    增量片段的回声（重复发会导致 reducer 重复累积）。
    """
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    events: list[Event] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            tool = block.get("name", "")
            tool_call_id = block.get("id", "")
            args = block.get("input") or {}
            events.append(
                _event(
                    "agent_tool_call",
                    session_id,
                    {"tool": tool, "args": args, "tool_call_id": tool_call_id},
                )
            )
        # text / thinking block：增量已在 stream_event 发，此处跳过（避免重复）
    return events


# ── user（tool_result）───────────────────────────────────────────────────────


def _translate_user(obj: dict, session_id: str) -> list[Event]:
    """user 行：tool_result（工具执行结果，SPEC §3.1）。

    content[] 里 type=tool_result 的 block → agent_tool_result。
    result 文本截断到 ``_TOOL_RESULT_MAX_CHARS``（防异常输出喷爆事件流）。
    """
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    events: list[Event] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_result":
            continue
        tool_call_id = block.get("tool_use_id", "")
        raw_content = block.get("content")
        result_text = _normalize_tool_result_content(raw_content)
        if len(result_text) > _TOOL_RESULT_MAX_CHARS:
            result_text = result_text[:_TOOL_RESULT_MAX_CHARS] + "…[truncated]"
        events.append(
            _event(
                "agent_tool_result",
                session_id,
                {"tool_call_id": tool_call_id, "result": result_text},
            )
        )
    return events


def _normalize_tool_result_content(raw: Any) -> str:
    """tool_result.content 可能是 str / list[{type,text}] / None，归一成单字符串。

    claude 的 tool_result.content 实测有三种形态：
      - str（最常见，如 fixture 行 33 的 "PHASE_B_FIXTURE"）
      - list of {type:"text", text:"..."}（结构化多段）
      - None / 缺失
    归一成 str 以契合 Event.data.result 的扁平契约。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(raw)


# ── result（usage；result 文本走 on_result，不在此 emit）──────────────────────


def _translate_result(obj: dict, session_id: str) -> list[Event]:
    """result 行：只产 agent_usage（若有 usage），result 文本走 CLIRunner.on_result。

    - ``subtype=success`` + ``usage`` 字段 → [agent_usage]（SPEC §3.1 / §3.3）
    - ``is_error=true`` → []（executor 层处理错误路径，SPEC §3.1）
    - cache_tokens = usage.cache_read_input_tokens（SPEC §3.3）
    - cost_usd = 顶层 total_cost_usd（SPEC §3.3）
    - **只发一次**（result 行唯一，避免重复计数）
    """
    if obj.get("is_error") is True:
        # executor 据 on_result 的 result 文本 / exit_code 走 stream 错误路径；translator 不 emit。
        return []
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        # 无 usage 字段（异常 result）→ 不发 agent_usage（usage 聚合归 orchestrator，可缺）。
        return []
    return [
        _event(
            "agent_usage",
            session_id,
            {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_tokens": usage.get("cache_read_input_tokens", 0),
                "cost_usd": obj.get("total_cost_usd", 0.0),
            },
        )
    ]


# ── system（init / status / api_retry）───────────────────────────────────────


def _translate_system(obj: dict, session_id: str) -> list[Event]:
    """system 行：api_retry 发 warning 级 error 事件（可见但不阻断，SPEC §6 / §3.1）。

    - ``subtype=api_retry`` → [error(phase=api_retry, retry_count, wait_seconds, error_status)]
      warning 级。字段名按**真实 claude（2.1.195）协议**：``attempt`` / ``retry_delay_ms`` /
      ``error_status``（HTTP 码）/ ``error``（描述）/ ``max_retries``；旧字段名
      ``retry_count`` / ``wait_seconds`` 作 fallback（向后兼容旧 fixture / 其他 backend）。
    - ``subtype=init`` / ``status`` / ``hook_*`` → []（不产事件）

    实测真实行（2026-07-02，智谱 529 触发抓取）::

        {"type":"system","subtype":"api_retry","attempt":1,"max_retries":10,
         "retry_delay_ms":547.07,"error_status":529,"error":"overloaded"}

    message 优雅降级：字段缺失时省略对应片段，不显示无信息的 ``?`` 占位符。
    """
    subtype = obj.get("subtype")
    if subtype == "api_retry":
        # 真实 claude 发 attempt / retry_delay_ms / error_status；旧名 fallback 兼容。
        attempt = obj.get("attempt", obj.get("retry_count"))
        retry_delay_ms = obj.get("retry_delay_ms")
        wait_seconds = obj.get("wait_seconds")
        if retry_delay_ms is not None and wait_seconds is None:
            try:
                wait_seconds = float(retry_delay_ms) / 1000.0
            except (TypeError, ValueError):
                wait_seconds = None
        error_status = obj.get("error_status")
        error = obj.get("error") or ""
        max_retries = obj.get("max_retries")

        # message 优雅降级：缺失字段不出现「?」（无信息且难看）。
        parts: list[str] = ["claude 限流重试"]
        if attempt is not None:
            parts.append(
                f"第 {attempt}/{max_retries} 次"
                if max_retries is not None
                else f"第 {attempt} 次"
            )
        if wait_seconds is not None:
            parts.append(f"等待 {wait_seconds:.1f}s")
        detail = (
            f"HTTP {error_status} {error}".strip()
            if error_status is not None
            else error
        )
        message = " ".join(parts)
        if detail:
            message = f"{message}（{detail}）"

        return [
            _event(
                "error",
                session_id,
                {
                    "kind": "business_rate_limit",
                    "error_type": "ApiRetry",
                    "phase": "api_retry",
                    "message": message,
                    "retry_count": attempt,
                    "wait_seconds": wait_seconds,
                    "error_status": error_status,
                    "max_retries": max_retries,
                },
            )
        ]
    # init / status / hook_started / hook_response 等 → []（不产事件）
    return []
