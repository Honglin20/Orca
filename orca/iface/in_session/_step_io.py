"""orca/iface/in_session/_step_io.py —— in-session 成功/失败信封共享 helper（v5 §8 step 5b）。

回答「daemon 与 cli 两路 in-session 路径如何共享 emit + 信封拼装而不重复？」：本模块是
两路 RPC（``daemon.next`` / ``cli bootstrap|next``）的**共享 IO 边界**——把 ``advance_step``
的 ``StepResult`` 落成 tape 事件 + 构造给宿主的回复信封。失败路径统一以
``InSessionError.error_kind`` 为分类轴（SPEC §2.5），单一真相源（取代 daemon 旧 isinstance 粗分）。

副作用边界（spec-reviewer issue8，钉死）：helper **只做 emit + 返信封**。marker 清理 /
echo / exit 归调用方——cli 顺序 ``emit → clear_marker → echo → exit(1)``，daemon 无 marker。

字段名契约（spec-reviewer B4/B7，极易写错）：
  - **tape event data 字段 = ``kind``**（``lifecycle.make_workflow_failed`` 写，不变）。
  - **信封新字段 = ``error_kind``**。两者携带同一值（``InSessionError.error_kind``）。

依赖单向：本模块依赖 ``events.bus``（EventBus）+ ``run.lifecycle`` + ``run.step``——iface 层
调 run/events，符合 schema→compile→exec→run→iface 铁律。不反向依赖 cli/daemon。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from orca.events.bus import EventBus
from orca.run.lifecycle import make_workflow_failed

logger = logging.getLogger(__name__)


def _classify_in_session_error(exc: Exception) -> str:
    """读 ``exc.error_kind``（SPEC §2.5 taxonomy）；缺省 → ``internal_error``（兜底，fail loud）。

    用 ``getattr``（非 isinstance）：``InSessionError`` 显式携带 ``error_kind``；属性缺失
    → 兜底 ``internal_error``。分类轴单一（取代 daemon 旧 isinstance 二分把所有 InSessionError
    塌缩成单一粗粒度值丢精度的反模式）。

    分类由 step.py 各 raise 处显式传 ``error_kind=ERR_*`` 携带（类型安全：加新 kind = 加
    常量 + raise 处传，不必维护关键词表）。

    部署边界：当前 in-session 调用点（``daemon.next`` / cli ``bootstrap|next``）均
    ``except InSessionError`` 窄捕获（与原 daemon 行为一致，非回归）；非 InSessionError（如
    cli marker 写失败的 OSError）经字面 error_kind 路径调 ``_emit_workflow_failed``，不进本函数。
    无头 daemon 是否应宽捕获兜底（crash 时 emit workflow_failed 避免留腐败 tape）是独立
    follow-up，不在 step 5b 范围（plan §4.2 明定窄捕获）。
    """
    return getattr(exc, "error_kind", None) or "internal_error"


def _emits_to_event_datas(emits: list) -> list[dict]:
    """``advance_step`` 返的 ``list[Emit]`` → ``emit_batch`` 入参形态（吸收自原 cli.py 内联）。

    ``emit_batch`` 入参是不含 seq 的 event 字段 dict（同 ``EventBus.emit`` 内部 event_data）：
    ``{"type", "data", "node", "timestamp"}``。整批单次 write 原子化（B1，反 daemon 旧逐条 emit
    的 SIGTERM 半截 tape 风险——spec-reviewer Q1 裁定「batch emit 真活」）。
    """
    return [
        {
            "type": e.type,
            "data": e.data,
            "node": e.node,
            "timestamp": time.time(),
        }
        for e in emits
    ]


async def apply_step_result(bus: EventBus, result: Any) -> dict[str, Any]:
    """成功路径：``emit_batch(result.emits)`` + 构造回复信封 ``{done, node?, prompt?, reason?}``。

    - **emit**：整批一次 write（``emit_batch`` 原子化；``emits=[]`` 时 ``emit_batch`` no-op）。
    - **信封**：``{done}`` + 可选 ``node`` / ``prompt`` / ``reason``（取自 ``StepResult``）。
      调用方（cli）可在此基础上追加 ``prompt_file`` / 驱动协议等富字段。

    副作用边界：只 emit + 返信封；marker / echo / exit 归调用方。
    """
    await bus.emit_batch(_emits_to_event_datas(result.emits))
    reply: dict[str, Any] = {"done": result.done}
    if result.node:
        reply["node"] = result.node
    if result.prompt:
        reply["prompt"] = result.prompt
    if result.reason:
        reply["reason"] = result.reason
    return reply


async def _emit_workflow_failed(
    bus: EventBus, error_kind: str, message: str, node: str | None = None,
) -> None:
    """落 ``workflow_failed`` 终态（单真相源），吞错仅 log（tape 可能已坏，仍要让调用方返信封）。

    ``error_kind`` 写入 tape ``data.kind``（字段名 ``kind`` 不变，``lifecycle.make_workflow_failed``
    权威字段）+ ``data.error_type``（读兼容期）。本函数供 ``fail_in_session``（异常驱动）与
    cli 合规计数 / marker 写失败（字面 error_kind，非 InSessionError）两类路径共用。

    日志：入口用 ``logger.warning``（非 ``exception``）——本函数被合规计数正常流路径调用时
    无 active exception，``logger.exception`` 会附 ``NoneType: None`` 假栈。真正的 emit 失败
    在下方 except 用 ``logger.exception`` 记真栈。
    """
    logger.warning("emit workflow_failed (error_kind=%s): %s", error_kind, message)
    try:
        t, d = make_workflow_failed(error_kind, message, node=node)
        await bus.emit(t, d, node=node)
    except Exception:
        logger.exception("emit workflow_failed 也失败（tape 可能已坏）")


async def fail_in_session(
    bus: EventBus, exc: Exception, node: str | None = None,
) -> dict[str, Any]:
    """失败路径：classify ``error_kind`` + emit ``workflow_failed`` + 返错误信封。

    - ``error_kind = _classify_in_session_error(exc)``（SSOT：读 ``InSessionError.error_kind``，
      取代 daemon 旧 isinstance 粗分；非 InSessionError 兜底 ``internal_error``）。
    - emit ``workflow_failed``：tape ``data.kind = error_kind``（字段名 ``kind``，不变）；
      emit 本身吞错仅 log（tape 可能已坏，仍要返信封给调用方）。
    - 信封：``{done:True, error_kind, reason:"failed: <msg>"}``（**新字段 ``error_kind``**，
      供主 session/监控拿结构化分类；与 tape ``data.kind`` 同值，**字段名不同**——B4/B7 陷阱）。

    副作用边界：只 emit + 返信封；marker 清理 / echo / exit 归调用方（cli 顺序
    ``fail_in_session → clear_marker → echo → exit(1)``；daemon 无 marker）。
    """
    error_kind = _classify_in_session_error(exc)
    await _emit_workflow_failed(bus, error_kind, str(exc), node=node)
    return {"done": True, "error_kind": error_kind, "reason": f"failed: {exc}"}
