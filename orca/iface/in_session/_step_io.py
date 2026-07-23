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
from pathlib import Path
from typing import Any

from orca.events.bus import EventBus
from orca.run.lifecycle import make_workflow_failed
from orca.run.memory import write_node_memory

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


def merge_recoverable_envelope(reply: dict[str, Any], result: Any) -> None:
    """SPEC 2026-07-23 §4.1(a)：``result.recoverable`` → 把 recoverable 信封字段合并进 reply（in-place）。

    单一真相源：cli ``next``（自建 reply + 加 prompt 指针 + 驱动协议）与 daemon ``next``
    （直接返 ``apply_step_result`` 的 reply）两路共用，避免字段名/字段集漂移（DRY）。
    字段：``recoverable:true, error_kind, retry_count, retry_budget, hint``（``reason`` 由调用方
    按既有逻辑已加；``done:false`` 由 ``result.done`` 决定，不在本 helper 范围）。

    compliance-warn（``result.warn``）**不**经此 helper —— warn 是 cli 层 marker 计数注解
    （SPEC §4.1(b)），daemon 无 marker / 无 compliance，warn 信封由 cli ``next`` 单独拼。
    """
    if not getattr(result, "recoverable", False):
        return
    reply["recoverable"] = True
    if getattr(result, "error_kind", None) is not None:
        reply["error_kind"] = result.error_kind
    if getattr(result, "retry_count", None) is not None:
        reply["retry_count"] = result.retry_count
    if getattr(result, "retry_budget", None) is not None:
        reply["retry_budget"] = result.retry_budget
    if getattr(result, "hint", None):
        reply["hint"] = result.hint


async def apply_step_result(
    bus: EventBus, result: Any,
    *,
    wf: Any | None = None,
    run_id: str | None = None,
    no_memory: bool = False,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """成功路径：``emit_batch(result.emits)`` + 构造回复信封 ``{done, node?, prompt?, reason?}``。

    - **emit**：整批一次 write（``emit_batch`` 原子化；``emits=[]`` 时 ``emit_batch`` no-op）。
    - **信封**：``{done}`` + 可选 ``node`` / ``prompt`` / ``reason``（取自 ``StepResult``）。
      调用方（cli）可在此基础上追加 ``prompt_file`` / 驱动协议等富字段。

    【node-memory】``wf`` / ``run_id`` / ``project_root`` 给定且 ``not no_memory`` 时,
    emit_batch 成功后遍历 ``result.emits``,对每条 ``node_completed`` 事件按 ``e.node`` 名查
    wf 中的 node 对象,``memory=True`` 则覆盖写 ``<project_root>/.orca/memory/<wf>/<node>.md``
    (SPEC §3.2)。best-effort:写失败不阻断 run(``write_node_memory`` 内部 warn)。
    调用方未传 wf(如 daemon 单测)→ 跳过记忆写入,行为与改动前一致(回归红线)。

    副作用边界：只 emit + (可选)写记忆 + 返信封；marker / echo / exit 归调用方。
    """
    await bus.emit_batch(_emits_to_event_datas(result.emits))
    # SPEC §3.2:写记忆仅在 node_completed 触发(node_failed/workflow_failed/workflow_cancelled
    # 不触发;含 workflow_completed 出口前最后一条 node_completed)。``no_memory=True`` 整 run
    # 跳过(测试隔离 / 用户显式禁)。
    if wf is not None and not no_memory:
        _write_memories_for_emits(wf, result.emits, run_id=run_id or "", project_root=project_root)
    reply: dict[str, Any] = {"done": result.done}
    if result.node:
        reply["node"] = result.node
    if result.prompt:
        reply["prompt"] = result.prompt
    if result.reason:
        reply["reason"] = result.reason
    # SPEC 2026-07-23 §4.1(a)：recoverable 信封字段（daemon.next 直接返此 reply → 自动复用）。
    merge_recoverable_envelope(reply, result)
    # SPEC 2026-07-23 §4.2：升格终态（``result.done=True`` + ``result.error_kind``，连续 recoverable
    # 撞上限 emit workflow_failed）→ surface ``error_kind`` 给信封消费方。cli 在自家 next 命令
    # 显式补（``cli.py`` 末段 ``elif result.done and result.error_kind``）；daemon 直接返此 reply，
    # 若不在此补，daemon 升格终态信封会丢 ``error_kind``（cli/daemon parity bug，code-reviewer
    # 🟡#1）。``setdefault`` 不覆盖调用方已显式赋的值（防御性，与 cli 路径重叠时无副作用）。
    if getattr(result, "done", False) and getattr(result, "error_kind", None) is not None:
        reply.setdefault("error_kind", result.error_kind)
    return reply


def _write_memories_for_emits(
    wf: Any, emits: list, *, run_id: str, project_root: Path | None,
) -> None:
    """SPEC §3.2 helper:遍历 emits,对 ``memory=True`` 的 node_completed 写记忆 MD。

    抽出来是为复用单一真相源(避免 daemon / cli 两路各写一遍 —— SPEC §6 「daemon 同步」
    守门)。node 对象按 ``e.node`` 名从 ``wf.nodes`` 查;查不到(并行组名 / orphan) → 跳过。
    """
    if project_root is None:
        # in-session 路径下 CLI 恒传 Path.cwd();此处的 None 兜底只用于 daemon / 单测。
        return
    # name → node 索引(wf.nodes 内嵌 foreach body 无 name,顶层 nodes 均有名)。
    nodes_by_name = {getattr(n, "name", ""): n for n in getattr(wf, "nodes", [])}
    for e in emits:
        if getattr(e, "type", None) != "node_completed":
            continue
        node_name = e.node
        if not node_name:
            continue
        node_obj = nodes_by_name.get(node_name)
        if node_obj is None:
            continue
        if not getattr(node_obj, "memory", False):
            continue
        output = (e.data or {}).get("output")
        write_node_memory(wf, node_obj, output, run_id=run_id, project_root=project_root)


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
