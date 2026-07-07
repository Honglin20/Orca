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

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from orca.events.replay import replay_state
from orca.exec.render import render_prompt
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


class InSessionError(Exception):
    """in-session 推进中的非法状态（fail loud）：状态腐败 / 不支持的节点类型等。"""


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
    prompt: str | None = None        # 该节点渲染后的 prompt
    reason: str | None = None        # done=True 时的终止原因 / 错误说明


def _running_node(state: Any) -> str | None:
    """reducer state 中唯一 ``running`` 节点（在途、started 未 completed）。

    in-session 顺序推进，同一时刻至多一个 running；>1 即状态腐败，fail loud。
    """
    running = [n for n, s in state.node_status.items() if s == "running"]
    if len(running) > 1:
        raise InSessionError(
            f"tape 中存在多个 running 节点 {running}（状态腐败 / 并发调用）"
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

    v1：结构化解析走 prompt 引导（同 opencode profile 的 structured_output=
    "prompt_injection"），此处仅做 JSON 宽松解析（schema 校验留 phase SPEC）。
    """
    import json

    schema = getattr(node, "output_schema", None)
    if not schema:
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # 结构化预期但拿到非 JSON —— fail loud（route 求值会缺字段，不如早炸）。
        raise InSessionError(
            f"节点 {node.name!r} 声明了 output_schema 但宿主输出非 JSON：{raw[:80]!r}"
        )


def _final_outputs(wf: Workflow, outputs_acc: dict[str, Any]) -> dict[str, Any]:
    """workflow_completed 的 outputs。

    v1：``wf.outputs`` 非空（声明了输出模板）时 fail loud —— 完整 ``evaluate_outputs``
    模板求值与 drive_loop 对齐留独立 phase（避免改 drive_loop，方案 E）。无声明则
    返各节点 raw output 集合。
    """
    if getattr(wf, "outputs", None):
        raise InSessionError(
            "in-session shell v1 不支持 wf.outputs 模板（evaluate_outputs 未对齐；"
            "用 orca run / TUI / Web）"
        )
    return {node: acc.get("output") for node, acc in outputs_acc.items()}


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
    """
    state = replay_state(tape)
    inputs = _resolve_inputs(wf, inputs)
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
        t, d = make_workflow_started(rid, wf, inputs)
        emits.append(Emit(t, d))
        emits.append(Emit("node_started", {"node": entry}, node=entry))
        ctx = _build_ctx(wf, {}, inputs, rid)
        return StepResult(emits=emits, done=False, node=entry,
                          prompt=render_prompt(nodes[entry], ctx))

    # 3. 进行中。
    pending = _running_node(state)
    if output is not None:
        # 完成一个在途节点 → emit nc + rt + ns(next)（或 workflow_completed）。
        if pending is None:
            raise InSessionError(
                "advance(output=...) 但 tape 中无 running 节点（状态腐败 / 重复完成）"
            )
        parsed = _parse_output(output, nodes[pending])
        emits.append(Emit("node_completed", {"output": parsed}, node=pending))
        # 用「历史 outputs + 本次 output」求下一 node（同 _next_node_for_resume 的入参形态）。
        outputs_acc = _outputs_acc_from_state(state)
        outputs_acc[pending] = {"output": parsed}
        nxt = Orchestrator._next_node_for_resume(wf, pending, outputs_acc)
        if nxt == END:
            emits.append(Emit("route_taken", {"from": pending, "to": END}))
            t, d = make_workflow_completed(wf, _final_outputs(wf, outputs_acc), elapsed=elapsed)
            emits.append(Emit(t, d))
            logger.info("workflow 完成（%s，elapsed=%.2fs）", rid, elapsed)
            return StepResult(emits=emits, done=True, reason="completed")
        _check_agent_node(nodes.get(nxt), nxt)
        emits.append(Emit("route_taken", {"from": pending, "to": nxt}))
        emits.append(Emit("node_started", {"node": nxt}, node=nxt))
        ctx = _build_ctx(wf, outputs_acc, inputs, rid)
        return StepResult(emits=emits, done=False, node=nxt,
                          prompt=render_prompt(nodes[nxt], ctx))

    # 4. 无 output 且进行中 → 幂等重发 pending prompt（宿主可能丢失了上次的指令）。
    if pending is None:
        raise InSessionError(
            "advance() 无 output 但 tape 中无 running 节点（workflow_started 后未起节点？）"
        )
    ctx = _build_ctx(wf, _outputs_acc_from_state(state), inputs, rid)
    return StepResult(emits=[], done=False, node=pending,
                      prompt=render_prompt(nodes[pending], ctx))


def _check_agent_node(node: Any, name: str) -> None:
    """v1 只支持 agent 节点（宿主 subagent 执行模型）。其余 fail loud。"""
    if node is None:
        raise InSessionError(f"节点 {name!r} 不在 workflow.nodes 中")
    if getattr(node, "kind", None) != "agent":
        raise InSessionError(
            f"in-session shell v1 仅支持 agent 节点，{name!r} 是 {getattr(node,'kind',None)!r}"
            "（parallel/foreach/script/gate 请用 orca run / TUI / Web）"
        )
