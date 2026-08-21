"""orca/iface/in_session/_step_io.py —— in-session 成功/失败信封共享 helper（v5 §8 step 5b）。

回答「daemon 与 cli 两路 in-session 路径如何共享 emit + 信封拼装而不重复？」：本模块是
两路 RPC（``daemon.next`` / ``cli bootstrap|next``）的**共享 IO 边界**——把 ``advance_step``
的 ``StepResult`` 落成 tape 事件 + 构造给宿主的回复信封。失败路径统一以
``InSessionError.error_kind`` 为分类轴（SPEC §2.5），单一真相源（取代 daemon 旧 isinstance 粗分）。

SPEC 2026-08-21-in-session-script-node §2.4/§2.5 扩展：``execute_script_inline``（script 节点
就地内联执行，ns 先行 / nc|nf 各自成批 / 丢弃尾随 error 事件）+ ``advance_with_scripts``
（三入口共享驱动循环：advance → 内联 script 链 → 停在 agent / 终态，D3 max_iter 上限）。

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
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orca.events.bus import EventBus
from orca.run.lifecycle import make_workflow_failed, resolve_max_iter
from orca.run.memory import write_node_memory
from orca.run.step import (
    ERR_INTERNAL_ERROR,
    ERR_SCRIPT_TIMEOUT,
    InSessionError,
    advance_after_script,
    advance_step,
    inline_script_ctx,
    merged_inputs_for,
)

if TYPE_CHECKING:
    from orca.events.tape import Tape
    from orca.schema.workflow import ScriptNode, Workflow

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
    - **``auto_executed``（SPEC 2026-08-21-in-session-script-node §2.7）**：失败信封同样附
      已成功执行的 script 摘要（「不显性化 ≠ 不可见」）。注入通道钉死一种：共享循环把已成功
      条目挂到 ``InSessionError.auto_executed`` 属性，本函数单点读取（bootstrap / next /
      daemon 三个失败出口共用）。零成功条目（属性缺省 / 空）→ 字段省略。

    副作用边界：只 emit + 返信封；marker 清理 / echo / exit 归调用方（cli 顺序
    ``fail_in_session → clear_marker → echo → exit(1)``；daemon 无 marker）。
    """
    error_kind = _classify_in_session_error(exc)
    await _emit_workflow_failed(bus, error_kind, str(exc), node=node)
    reply = {"done": True, "error_kind": error_kind, "reason": f"failed: {exc}"}
    auto_executed = getattr(exc, "auto_executed", None)
    if auto_executed:
        reply["auto_executed"] = list(auto_executed)
    return reply


# ── in-session script 内联执行（SPEC 2026-08-21-in-session-script-node §2.4/§2.5）──


# §2.4.5：auto_executed 摘要的 stdout/stderr tail 截断上限——取**末** N 字符（Unix tail
# 语义：长输出的 verdict/error 在末尾；完整 output 只在 tape，web 可查）。
_AUTO_EXEC_TAIL_LIMIT = 500


def _node_by_name(wf: Workflow) -> dict[str, Any]:
    return {n.name: n for n in wf.nodes}


def _raise_script_failure(node_name: str, nf_data: dict[str, Any]) -> None:
    """§2.6 错误映射：script ``node_failed``（ExecError 路径）→ in-session taxonomy。

    - ``phase == "timeout"`` → ``ERR_SCRIPT_TIMEOUT``（终态 fail loud）。
    - 其余（spawn / render 等）→ ``internal_error``（消息含 phase，终态 fail loud）。
    非零退出码**不走本函数**（ScriptExecutor 正常 nc，业务结果由路由分叉）。
    """
    phase = nf_data.get("phase", "")
    message = nf_data.get("message", "script 执行失败")
    if phase == "timeout":
        raise InSessionError(
            f"script 节点 {node_name!r} 超时失败：{message}",
            error_kind=ERR_SCRIPT_TIMEOUT,
        )
    raise InSessionError(
        f"script 节点 {node_name!r} 执行失败（phase={phase!r}）：{message}",
        error_kind=ERR_INTERNAL_ERROR,
    )


