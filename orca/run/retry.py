"""retry.py —— 节点级自动重试 transient claude 子进程失败（SPEC §9.5）。

回答「agent node 偶发失败（API 429 / 子进程崩 / timeout）怎么办？」：按节点声明的
``RetryPolicy`` 自动重试，带 backoff + jitter，对用户可观测（retry_started / retry_succeeded /
retry_exhausted 事件写 Tape）。

核心循环（SPEC §9.5.4 / §9.5.6）::

    for attempt in 1..max_attempts:
        收 executor 的事件流（逐个 emit 落 Tape）
        terminal = node_completed | node_failed
        if node_completed:
            if attempt > 1: emit retry_succeeded
            return output
        if node_failed:
            if data["was_interrupted"]:   # 用户主动中断
                raise                      # 短路退出，不进 retry 判定
            if error_type not in retry_on: # 不在白名单（如 result_parse 配置错）
                raise                      # fail loud，不重试
            if attempt < max_attempts:
                delay = _compute_delay(policy, attempt)
                emit retry_started(attempt+1, error_type, delay)
                await asyncio.sleep(delay)
                continue
            # 用完
            emit retry_exhausted
            raise last_error

设计要点（SPEC §9.5.2 error_type 对齐表）：
  - **was_interrupted 短路**：用户 Ctrl+G 触发的中断不属于 transient error，**优先于**
    retry_on 白名单判定短路退出（防御性 ``.get(..., False)``，缺字段不崩）。
  - **retry_on 白名单**：``node_failed.data["error_type"]`` 必须命中才重试。配置错
    （result_parse）/ schema 错等 fail loud 不重试。
  - **max_attempts=1** ⇒ 等价无 retry（循环只跑一次，无 retry_started / retry_exhausted）。
  - **DRY**：``_compute_delay`` 是唯一的 delay 计算点（constant / linear / exponential +
    cap + jitter）。

依赖单向：本模块依赖 ``orca.exec``（Executor 类型 + ExecError）+ ``orca.schema``
（RetryPolicy / Event / AgentNode）+ ``orca.events``（EventBus）。**不**依赖 ``iface/``、
**不**依赖 ``run.orchestrator``（orchestrator 调本模块，反向不行）。

与 validator（phase 11 §9.6.5，wave 3）的边界：本函数是「执行一次 + 重试 transient 失败」
的 primitive。validator 失败（error_type=``validator_failed``）若在 ``retry_on`` 白名单内，
由本函数按同一 loop 重试；validator 自身的 max_retries 折进 ``RetryPolicy.max_attempts``
计数（SPEC §9.6.5：单一 retry loop，不双层嵌套）。wave 3 接 validator 时，调用方在 executor
外层包 validator，validator 失败 emit ``node_failed{error_type:"validator_failed"}`` 即可
复用本 loop。
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Any

from orca.exec.error import ExecError

if TYPE_CHECKING:
    from orca.events.bus import EventBus
    from orca.exec.context import RunContext
    from orca.exec.interface import Executor
    from orca.schema import AgentNode, Event, RetryPolicy

logger = logging.getLogger(__name__)

# jitter 幅度：delay × [1 - JITTER, 1 + JITTER]（SPEC §9.5.2 ±20%）。
_JITTER_FRACTION = 0.2

# ClaudeExecutor 实际产出的 error_type（via phase_to_error_type）→ retry_on Literal 取值
# 的映射（SPEC §9.5.2 error_type 对齐表）。retry_on 用语义短名（spawn_error/timeout/...），
# executor 用 phase 派生名（CliExitNonZero/ExecTimeout/...）—— 两者命名空间不同，本表是
# 桥接层，让用户写 ``retry_on: [spawn_error]`` 时真能命中 ``error_type=CliExitNonZero``。
#
# http_429 优先于 api_error 判定（result_text 含限流关键词时归 http_429，否则归 api_error）——
# 让用户能精确「只重试限流，fail-fast 通用 API 错」。validator_failed / 自定义 error_type
# 透传（不在表里的原样返回，让 retry_on=[validator_failed] / 未来新类型直接命中）。
_API_ERROR_KEYS = ("api_error", "api_retry", "rate_limit", "overloaded", "429", "529")
_ERROR_TYPE_TO_RETRY_KEY: dict[str, str] = {
    "CliExitNonZero": "spawn_error",
    "ExecTimeout": "timeout",
    "ClaudeStreamError": "api_error",
}


def _classify_for_retry(error_type: str, err_data: dict[str, Any]) -> str:
    """把 executor 产出的 ``error_type`` 归类为 ``retry_on`` 白名单取值（SPEC §9.5.2）。

    Args:
        error_type: ``node_failed.data["error_type"]``（executor 写入，如 CliExitNonZero）。
        err_data: ``node_failed.data`` 全量（http_429 判定需读 message 文本关键词）。

    Returns:
        retry_on 白名单取值（spawn_error / timeout / api_error / http_429 /
        validator_failed / 原样透传）。

    归类规则（SPEC §9.5.2 对齐表）：
      - CliExitNonZero → spawn_error
      - ExecTimeout → timeout
      - ClaudeStreamError → 若 message 含限流关键词 → http_429；否则 → api_error
      - validator_failed / 不在表里的 → 原样透传（让 retry_on 直接命中或精确不命中）
      - 空字符串 → 空字符串（不在白名单，不重试）
    """
    mapped = _ERROR_TYPE_TO_RETRY_KEY.get(error_type)
    if mapped is None:
        # 不在映射表：validator_failed / 自定义 error_type / 空串 —— 原样透传。
        return error_type
    if mapped == "api_error":
        # ClaudeStreamError 细分：限流关键词命中 → http_429，否则 api_error。
        message = str(err_data.get("message", "")).lower()
        if any(k in message for k in _API_ERROR_KEYS):
            return "http_429"
    return mapped


async def execute_with_retry(
    executor: Executor,
    node: AgentNode,
    ctx: RunContext,
    bus: EventBus,
) -> tuple[Any, list[Event]]:
    """执行 agent node，按 ``node.retry`` 自动重试 transient 失败（SPEC §9.5.4）。

    Args:
        executor: ``make_executor(node)`` 产出的 executor（每次 attempt 复用同一实例；
            executor 本身无状态，重 spawn 由其内部 CLIRunner 完成）。
        node: 被执行的 agent node（``node.retry`` 必须非 None —— 调用方负责判定）。
        ctx: 当前 RunContext（每次 attempt 复用；retry 不修改 ctx）。
        bus: EventBus（所有事件 emit 落 Tape，retry_* 事件也经此写 Tape）。

    Returns:
        ``(final_output, all_events)``：``final_output`` 是最终成功 attempt 的
        ``node_completed.data["output"]``；``all_events`` 是所有 attempt 产出的事件
        全集（含中间失败的 node_failed —— 它们已逐个 emit 落 Tape，此处也返回供调用方
        断言/诊断）。

    Raises:
        ExecError: 任一情况：
          - 首次及后续 attempt 的 ``node_failed`` 且不在重试路径（was_interrupted 短路 /
            error_type 不在 retry_on 白名单 / 重试用尽）。**re-raise 最后一次** ExecError
            （透传其 phase / error_type / message），让上层 orchestrator 走 workflow_failed。
          - executor 违约生命周期（既无 completed 也无 failed，复用 execute_and_emit 的
            fail loud 语义）。
    """
    policy = node.retry
    if policy is None:
        # 防御性：调用方（orchestrator._dispatch）负责判定 retry is not None 才进本函数。
        # 走到这里是调用方 bug → fail loud（不静默退化为不重试）。
        raise ValueError(
            f"execute_with_retry 收到 node.retry=None（node={node.name!r}）；"
            "调用方应走 execute_and_emit 既有路径，不调本函数。"
        )

    all_events: list[Event] = []
    last_error: ExecError | None = None

    for attempt in range(1, policy.max_attempts + 1):
        # 收本 attempt 的事件流，逐个 emit 落 Tape（retry 期间所有 attempt 的流式事件
        # 都进 Tape，用户可观测「第 N 次尝试的 agent_message / tool_call」）。
        terminal_completed: Event | None = None
        terminal_failed: Event | None = None
        async for event in executor.exec(node, ctx):
            all_events.append(event)
            await bus.emit(
                event.type,
                event.data,
                node=event.node,
                session_id=event.session_id,
            )
            if event.type == "node_completed":
                terminal_completed = event
            elif event.type == "node_failed":
                terminal_failed = event

        # ── 成功路径 ──────────────────────────────────────────────────────────
        if terminal_completed is not None:
            if attempt > 1:
                # 重试后成功才发 retry_succeeded（首次成功不发，避免噪声）。
                await bus.emit(
                    "retry_succeeded",
                    {"attempt_total": attempt, "node": node.name},
                    node=node.name,
                )
            return terminal_completed.data.get("output"), all_events

        # ── 失败路径 ──────────────────────────────────────────────────────────
        if terminal_failed is None:
            # 生命周期违约（executor 既没 completed 也没 failed）—— fail loud。
            # 复用 execute_and_emit 的语义：构造 ExecError 抛出，不静默继续。
            logger.error(
                "executor 未按生命周期契约产出 node_completed/node_failed"
                "（node=%s, attempt=%d）",
                node.name,
                attempt,
            )
            raise ExecError(
                phase="node_failed",
                message=(
                    f"executor 执行 node {node.name!r} 未产出 "
                    "node_completed/node_failed（生命周期违约）"
                ),
                node=node.name,
            )

        err_data = terminal_failed.data
        # was_interrupted 短路（SPEC §9.5.2）：用户 Ctrl+G 主动中断不属于 transient error。
        # 防御性 .get(default=False)：node_failed 缺此字段不崩 retry 逻辑。
        if err_data.get("was_interrupted", False):
            raise _exec_error_from_failed(err_data, node.name)

        # error_type 是 executor 写入的原始值（CliExitNonZero / ExecTimeout / ...），
        # 透传到 retry_started/retry_exhausted 事件供诊断。retry_key 是归类后的 retry_on
        # 白名单取值（spawn_error / timeout / ...），用于白名单匹配（SPEC §9.5.2 对齐表）。
        error_type = err_data.get("error_type", "")
        retry_key = _classify_for_retry(error_type, err_data)
        last_error = _exec_error_from_failed(err_data, node.name)

        # retry_on 白名单过滤：retry_key 不在白名单 → fail loud 不重试（如 result_parse
        # 配置错、schema 错 —— 重试也是错，浪费 token）。
        if retry_key not in policy.retry_on:
            raise last_error

        # 还有 attempt 额度 → emit retry_started + sleep + continue。
        if attempt < policy.max_attempts:
            delay = _compute_delay(policy, attempt)
            phase = err_data.get("phase") if isinstance(err_data, dict) else None
            kind_value = _kind_from_error_type(error_type, phase).value
            await bus.emit(
                "retry_started",
                {
                    "attempt": attempt + 1,
                    "max_attempts": policy.max_attempts,
                    "error_type": error_type,
                    "kind": kind_value,
                    "delay_seconds": delay,
                    "node": node.name,
                    "layer": layer_for_kind_from_value(kind_value),
                    "reason": str(err_data.get("message", "")),
                    "next_retry_at": _next_retry_at(delay),
                },
                node=node.name,
            )
            await asyncio.sleep(delay)
            continue

        # 用完仍失败 → emit retry_exhausted + raise（让上层 orchestrator 走 workflow_failed）。
        phase = err_data.get("phase") if isinstance(err_data, dict) else None
        kind_value = _kind_from_error_type(error_type, phase).value
        await bus.emit(
            "retry_exhausted",
            {
                "attempts": policy.max_attempts,
                "last_error_type": error_type,
                "last_kind": kind_value,
                "node": node.name,
                "layer": layer_for_kind_from_value(kind_value),
            },
            node=node.name,
        )
        raise last_error

    # 不可达：for 循环必经 return / raise 退出。防御性 fail loud（不应触发）。
    raise ExecError(
        phase="node_failed",
        message=f"retry loop 异常退出（node={node.name!r}, max_attempts={policy.max_attempts}）",
        node=node.name,
    )


def _exec_error_from_failed(err_data: dict[str, Any], node_name: str) -> ExecError:
    """从 ``node_failed`` 事件的 data 构造 ExecError（透传 phase / error_type / message）。

    薄包装 ``ExecError.from_failed_data``（DRY：与 ``execute_and_emit`` 共享同一构造点，
    避免 retry loop 与既有路径逻辑漂移）。保留本函数是为了 retry loop 调用点可读
    （命名达意「从 failed 事件构造 error」），不直接暴露 classmethod 调用。
    """
    return ExecError.from_failed_data(err_data, node=node_name)


def _kind_from_error_type(error_type: str, phase: "str | None" = None) -> "ErrorKind":
    """从 executor 旧 ``error_type`` 字符串反推 ErrorKind（retry emit 用，DRY）。

    优先级（避免「retry_on 字面量当 error_type」场景误判为 UNKNOWN）：
      1. ``phase`` 非空 → ``_DEFAULT_KIND_FOR_PHASE[phase]``（phase 是 executor 写的权威诊断）
      2. ``error_type`` 在 ``_LEGACY_ERROR_TYPE_TO_KIND`` → 用之
      3. ``error_type`` 是 retry_on 字面量（spawn_error / timeout / ...）→ 经
         ``_RETRY_ON_TO_KINDS`` 反查（SPEC §4.5 retry_on → kind 子集映射）
      4. 都无 → UNKNOWN
    """
    from orca.exec.error_kinds import (
        _DEFAULT_KIND_FOR_PHASE,
        _LEGACY_ERROR_TYPE_TO_KIND,
        ErrorKind,
    )
    from orca.exec.retry import _RETRY_ON_TO_KINDS

    if phase:
        return _DEFAULT_KIND_FOR_PHASE.get(phase, ErrorKind.UNKNOWN)
    if error_type in _LEGACY_ERROR_TYPE_TO_KIND:
        return _LEGACY_ERROR_TYPE_TO_KIND[error_type]
    # retry_on 字面量（如 "spawn_error"）当 error_type 写进 data 时，反查为单元素 set 取唯一值
    for retry_key, kinds in _RETRY_ON_TO_KINDS.items():
        if error_type == retry_key and kinds:
            return next(iter(kinds))
    return ErrorKind.UNKNOWN


def _layer_for_error(error_type: str, phase: "str | None" = None) -> str:
    """从 error_type 派生 layer（retry emit ``layer`` 字段用，ADR §4.5）。

    **single source of truth**：layer 从 kind 派生（``Error.layer_from_kind`` 同款逻辑），
    不再走 retry_on → layer 的并行映射表（避免 kind/layer 不一致，E2E 闭环审视 Defect B）。
    """
    from orca.exec.retry import layer_for_kind
    return layer_for_kind(_kind_from_error_type(error_type, phase))


def layer_for_kind_from_value(kind_value: str) -> str:
    """kind 字符串值 → layer（DRY：与 ``Error.layer_from_kind`` 同款派生逻辑）。

    复用 ``orca.exec.retry.layer_for_kind`` 但接受字符串值（retry emit 已 coerce
    kind 为 .value 字符串，避免重复 coerce）。
    """
    from orca.exec.error_kinds import ErrorKind
    from orca.exec.retry import layer_for_kind
    try:
        return layer_for_kind(ErrorKind(kind_value))
    except ValueError:
        return "business"  # unknown 默认 business（旧 tape 兼容）


def _next_retry_at(delay_seconds: float) -> "str | None":
    """计算 next_retry_at ISO 时间戳（ADR §4.5）；delay=0 → None。"""
    if delay_seconds <= 0:
        return None
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()


def _compute_delay(policy: RetryPolicy, attempt: int) -> float:
    """按 backoff 策略算本次 attempt 失败后到下次 attempt 的 sleep 时长（SPEC §9.5.4）。

    Args:
        policy: 节点的 RetryPolicy。
        attempt: 当前 attempt 编号（1-based）。即「第 attempt 次失败后等多久」。

    Returns:
        delay 秒数（float）。已应用 ``max_delay_seconds`` 上限 + 可选 ±20% jitter。

    策略（SPEC §9.5.6）：
      - ``constant``：每次都 ``initial_delay_seconds``。
      - ``linear``：``initial_delay_seconds × attempt``（1s, 2s, 3s, ...）。
      - ``exponential``：``initial_delay_seconds × 2^(attempt-1)``（1s, 2s, 4s, 8s, ...）。

    全部先 cap 到 ``max_delay_seconds``，再按 ``jitter`` 决定是否加 ±20% 抖动
    （防雪崩：同一批 429 不会同时重试）。``jitter=False`` 路径用于测试确定性断言。
    """
    if policy.backoff == "constant":
        base = policy.initial_delay_seconds
    elif policy.backoff == "linear":
        base = policy.initial_delay_seconds * attempt
    else:  # "exponential"
        base = policy.initial_delay_seconds * (2 ** (attempt - 1))

    base = min(base, policy.max_delay_seconds)

    if policy.jitter:
        # uniform[1 - JITTER, 1 + JITTER]：attempt=1 失败后等 [0.8, 1.2]×base。
        factor = 1.0 + random.uniform(-_JITTER_FRACTION, _JITTER_FRACTION)
        return base * factor
    return base
