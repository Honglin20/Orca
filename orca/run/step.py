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

from orca.events.replay import replay_state
from orca.exec.error import ExecError
from orca.exec.render import render_prompt, render_template
from orca.run.lifecycle import (
    make_workflow_completed,
    make_workflow_started,
    now_monotonic,
)
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


class InSessionError(Exception):
    """in-session 推进中的非法状态（fail loud）：状态腐败 / 不支持的节点类型等。

    ``error_kind`` 显式携带 SPEC §2.5 taxonomy 分类（默认 ``internal_error`` 兜底），
    供 cli.py ``_classify_in_session_error`` 直读。每个 raise 处传对应 ``ERR_*`` 常量。
    """

    def __init__(self, message: str, *, error_kind: str = ERR_INTERNAL_ERROR):
        super().__init__(message)
        self.error_kind = error_kind


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
        # 结构化预期但拿到非 JSON —— fail loud（route 求值会缺字段，不如早炸）。
        raise InSessionError(
            f"节点 {node.name!r} 声明了 output_schema 但宿主输出非 JSON：{raw[:80]!r}",
            error_kind=ERR_OUTPUT_SCHEMA_MISMATCH,
        )
    # 字段校验：jsonschema>=4.0（pyproject 已声明，exec/ result_extractor 同款用法）。
    # 必须同时 catch SchemaError：compile 层不校验 output_schema 形状，用户 YAML 写错
    # （如 required 非字符串、type 拼错）会让 validate 抛 SchemaError（非 ValidationError
    # 子类）——只 catch ValidationError 会逃逸成脏崩溃（review 🔴，D-v8.x-2 初衷）。
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as e:
        path = ".".join(str(p) for p in e.absolute_path) or "<root>"
        raise InSessionError(
            f"节点 {node.name!r} 输出不满足 output_schema：{e.message}（路径 {path}）",
            error_kind=ERR_OUTPUT_SCHEMA_MISMATCH,
        )
    except jsonschema.SchemaError as e:
        # schema 自身畸形 → fail loud（归类 output_schema_mismatch，消息区分语义）。
        raise InSessionError(
            f"节点 {node.name!r} 的 output_schema 自身畸形：{e.message}",
            error_kind=ERR_OUTPUT_SCHEMA_MISMATCH,
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


def _deliver(node: Any, ctx: Any, prompts_dir: Path | None) -> tuple[str | None, str | None, str | None]:
    """渲染 prompt 并按交付模式产出 ``(prompt, prompt_file, resources_root)``。

    - ``prompts_dir`` 给定（compact，生产路径）：写文件，返 ``(None, <path>, resources_root)``。
    - ``prompts_dir=None``（inline 回退，单测决策逻辑用）：返 ``(rendered, None, None)``。
    """
    rendered = _render_or_fail(node, ctx)
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
) -> StepResult:
    """单步推进（纯决策：读 tape 现状 → 决定 emits + 回复；不写 tape）。

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
    """
    state = replay_state(tape)
    # tape 是 inputs 真相源（workflow_started.data.inputs）：next 不传 --inputs 时从 tape
    # 恢复（deterministic —— 模型不必每步重传，且修掉非 entry 节点 {{ inputs.* }} 依赖 CLI
    # 重传的隐患）。bootstrap 首调时 tape 无 workflow_started → _inputs_from_tape 返 {} →
    # 自然 fallback 到 CLI 传入的 inputs。与 Orchestrator resume（_inputs_from_tape）同源。
    tape_inputs = Orchestrator._inputs_from_tape(tape)
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
        prompt, prompt_file, rroot = _deliver(nodes[entry], ctx, prompts_dir)
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
        parsed = _parse_output(output, nodes[pending])
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
        prompt, prompt_file, rroot = _deliver(nodes[nxt], ctx, prompts_dir)
        return StepResult(emits=emits, done=False, node=nxt,
                          prompt=prompt, prompt_file=prompt_file, resources_root=rroot)

    # 4. 无 output 且进行中 → 幂等重发 pending prompt（宿主可能丢失了上次的指令）。
    if pending is None:
        raise InSessionError(
            "advance() 无 output 但 tape 中无 running 节点（workflow_started 后未起节点？）",
            error_kind=ERR_STATE_CORRUPT,
        )
    ctx = _build_ctx(wf, _outputs_acc_from_state(state), inputs, rid)
    prompt, prompt_file, rroot = _deliver(nodes[pending], ctx, prompts_dir)
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
