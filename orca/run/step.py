"""step.py —— in-session shell 的单步推进纯函数（ADR v2 方案 E）。

回答「宿主每完成一个节点，Orca 怎么确定性地推下一步、并 emit 与 drive_loop
对齐的事件？」。供 ``orca in-session serve`` daemon 调用；**drive_loop 零改动**
（方案 E：用户底线「不影响现有」优先于 DRY；drive_loop 内联 emit 与本模块短期
不 DRY，登记 known-debt，独立 phase 处理）。

复用（零新增逻辑路径，全是已有纯函数 / staticmethod）：
  - ``replay_state`` —— reducer，唯一状态派生（铁律 1 读路径统一）
  - ``Orchestrator._next_node_for_resume`` —— 路由求值（同 drive_loop 的路由逻辑）
  - ``_outputs_acc_from_state`` —— raw output → ``{"output": raw}`` 包装形态转换
  - ``render_prompt`` —— 节点 prompt 渲染（同 drive_loop / executor 用）
  - ``lifecycle.make_workflow_started/completed`` —— workflow 级事件构造

事件序列与 drive_loop 逐 seq 对齐（每节点 ``ns → nc → rt → ns(next)``）；G2 回归
（daemon 跑某 wf 的 tape vs ``orca run`` 跑同 wf 的 tape，``(type, node, 关键字段)``
对齐）是行为正确性的守门。

不在此模块（属 daemon / phase SPEC）：tape 写入（flock / 半写恢复 / pid 探活）、
MCP 传输、CLI 命令面。本模块只给「读 tape 现状 → 决定要 emit 什么 + 返回什么」
的纯决策，IO 由调用方（daemon）执行。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jsonschema

from orca.events.replay import _replay_state_and_inputs
from orca.exec.error import ExecError
from orca.exec.render import render_prompt, render_template
from orca.run.lifecycle import (
    make_workflow_completed,
    make_workflow_failed,
    make_workflow_started,
    now_monotonic,
)
from orca.run.memory import inject_memory_prompt
from orca.run.orchestrator import Orchestrator
from orca.run.resume import _outputs_acc_from_state

if TYPE_CHECKING:
    from orca.events.tape import Tape
    from orca.exec.context import RunContext
    from orca.schema.workflow import Workflow

logger = logging.getLogger(__name__)

END = "$end"

# in-session 失败 taxonomy error_type 常量（SPEC §2.5）。``InSessionError.error_kind`` 显式
# 携带，cli.py ``_classify_in_session_error`` 直读——取代脆弱的消息子串匹配（类型安全；
# 加新 kind = 加常量 + raise 处传，不必维护 cli.py 的关键词表）。
# 注：``subagent_compliance`` 由 cli.py marker 计数器路径直接 emit（不经 InSessionError）。
ERR_OUTPUT_SCHEMA_MISMATCH = "output_schema_mismatch"
ERR_RENDER_ERROR = "render_error"
ERR_UNSUPPORTED_NODE_KIND = "unsupported_node_kind"
ERR_STATE_CORRUPT = "state_corrupt"
ERR_INTERNAL_ERROR = "internal_error"

# SPEC 2026-07-23-in-session-error-management §2 P4：同节点连续 recoverable 失败上限。
# 撞上限 → 升格 workflow_failed（防死循环）。对齐哨兵 MAX_ASK=3，不可配（YAGNI）。
_RECOVERABLE_ESCALATE_AT = 3


class InSessionError(Exception):
    """in-session 推进中的非法状态（fail loud）：状态腐败 / 不支持的节点类型等。

    ``error_kind`` 显式携带 SPEC §2.5 taxonomy 分类（默认 ``internal_error`` 兜底），
    供 cli.py ``_classify_in_session_error`` 直读。每个 raise 处传对应 ``ERR_*`` 常量。
    """

    def __init__(self, message: str, *, error_kind: str = ERR_INTERNAL_ERROR):
        super().__init__(message)
        self.error_kind = error_kind


class RecoverableInSessionError(InSessionError):
    """可恢复的节点产出错误（SPEC 2026-07-23 §3：v1 仅 ``output_schema_mismatch``）。

    与 plain ``InSessionError`` 的区别：``advance_step`` 在 ``output is not None`` 分支
    **自捕** 此子类 —— 不 re-raise，而是 emit ``[node_failed, node_started]`` 重 arm 同节点、
    返 ``StepResult(recoverable=True)``（run 存活，决策权交主 session）。连续 N 次未通过才
    升格 ``workflow_failed``（终态）。

    ``error_kind`` 恒为 ``output_schema_mismatch``（仅此 kind 可恢复；render_error / state_corrupt /
    unsupported_node_kind / internal_error 仍 plain ``InSessionError`` = irrecoverable）。

    因属 ``InSessionError`` 子类，``cli.next`` 的 ``except InSessionError`` 仍能兜底捕获（防御：
    正常路径 advance_step 自捕不外抛，但若未来调用方直接调 ``_parse_output`` 仍走旧 fail 路径）。
    """

    def __init__(self, message: str):
        super().__init__(message, error_kind=ERR_OUTPUT_SCHEMA_MISMATCH)


@dataclass
class Emit:
    """一条待 emit 的事件指令（type, data, node）—— daemon 逐条 ``bus.emit``。"""

    type: str
    data: dict[str, Any]
    node: str | None = None


@dataclass
class StepResult:
    """一次 advance 推进的结果：要 emit 的事件 + 给宿主的回复。"""

    emits: list[Emit] = field(default_factory=list)
    done: bool = False
    node: str | None = None          # 下一个要让宿主执行的节点（done=False 时）
    prompt: str | None = None        # inline 回退：该节点渲染后的完整 prompt（compact 模式为 None）
    prompt_file: str | None = None   # compact：渲染后 prompt 落盘路径（指针交付，主 session 只过指针）
    resources_root: str | None = None  # compact：agent 资源目录绝对路径（指针里附给子代理按需 Read）
    reason: str | None = None        # done=True 时的终止原因 / 错误说明
    # SPEC 2026-07-23-in-session-error-management §4.1：recoverable / warn 信封字段。
    # recoverable=True → 节点产出不合 schema 但 run 存活（重 arm 同节点，主 session 反馈重派）；
    # warn=True → compliance 计数达 warn 阈值（cli 层注解，advance_step 本身不置位）。
    recoverable: bool = False
    warn: bool = False
    retry_count: int | None = None     # 本次是第几次重试（1-based）
    retry_budget: int | None = None    # 剩余重试次数（N - retry_count）
    error_kind: str | None = None      # recoverable/warn 的 error_kind（output_schema_mismatch / subagent_compliance）
    hint: str | None = None            # 给主 session 的恢复指引


def _running_node(state: Any) -> str | None:
    """reducer state 中唯一 ``running`` 节点（在途、started 未 completed）。

    in-session 顺序推进，同一时刻至多一个 running；>1 即状态腐败，fail loud。
    """
    running = [n for n, s in state.node_status.items() if s == "running"]
    if len(running) > 1:
        raise InSessionError(
            f"tape 中存在多个 running 节点 {running}（状态腐败 / 并发调用）",
            error_kind=ERR_STATE_CORRUPT,
        )
    return running[0] if running else None


def _node_by_name(wf: Workflow) -> dict[str, Any]:
    return {n.name: n for n in wf.nodes}


def consecutive_fail_count(tape: Tape, node: str) -> int:
    """当前节点在 tape 末尾的**连续** recoverable 失败次数（SPEC §4.3，AC9）。

    派生谓词（E1 钉死）：计 ``node_failed(node)``；遇 ``node_completed(任意节点)`` 即重置为 0。
    v1 顺序单 running 节点下「任意节点 nc」与「当前节点 nc」等价（DAG 前向），谓词显式写
    「任意节点」以备未来并行。

    实现选**正向扫描 + reset-on-nc**（与 SPEC §4.3 描述的「从末尾向前扫」数学等价）：
    正向遍历遇 nc(any) 把计数归零、遇 nf(node) 累加，遍历结束时的计数 = 末尾连续 streak 长度。
    正向单次遍历无需物化 ``reversed(list(...))``，O(n) 常量空间，结果与反向扫到首个 nc 停止一致。

    不进 reducer fold（``events/replay.py`` 零改边界）；``advance_step`` 在 recoverable 决策点
    调它。不入 marker（避免 desync，与「marker 只 3 字段」铁律一致）—— SSOT 在 tape。
    """
    count = 0
    for event in tape.replay():
        if event.type == "node_completed":
            count = 0
        elif event.type == "node_failed" and event.node == node:
            count += 1
    return count


def _build_ctx(wf: Workflow, outputs_acc: dict[str, Any], inputs: dict[str, Any],
               run_id: str) -> RunContext:
    from orca.exec.context import RunContext

    return RunContext(
        inputs=inputs, outputs=outputs_acc, run_id=run_id, task=None,
    )


def _parse_output(raw: str, node: Any) -> Any:
    """按 node.output_schema 解析宿主回捕的文本输出；无 schema 视为裸字符串。

    声明了 output_schema 时做两段校验（确定性，非 LLM validator）：
      1. JSON 解析失败 → ``output_schema_mismatch``（非 JSON）。
      2. ``jsonschema.validate`` 字段校验（缺失/类型错）→ ``output_schema_mismatch``；
         schema 自身畸形（用户 YAML 写错）→ 同样 fail loud（不脏崩溃，D-v8.x-2）。
    缺字段在此被抓（早于下游 render 的 UndefinedError），给清晰错误而非脏崩溃。
    子代理"自我纠正"发生在它自己 turn 内（rendered prompt 文件写明 schema 要求）；
    Orca 层产不对就 fail loud，不做重试循环（in-session 主 session 自己当判官）。
    """
    schema = getattr(node, "output_schema", None)
    if not schema:
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # 结构化预期但拿到非 JSON —— recoverable（SPEC 2026-07-23 §3）：advance_step
        # 自捕 RecoverableInSessionError → 重 arm 同节点 + 回反馈信封，主 session 重派。
        raise RecoverableInSessionError(
            f"节点 {node.name!r} 声明了 output_schema 但宿主输出非 JSON：{raw[:80]!r}",
        )
    # 字段校验：jsonschema>=4.0（pyproject 已声明，exec/ result_extractor 同款用法）。
    # 必须同时 catch SchemaError：compile 层不校验 output_schema 形状，用户 YAML 写错
    # （如 required 非字符串、type 拼错）会让 validate 抛 SchemaError（非 ValidationError
    # 子类）——只 catch ValidationError 会逃逸成脏崩溃（review 🔴，D-v8.x-2 初衷）。
    # 三处均 recoverable（SPEC §3）：产出不合 schema 由主 session 反馈子代理重派修正。
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as e:
        path = ".".join(str(p) for p in e.absolute_path) or "<root>"
        raise RecoverableInSessionError(
            f"节点 {node.name!r} 输出不满足 output_schema：{e.message}（路径 {path}）",
        )
    except jsonschema.SchemaError as e:
        # schema 自身畸形 → 同归 recoverable（error_kind=output_schema_mismatch，消息区分语义）。
        raise RecoverableInSessionError(
            f"节点 {node.name!r} 的 output_schema 自身畸形：{e.message}",
        )
    return parsed


def _render_or_fail(node: Any, ctx: Any) -> str:
    """渲染节点 prompt，``ExecError``（Jinja UndefinedError / 模板错）→ ``InSessionError``。

    包 ``render_prompt`` 的目的：让"下游 prompt 引用上游缺失字段"这类 render 错走
    cli.py 既有的 ``except InSessionError`` 干净路径（emit workflow_failed + 清 marker），
    而非作为 ``ExecError`` 逃逸成脏崩溃（tape 悬挂、卡死）。
    """
    try:
        return render_prompt(node, ctx)
    except ExecError as e:
        raise InSessionError(
            f"渲染节点 {node.name!r} prompt 失败（可能是上游 output 缺字段或模板错）：{e}",
            error_kind=ERR_RENDER_ERROR,
        ) from e


def _write_prompt_file(prompts_dir: Path, node_name: str, rendered: str) -> Path:
    """compact：把渲染后的 prompt 原子写到 ``<prompts_dir>/<node_name>.md``。

    loop 时同节点覆盖（最新即所用；逐次历史在 tape）。``tmp + os.replace`` 原子写
    （与 marker / install_cmds ``_atomic_write_with_backup`` 同模式）。OSError → fail loud。
    """
    prompts_dir = Path(prompts_dir)
    final = prompts_dir / f"{node_name}.md"
    tmp = final.with_name(f".{final.name}.tmp.{os.getpid()}")
    try:
        prompts_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(rendered, encoding="utf-8")
        os.replace(tmp, final)
    except OSError as e:
        # 清残 tmp（write 后 replace 前失败会留残文件；missing_ok=True 兼容 mkdir 阶段未创建）。
        tmp.unlink(missing_ok=True)
        raise InSessionError(
            f"写节点 {node_name!r} 的 compact prompt 文件失败：{e}",
            error_kind=ERR_INTERNAL_ERROR,
        ) from e
    return final


def _deliver(
    node: Any, ctx: Any, prompts_dir: Path | None,
    *,
    wf: Any | None = None,
    project_root: Path | None = None,
    no_memory: bool = False,
) -> tuple[str | None, str | None, str | None]:
    """渲染 prompt 并按交付模式产出 ``(prompt, prompt_file, resources_root)``。

    - ``prompts_dir`` 给定（compact，生产路径）：写文件，返 ``(None, <path>, resources_root)``。
    - ``prompts_dir=None``（inline 回退，单测决策逻辑用）：返 ``(rendered, None, None)``。

    【node-memory】``wf`` / ``project_root`` / ``no_memory`` 给定且 ``node.memory=True`` 时,
    在渲染后、写文件前调 ``inject_memory_prompt`` 把上一轮 MD body + 复用协议拼到 rendered
    末尾(SPEC §4.1)。三 kwarg 默认值保 ``_deliver(node, ctx, prompts_dir)`` 旧调用形态不破
    (单测 inline 路径 / 非记忆节点零行为变更)。
    """
    rendered = _render_or_fail(node, ctx)
    if getattr(node, "memory", False) and not no_memory and wf is not None and project_root is not None:
        rendered = inject_memory_prompt(node, wf, rendered, project_root=project_root)
    if prompts_dir is not None:
        path = _write_prompt_file(prompts_dir, node.name, rendered)
        return None, str(path), getattr(node, "resources_root", None)
    return rendered, None, None


def _final_outputs(
    wf: Workflow, outputs_acc: dict[str, Any], inputs: dict[str, Any], run_id: str,
) -> dict[str, Any]:
    """workflow_completed 的 outputs。

    ``wf.outputs`` 声明了输出模板 → 渲染（与 ``Orchestrator._evaluate_outputs`` 同源：
    ``render_template`` + ``_build_ctx``，tape 是 inputs/outputs 真相源）；无声明 →
    返各节点 raw output 集合（旧行为）。

    已知 DRY 债（同 ``_resolve_inputs``）：渲染逻辑短期与 orchestrator 内联一份，
    不抽共享函数以免动 drive_loop。phase-14 的 ``end_route.output``（命中 ``$end`` 那条
    route 的独立输出变换）in-session 暂不支持 —— 此处只求 ``wf.outputs``（覆盖绝大多数
    workflow；per-route 变换留 follow-up）。
    """
    templates = getattr(wf, "outputs", None)
    if not templates:
        return {node: acc.get("output") for node, acc in outputs_acc.items()}
    ctx = _build_ctx(wf, outputs_acc, inputs, run_id)
    try:
        return {key: render_template(tpl, ctx) for key, tpl in templates.items()}
    except ExecError as e:
        # 渲染失败（上游 output 缺字段 / 模板语法错）→ fail loud 统一走 InSessionError
        # （cli 层 except 捕获 → emit workflow_failed），不静默返 {}（鲁棒性底线）。
        # 精确 catch ExecError（render_template 仅抛此），与同文件 ``_render_or_fail`` 一致。
        raise InSessionError(
            f"渲染 workflow outputs 模板失败（可能上游 output 缺字段或模板错）：{e}",
            error_kind=ERR_RENDER_ERROR,
        ) from e


def _resolve_inputs(wf: Workflow, inputs: dict[str, Any] | None) -> dict[str, Any]:
    """应用 wf.inputs 的 default（mirror ``Orchestrator.__init__`` 的 default 填充）。

    方案 E：daemon 独立实现，不改 drive_loop；此处的 default 填充逻辑与
    orchestrator 内联一份短期不 DRY（known-debt）。必填缺失由后续 render 的
    StrictUndefined fail loud 兜底。
    """
    resolved = dict(inputs or {})
    for name, idef in (wf.inputs or {}).items():
        if name not in resolved and getattr(idef, "default", None) is not None:
            resolved[name] = idef.default
    return resolved


def _node_failed_data(exc: RecoverableInSessionError) -> dict[str, Any]:
    """构造 ``node_failed`` 的 4-字段 data（SPEC §4.2 E6，inline，不加 lifecycle helper）。

    复用 executor 形态 ``{kind, error_type, message, phase}``（``exec/interface.py:15``），
    但 ``kind`` 值是 in-session 专属字符串（``output_schema_mismatch``），**故意不**是
    ``ErrorKind`` 枚举成员 —— 失败本体不同（in-session 是宿主协同错误，executor 是后端协议
    错误），不强求共享枚举。reducer 对 node_failed 只置 ``node_status[node]=failed``，不读
    这些字段（纯可观测）。
    """
    return {
        "kind": ERR_OUTPUT_SCHEMA_MISMATCH,
        "error_type": ERR_OUTPUT_SCHEMA_MISMATCH,
        "message": str(exc),
        "phase": "output_validation",
    }


def _recover_step_result(
    tape: Tape, wf: Workflow, exc: RecoverableInSessionError, pending: str,
    state: Any, inputs: dict[str, Any], rid: str,
    prompts_dir: Path | None, project_root: Path | None, no_memory: bool,
) -> StepResult:
    """recoverable 自恢复（SPEC §4.2）：emit ``[node_failed, node_started]`` 重 arm 同节点；
    连续 ``_RECOVERABLE_ESCALATE_AT`` 次未通过 → 升格 ``workflow_failed``。

    计数语义（SPEC §4.3）：``count = consecutive_fail_count(tape, pending)`` 是**本次失败
    落 tape 前**的前序连续失败数；本次是第 ``count+1`` 次（1-based ``retry_count``）。
      - ``count+1 < N``：emit ``[nf, ns]`` + 重渲染 prompt + 返 ``StepResult(recoverable=True,
        retry_count=count+1, retry_budget=N-(count+1))``（run 存活，marker 不清）。
      - ``count+1 >= N``：升格——emit 顺序 ``nf → ns → workflow_failed``（E8 钉死：第 N 次失败
        真实记录后再终态，保 ``count 重建 = retry_count`` 不变量）；返 ``done=True``。

    重渲染用 ``_outputs_acc_from_state(state)``：pending 未 completed（emit 的是 nf 而非 nc），
    其坏 output 不进 context → 上下文与首次 arm 一致（确定性把手，P3）。

    Corner case（code-reviewer 🟢）：``count+1 < N`` 分支调 ``_deliver`` 重渲染 prompt 时，
    若节点 prompt 自身有 Jinja 错 / 引用上游缺字段（render_error），``_render_or_fail`` 抛
    ``InSessionError(render_error)`` —— 已构的 ``[nf, ns]`` emits 被丢弃，异常透传到 cli
    ``except InSessionError`` → ``fail_in_session`` emit ``workflow_failed(render_error)``。
    即「合法升格被 render_error 短路为 irrecoverable fail loud」（render_error 本质 wf-author
    bug，重跑也修不了；SPEC §3 明示 render_error 全 irrecoverable）。此场景下本次 nf 未落 tape，
    count 不变量不成立 —— 可接受，因 render_error 永远进 irrecoverable 终态、不再循环。
    """
    nodes = _node_by_name(wf)
    count = consecutive_fail_count(tape, pending)
    this_attempt = count + 1
    emits: list[Emit] = [
        Emit("node_failed", _node_failed_data(exc), node=pending),
        Emit("node_started", {"node": pending}, node=pending),
    ]

    if this_attempt >= _RECOVERABLE_ESCALATE_AT:
        # 升格（E8）：先 emit 本次 [nf, ns]，再追加 workflow_failed（tape 记录第 N 次真实失败）。
        reason = (f"consecutive recoverable exhausted: 节点 {pending!r} 连续 "
                  f"{this_attempt} 次产出不合 schema")
        t, d = make_workflow_failed(ERR_OUTPUT_SCHEMA_MISMATCH, reason, node=pending)
        emits.append(Emit(t, d))
        logger.warning(
            "节点 %s 连续 %d 次 recoverable 失败，升格 workflow_failed（run=%s）",
            pending, this_attempt, rid,
        )
        return StepResult(emits=emits, done=True, reason=reason,
                          error_kind=ERR_OUTPUT_SCHEMA_MISMATCH)

    # 未升格 → 重 arm：重渲染 prompt（与正常 next 同形交付，compact/inline 由 prompts_dir 决定）。
    ctx = _build_ctx(wf, _outputs_acc_from_state(state), inputs, rid)
    prompt, prompt_file, rroot = _deliver(
        nodes[pending], ctx, prompts_dir,
        wf=wf, project_root=project_root, no_memory=no_memory,
    )
    retry_budget = _RECOVERABLE_ESCALATE_AT - this_attempt
    hint = (
        f"把上面的 reason 反馈给执行本节点的子代理，重派它产出修正后的 output，"
        f"再 orca next --output（剩余 {retry_budget} 次重试机会）"
    )
    logger.info(
        "节点 %s recoverable 失败（第 %d/%d 次），重 arm（run=%s）",
        pending, this_attempt, _RECOVERABLE_ESCALATE_AT, rid,
    )
    return StepResult(
        emits=emits, done=False, node=pending,
        prompt=prompt, prompt_file=prompt_file, resources_root=rroot,
        recoverable=True, retry_count=this_attempt, retry_budget=retry_budget,
        error_kind=ERR_OUTPUT_SCHEMA_MISMATCH, reason=str(exc), hint=hint,
    )


def advance_step(
    tape: Tape,
    wf: Workflow,
    *,
    output: str | None = None,
    inputs: dict[str, Any] | None = None,
    run_id: str | None = None,
    elapsed: float = 0.0,
    prompts_dir: Path | None = None,
    yaml_path: str | None = None,
    host_session: str | None = None,
    project_root: Path | None = None,
    no_memory: bool = False,
) -> StepResult:
    """单步推进（决策 + recoverable 自恢复；emit-only——不写 tape，但走既有 ``_deliver``
    写 prompt 文件，与 pre-SPEC 行为一致，非新副作用）。

    调用契约（宿主侧）：
      - 首次：``advance_step()`` 无 output → 返回 entry 节点 prompt（emit
        ``workflow_started`` + ``node_started(entry)``）。
      - 完成一节点：``advance_step(output=<宿主执行结果>)`` → emit
        ``node_completed(pending, output)`` + ``route_taken`` + ``node_started(next)``
        （或到 ``$end`` 时 ``workflow_completed``），返回 next prompt / done。
      - 重复无 output 调用（宿主丢失 prompt）：幂等重发 pending prompt，不 emit。

    幂等 / 终态：
      - ``state.status`` 为终态（completed/failed/cancelled）→ 直接 ``{done, reason}``，不 emit。
      - ``output`` 给出但无 running 节点 → ``InSessionError``（状态腐败，fail loud）。

    v1 范围：仅 agent 节点（宿主 subagent 执行模型）；parallel / foreach / gate /
    ask_user 由 compile validator 在更上层 fail loud 拒绝（D2）。
    ``elapsed`` 由 daemon 传真实 workflow 总耗时（M5：不撒谎）。
    ``prompts_dir`` 给定时走 compact 交付（渲染后 prompt 落盘、StepResult.prompt_file 指针）；
    None 时 inline 回退（StepResult.prompt 全文，单测决策逻辑用）。

    ``host_session``（host-session-binding v2）：宿主 session id，**仅 ``state.status=="pending"``
    首节点分支透传给 ``make_workflow_started``**（写入 tape 的归属字段，单一真相源）。next
    路径（非 pending）**不传**——``workflow_started`` 在 bootstrap 已 emit，next 不重发
    （emit 真链 §4.1：host_session 经 lifecycle←step←cli 三点穿，不在 cli.py emit）。

    【node-memory】``project_root`` / ``no_memory`` 透传 ``_deliver``:节点 ``memory=True`` 且
    ``not no_memory`` 时,渲染后注入「上一轮记忆 + 复用协议」(SPEC §4.1)。``project_root=None``
    时即使 ``memory=True`` 也不注入(回归旧形态,保单测 inline 路径不破)。
    """
    # SPEC §3 O1a（包 P3）：单次遍历 tape 既 fold RunState 又抽 workflow_started.data.inputs
    # （reducer 只存 workflow_name、不存 inputs → 必须在同一次遍历里顺手抽）。
    # tape 是 inputs 真相源：next 不传 --inputs 时从 tape 恢复（deterministic —— 模型不必
    # 每步重传，且修掉非 entry 节点 {{ inputs.* }} 依赖 CLI 重传的隐患）。bootstrap 首调时
    # tape 无 workflow_started → inputs 返 {} → 自然 fallback 到 CLI 传入的 inputs。
    # 与 Orchestrator resume（_inputs_from_tape）同源（后者现为薄封装调同一 reducer 路径）。
    state, tape_inputs = _replay_state_and_inputs(tape)
    merged = {**tape_inputs, **(inputs or {})}  # CLI override 罕见但保留兼容
    inputs = _resolve_inputs(wf, merged)
    rid = run_id or getattr(tape, "run_id", "") or ""

    # 1. 已终态（重复调用 / crash 后重启撞终态）—— 幂等，不 emit。
    if state.status in ("completed", "failed", "cancelled"):
        return StepResult(done=True, reason=f"already_{state.status}")

    nodes = _node_by_name(wf)
    emits: list[Emit] = []

    # 2. 首次（无 workflow_started）：起 workflow + entry 节点。
    if state.status == "pending":
        entry = wf.entry
        _check_agent_node(nodes.get(entry), entry)
        logger.info("workflow 启动（%s，entry=%s）", rid, entry)
        # host_session 仅在此 bootstrap 分支透传（写 workflow_started.data，tape 唯一真相源）；
        # next 路径（非 pending）不 emit workflow_started → 不需要传（SPEC §4.1 emit 真链）。
        t, d = make_workflow_started(rid, wf, inputs, yaml_path=yaml_path, host_session=host_session)
        emits.append(Emit(t, d))
        emits.append(Emit("node_started", {"node": entry}, node=entry))
        ctx = _build_ctx(wf, {}, inputs, rid)
        prompt, prompt_file, rroot = _deliver(
            nodes[entry], ctx, prompts_dir,
            wf=wf, project_root=project_root, no_memory=no_memory,
        )
        return StepResult(emits=emits, done=False, node=entry,
                          prompt=prompt, prompt_file=prompt_file, resources_root=rroot)

    # 3. 进行中。
    pending = _running_node(state)
    if output is not None:
        # 完成一个在途节点 → emit nc + rt + ns(next)（或 workflow_completed）。
        if pending is None:
            raise InSessionError(
                "advance(output=...) 但 tape 中无 running 节点（状态腐败 / 重复完成）",
                error_kind=ERR_STATE_CORRUPT,
            )
        try:
            parsed = _parse_output(output, nodes[pending])
        except RecoverableInSessionError as e:
            # SPEC 2026-07-23 §4.2：自捕 recoverable（不 re-raise）→ 重 arm 同节点，
            # 返 StepResult(recoverable=True)（run 存活）。连续 N 次升格 workflow_failed。
            # 不外抛 → cli 的 except InSessionError 不触发 recoverable；cli 走正常 result 路径。
            return _recover_step_result(
                tape, wf, e, pending, state, inputs, rid,
                prompts_dir, project_root, no_memory,
            )
        emits.append(Emit("node_completed", {"output": parsed}, node=pending))
        # 用「历史 outputs + 本次 output」求下一 node（同 _next_node_for_resume 的入参形态）。
        outputs_acc = _outputs_acc_from_state(state)
        outputs_acc[pending] = {"output": parsed}
        nxt = Orchestrator._next_node_for_resume(wf, pending, outputs_acc)
        if nxt == END:
            emits.append(Emit("route_taken", {"from": pending, "to": END}))
            t, d = make_workflow_completed(wf, _final_outputs(wf, outputs_acc, inputs, rid), elapsed=elapsed)
            emits.append(Emit(t, d))
            logger.info("workflow 完成（%s，elapsed=%.2fs）", rid, elapsed)
            return StepResult(emits=emits, done=True, reason="completed")
        _check_agent_node(nodes.get(nxt), nxt)
        emits.append(Emit("route_taken", {"from": pending, "to": nxt}))
        emits.append(Emit("node_started", {"node": nxt}, node=nxt))
        ctx = _build_ctx(wf, outputs_acc, inputs, rid)
        prompt, prompt_file, rroot = _deliver(
            nodes[nxt], ctx, prompts_dir,
            wf=wf, project_root=project_root, no_memory=no_memory,
        )
        return StepResult(emits=emits, done=False, node=nxt,
                          prompt=prompt, prompt_file=prompt_file, resources_root=rroot)

    # 4. 无 output 且进行中 → 幂等重发 pending prompt（宿主可能丢失了上次的指令）。
    if pending is None:
        raise InSessionError(
            "advance() 无 output 但 tape 中无 running 节点（workflow_started 后未起节点？）",
            error_kind=ERR_STATE_CORRUPT,
        )
    ctx = _build_ctx(wf, _outputs_acc_from_state(state), inputs, rid)
    prompt, prompt_file, rroot = _deliver(
        nodes[pending], ctx, prompts_dir,
        wf=wf, project_root=project_root, no_memory=no_memory,
    )
    return StepResult(emits=[], done=False, node=pending,
                      prompt=prompt, prompt_file=prompt_file, resources_root=rroot)


def _check_agent_node(node: Any, name: str) -> None:
    """v1 只支持 agent 节点（宿主 subagent 执行模型）。其余 fail loud。"""
    if node is None:
        raise InSessionError(
            f"节点 {name!r} 不在 workflow.nodes 中",
            error_kind=ERR_UNSUPPORTED_NODE_KIND,
        )
    if getattr(node, "kind", None) != "agent":
        raise InSessionError(
            f"in-session shell v1 仅支持 agent 节点，{name!r} 是 {getattr(node,'kind',None)!r}"
            "（parallel/foreach/script/gate 请用 orca run / TUI / Web）",
            error_kind=ERR_UNSUPPORTED_NODE_KIND,
        )