async def execute_script_inline(
    bus: EventBus, tape: Tape, wf: Workflow, node: ScriptNode, *,
    run_id: str, inputs: dict[str, Any], yaml_path: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """§2.4：就地（同一次调用内）执行一个 script 节点，逐批 emit 事件。

    事件流（D1=a，与 headless 逐条 emit 同形）：
      - 首 yield ``node_started`` → **立即** ``emit_batch([ns])``（ns 先落 tape：script 执行
        期间被杀的崩溃态可达，§2.6-C 窗口 i 可恢复）；
      - ``node_completed`` → ``emit_batch([nc])``；
      - ``node_failed``（ExecError 路径）→ ``emit_batch([nf])``，executor 尾随的 ``error``
        事件**丢弃不 emit**（headless 同语义：``executor_adapter`` 在 node_failed 即 raise，
        ``error`` 从不落 tape；reducer 对 ``error`` no-op，丢弃零状态影响）→ 随后按 §2.6
        映射 raise ``InSessionError``。
      - emit 保留 executor 产出的 session_id / timestamp / node / data（session_id 是 web 按
        session 分组的依据）。

    ctx / executor（§2.4.1/§2.4.2）：``RunContext`` 经 ``step.inline_script_ctx`` 公开出口
    构造（``inputs`` = 共享循环的 ``merged_inputs``，**非** CLI 原始 ``--inputs``）；
    ``make_executor(node, runs_dir=tape.path.parent, workflows_root=<yaml_path 父目录>)``，
    ``yaml_path`` None → 回落 ``wf.workflows_root``。

    返回 ``(output, summary)``：``output`` = nc.data.output（``{stdout, stderr, exit_code[,
    json]}``，供调用方 / 测试消费——路由不再经它，nc 已落 tape 由 ``advance_after_script``
    重放取）；``summary`` = §2.4.5 auto_executed 摘要条目 ``{node, exit_code, elapsed,
    stdout_tail, stderr_tail}``（tail = 输出**末** ``_AUTO_EXEC_TAIL_LIMIT`` 字符）。

    批次间崩溃（nc/nf 未落）→ tape 停留 ns(S)，下次不带 --output 的 next 重执行（§2.6-C）。
    """
    from orca.exec.factory import make_executor

    ctx, workflows_root = inline_script_ctx(
        wf, tape, inputs=inputs, run_id=run_id, yaml_path=yaml_path,
    )
    executor = make_executor(
        node, runs_dir=tape.path.parent, workflows_root=workflows_root,
    )

    output: dict[str, Any] | None = None
    elapsed: float | None = None
    failed_data: dict[str, Any] | None = None
    async for event in executor.exec(node, ctx):
        if event.type == "error":
            # executor 尾随 error 事件丢弃（§2.4.3，headless 同语义）。
            continue
        await bus.emit_batch([{
            "type": event.type,
            "data": event.data,
            "node": event.node,
            "session_id": event.session_id,
            "timestamp": event.timestamp,
        }])
        if event.type == "node_completed":
            output = event.data.get("output")
            elapsed = event.data.get("elapsed")
        elif event.type == "node_failed":
            failed_data = event.data
    if failed_data is not None:
        _raise_script_failure(node.name, failed_data)
    if output is None:
        # executor 生命周期违约（无 nc 无 nf，interface 契约外）——fail loud 兜底。
        raise InSessionError(
            f"script 节点 {node.name!r} 的 executor 未产出 node_completed（生命周期违约）",
            error_kind=ERR_INTERNAL_ERROR,
        )
    logger.info(
        "inline script 完成（run=%s, node=%s, exit_code=%s, elapsed=%.3fs）",
        run_id, node.name, output.get("exit_code"), elapsed or 0.0,
    )
    summary = {
        "node": node.name,
        "exit_code": output.get("exit_code"),
        "elapsed": elapsed,
        "stdout_tail": (output.get("stdout") or "")[-_AUTO_EXEC_TAIL_LIMIT:],
        "stderr_tail": (output.get("stderr") or "")[-_AUTO_EXEC_TAIL_LIMIT:],
    }
    return output, summary


async def advance_with_scripts(
    bus: EventBus, tape: Tape, wf: Workflow, *,
    output: str | None, cli_inputs: dict[str, Any], run_id: str,
    prompts_dir: Path | None = None, yaml_path: str | None = None,
    host_session: str | None = None, project_root: Path | None = None,
    no_memory: bool = False,
    on_script_chain_start: Callable[[], None] | None = None,
) -> tuple[Any, dict[str, Any], list[dict[str, Any]]]:
    """§2.5 驱动循环（cli bootstrap / cli next / daemon next 三入口共享）。

    流程：
      1. ``merged_inputs = merged_inputs_for(tape, wf, cli_inputs)``（M1 单一口径：tape 真相源
         + CLI override + default 填充；向 execute_script_inline / advance_after_script /
         resolve_max_iter 全程透传同一值）。
      2. ``advance_step``（现行为）→ ``apply_step_result``（批 emit）。
      3. ``on_script_chain_start``（D4 round-3：仅 cli ``next`` 注入的守护前置 ensure 回调；
         首步 advance 判出 ``node_kind=="script"`` 且非终态时、进 script 循环**前**调用一次。
         bootstrap / daemon 不传 → 零行为）。
      4. script 循环（D3 上限 = ``resolve_max_iter(wf, merged_inputs)``，防 script 路由成环
         撞 flock 死锁）：``execute_script_inline``（ns / nc|nf 各自成批 emit）→
         ``advance_after_script``（rt + ns(next) | workflow_completed）→ ``apply_step_result``。
         循环停在 agent 节点（返其 prompt 交付）或终态。

    失败路径（§2.5）：循环内任一步 raise ``InSessionError`` → 把已成功条目挂到异常的
    ``auto_executed`` 属性后 re-raise → 调用方现有 ``except InSessionError`` 走
    ``fail_in_session`` 单点（emit workflow_failed + 失败信封注入 auto_executed）。
    node_failed 不重试、不 recoverable（§0 非目标）。

    返回 ``(result, reply, auto_executed)``：``result`` = 循环**最终** StepResult（合规计数 /
    marker RMW / env 写均取它判定，§2.5）；``reply`` = 末次 ``apply_step_result`` 的基础信封
    （daemon 直接返它 + auto_executed；cli 自建富信封可丢弃）；``auto_executed`` = 按执行
    顺序的成功摘要条目（零条 → 空列表，调用方据此省略字段，§2.7）。
    """
    merged_inputs = merged_inputs_for(tape, wf, cli_inputs)
    result = advance_step(
        tape, wf, output=output, inputs=merged_inputs, run_id=run_id,
        prompts_dir=prompts_dir, yaml_path=yaml_path, host_session=host_session,
        project_root=project_root, no_memory=no_memory,
    )
    reply = await apply_step_result(
        bus, result, wf=wf, run_id=run_id, no_memory=no_memory, project_root=project_root,
    )
    auto_executed: list[dict[str, Any]] = []
    nodes = _node_by_name(wf)
    if (not result.done) and result.node_kind == "script":
        if on_script_chain_start is not None:
            on_script_chain_start()
        # 非法 iterations（显式声明却非数值）→ 包成 InSessionError 走 workflow_failed 信封，
        # 与 headless 同契约（orchestrator 捕获 → workflow_failed，``lifecycle.resolve_max_iter``
        # fail loud 注释明示）——否则 ValueError 裸穿 except InSessionError 面（tape 悬留、无信封）。
        try:
            max_scripts = resolve_max_iter(wf, merged_inputs)
        except (ValueError, TypeError) as e:
            raise InSessionError(
                f"max_iter 解析失败（inputs.iterations 非法）：{e}",
                error_kind=ERR_INTERNAL_ERROR,
            ) from e
        while (not result.done) and result.node_kind == "script":
            try:
                if len(auto_executed) >= max_scripts:
                    # D3：内联 script 执行数撞上限（疑似路由成环）——fail loud 终态。
                    raise InSessionError(
                        "内联 script 执行数撞 max_iter 上限（疑似路由成环）",
                        error_kind=ERR_INTERNAL_ERROR,
                    )
                _output, summary = await execute_script_inline(
                    bus, tape, wf, nodes[result.node], run_id=run_id,
                    inputs=merged_inputs, yaml_path=yaml_path,
                )
                auto_executed.append(summary)
                result = advance_after_script(
                    tape, wf, result.node, inputs=merged_inputs, run_id=run_id,
                    prompts_dir=prompts_dir, project_root=project_root,
                    no_memory=no_memory, yaml_path=yaml_path,
                )
                reply = await apply_step_result(
                    bus, result, wf=wf, run_id=run_id, no_memory=no_memory,
                    project_root=project_root,
                )
            except InSessionError as e:
                # §2.7：失败信封同样报备已成功条目（挂属性 → fail_in_session 单点读取）。
                if auto_executed:
                    e.auto_executed = list(auto_executed)
                raise
    return result, reply, auto_executed
