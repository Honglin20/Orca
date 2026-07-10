"""classifier.py —— 错误分类纯函数（双入口，phase-11 SPEC §2.2）。

回答「exception / backend output → 哪个 ErrorKind？」：first-match-wins 规则表（SPEC §2.2
17 行），single source of truth。

**双入口**（v2.1 闭环审视 Q4，不用 type-union 反模式）：

  - ``classify_exception(exc, profile=None, ctx=None) -> Error``
    exception 输入分类（ExecError / WorkflowAborted / MaxIterationsError / RouteError /
    ConnectionError / BrokenPipeError / TimeoutError / OSError）

  - ``classify_backend_output(raw: dict, profile=None, ctx=None) -> Error``
    backend 原始产出分类（子进程 CompletedProcess 转 dict / stream-json 解析结果 / dict）

**调度顺序**（闭环审视 Q6，单一分类权威）：
  1. ``profile.classify_backend_error(raw) -> ErrorKind | None`` 若返非 None → 用之
     （profile 钩子优先，禁止抛错，内部 try/except → None）
  2. 否则走通用规则表（first-match-wins，自上而下）
  3. 都未命中 → ``UNKNOWN``（raw 必须保留）

**关键约束**（SPEC §2.2 / 铁律 3）：
  - 纯函数（同样输入同样输出）。禁止发事件 / 改状态。
  - **禁止**字符串匹配 message 文本决定分类（AST 守门见 phase-11 §6 验收 6）。
  - first-match-wins：规则表自上而下，命中即返。

依赖单向：本模块依赖 ``orca.exec.error_kinds`` + ``orca.exec.result``（Error）；
profile / ctx 仅 TYPE_CHECKING（duck typing，避免硬依赖 profiles 包）。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Optional

from orca.exec.error_kinds import ErrorKind, _DEFAULT_RETRYABLE
from orca.exec.result import Error

if TYPE_CHECKING:
    from orca.exec.context import RunContext
    from orca.profiles.base import CliProfile

logger = logging.getLogger(__name__)


def _with_retryable(err: Error, retryable: bool) -> Error:
    """DRY：返回 err 的副本，retryable 显式覆盖（dataclasses.replace，SPEC §2.1）。"""
    return replace(err, retryable=retryable)


# ── 编排层 exception 提示（SPEC §4.2 / 决策 1.2）─────────────────────────────
# WorkflowAborted / MaxIterationsError / RouteError 是 ExecError 子类（kind 固定），
# 走 0 号 ``isinstance(exc, ExecError)`` 分支，``Error.from_exec_error(exc)`` 直接投影。
# WorkflowTerminated 保留独立 signal（非 ExecError 子类），orchestrator 显式翻译，
# 不经 classifier。


def classify_exception(
    exc: BaseException,
    profile: "CliProfile | None" = None,
    ctx: "RunContext | None" = None,
) -> Error:
    """exception → Error 分类（SPEC §2.2 规则表）。

    Args:
        exc: 任意 BaseException。常见类型：ExecError（含 WorkflowAborted/
            MaxIterationsError/RouteError 子类）/ ConnectionError / BrokenPipeError
            / TimeoutError / OSError。
        profile: 可选，提供 ``classify_backend_error`` 钩子（不抛错，吞 → None）。
        ctx: 可选诊断上下文（当前仅诊断，不参与分类决策）。

    Returns:
        Error 信封（kind + message + raw）。UNKNOWN 带 raw（exception dict）。
    """
    # 0. ExecError 直接投影（kind 已是权威，不重新分类；ADR §4.1 决策 1.4）
    #    WorkflowAborted/MaxIter/RouteError 都走此分支（ExecError 子类）。
    from orca.exec.error import ExecError
    if isinstance(exc, ExecError):
        return Error.from_exec_error(exc)

    # 1. profile 钩子（不抛错；吞 → 跳过）
    profile_kind = _call_profile_classifier(exc, profile, ctx)
    if profile_kind is not None:
        return _build_error(profile_kind, str(exc), raw={"exception": type(exc).__name__})

    raw = {"exception": type(exc).__name__, "args": list(exc.args)}

    # 2. 通用规则表（first-match-wins，SPEC §2.2 isinstance 行）
    if isinstance(exc, TimeoutError):
        return _build_error(ErrorKind.TRANSPORT_TIMEOUT, str(exc), raw=raw)

    if isinstance(exc, BrokenPipeError):
        return _build_error(ErrorKind.PROTOCOL_MCP, str(exc), raw=raw)

    if isinstance(exc, ConnectionError):
        return _build_error(ErrorKind.TRANSPORT_NETWORK, str(exc), raw=raw)

    if isinstance(exc, OSError):
        # OSError 含 ConnectionError / BrokenPipeError 子类；前面分支先命中
        return _build_error(ErrorKind.TRANSPORT_NETWORK, str(exc), raw=raw)

    # 4. 未命中 → UNKNOWN（raw 完整保留）
    return _build_error(ErrorKind.UNKNOWN, str(exc), raw=raw)


def classify_backend_output(
    raw: dict,
    profile: "CliProfile | None" = None,
    ctx: "RunContext | None" = None,
) -> Error:
    """backend 原始产出 → Error 分类（SPEC §2.2 规则表）。

    Args:
        raw: backend 原始产出（dict）。CompletedProcess 转 ``{exit_code, stdout, stderr}``；
             stream-json 解析结果保留完整 dict。
        profile: 可选，提供 ``classify_backend_error`` 钩子。
        ctx: 可选诊断上下文。

    Returns:
        Error 信封。
    """
    # 1. profile 钩子（优先；不抛错）
    profile_kind = _call_profile_classifier(raw, profile, ctx)
    if profile_kind is not None:
        return _build_error(profile_kind, _message_from_raw(raw), raw=raw)

    # 2. 通用规则表（first-match-wins，SPEC §2.2 行 1-17）
    exit_code = raw.get("exit_code")
    if isinstance(exit_code, int):
        # 行 1：OOM / SIGKILL / SIGSEGV
        if exit_code in (137, 139) or exit_code == -9:
            # OOM 显式覆盖 retryable=True（SPEC §2.1 备注）
            return _with_retryable(
                _build_error(
                    ErrorKind.TRANSPORT_PROCESS, _message_from_raw(raw), raw=raw,
                ),
                retryable=True,
            )
        # 行 2：非零非 1（业务报错）
        if exit_code != 0 and exit_code != 1:
            return _build_error(
                ErrorKind.BUSINESS_AGENT, _message_from_raw(raw), raw=raw,
            )

    # 3. http_status 429（rate limit）
    http_status = raw.get("http_status")
    if http_status == 429 or raw.get("rate_limit"):
        return _with_retryable(
            _build_error(
                ErrorKind.BUSINESS_RATE_LIMIT, _message_from_raw(raw), raw=raw,
            ),
            retryable=True,
        )

    # 4. is_error + tool_use_id → BUSINESS_AGENT（stream 行 6）
    if raw.get("is_error") or raw.get("tool_use_id"):
        return _build_error(ErrorKind.BUSINESS_AGENT, _message_from_raw(raw), raw=raw)

    # 5. phase 字段（若 raw 带）
    phase = raw.get("phase")
    if phase == "stream":
        # 行 7：非 is_error + 无 tool_use_id → PROTOCOL_PARSE
        return _build_error(ErrorKind.PROTOCOL_PARSE, _message_from_raw(raw), raw=raw)
    if phase in ("schema", "validator"):
        return _build_error(ErrorKind.PROTOCOL_SCHEMA, _message_from_raw(raw), raw=raw)
    if phase in ("config", "render"):
        return _build_error(ErrorKind.BUSINESS_CONFIG, _message_from_raw(raw), raw=raw)
    if phase == "interrupted":
        return _build_error(ErrorKind.BUSINESS_GATE, _message_from_raw(raw), raw=raw)
    if phase == "result_parse":
        return _build_error(ErrorKind.PROTOCOL_PARSE, _message_from_raw(raw), raw=raw)
    if phase == "timeout":
        return _build_error(ErrorKind.TRANSPORT_TIMEOUT, _message_from_raw(raw), raw=raw)

    # 6. 未命中 → UNKNOWN（raw 必须保留）
    return _build_error(ErrorKind.UNKNOWN, _message_from_raw(raw), raw=raw)


# ── helpers ─────────────────────────────────────────────────────────────────


def _call_profile_classifier(
    payload: Any,
    profile: "CliProfile | None",
    ctx: "RunContext | None",
) -> Optional[ErrorKind]:
    """调 ``profile.classify_backend_error(payload) -> ErrorKind | None`` 钩子。

    profile 钩子禁止抛错（CLAUDE.md 报错处理：profile 钩子不吞下层错误，但自身异常
    应降级为 None，让通用规则表兜底）。异常 → log + None。
    """
    if profile is None:
        return None
    classify_fn = getattr(profile, "classify_backend_error", None)
    if classify_fn is None:
        return None
    try:
        result = classify_fn(payload, ctx) if ctx is not None else classify_fn(payload)
    except Exception as e:  # noqa: BLE001 - profile 钩子降级，不让其崩溃分类路径
        # SPEC §2.2 第 1 条「profile 钩子禁止抛错」+ CLAUDE.md「重试/异常必须用户可见」：
        # 不让 profile 实现 bug 阻断分类路径，但 log warning 让 bug 可观测（不静默吞）。
        logger.warning(
            "profile %s.classify_backend_error 抛异常（已降级 None，走通用规则表）：%s",
            type(profile).__name__, e, exc_info=True,
        )
        return None
    if result is None:
        return None
    if isinstance(result, ErrorKind):
        return result
    # profile 返回字符串值（容错）
    try:
        return ErrorKind(result)
    except ValueError:
        return None


def _build_error(kind: ErrorKind, message: str, *, raw: dict | None = None) -> Error:
    """构造 Error（补默认 retryable）。UNKNOWN 必须带 raw（Error.__post_init__ 守）。"""
    if kind == ErrorKind.UNKNOWN and raw is None:
        raw = {"_note": "UNKNOWN kind, no raw provided"}
    return Error(
        kind=kind,
        message=message or "",
        raw=raw,
        retryable=_DEFAULT_RETRYABLE.get(kind, False),
    )


def _message_from_raw(raw: dict) -> str:
    """从 raw dict 提取人读 message（DRY）。"""
    msg = raw.get("message") or raw.get("stderr") or raw.get("error")
    if isinstance(msg, str) and msg:
        return msg
    return f"backend error ({raw.get('error_type') or raw.get('name') or 'unknown'})"
