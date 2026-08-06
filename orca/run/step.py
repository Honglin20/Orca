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
# SPEC 2026-08-04 §3：子代理自报失败哨兵（orca_node_failed_v1）→ recoverable（v1 recoverable
# 集合扩为 {output_schema_mismatch, agent_blocked}，共用 _recover_step_result + 升格 + 信封）。
ERR_AGENT_BLOCKED = "agent_blocked"

# SPEC 2026-07-23-in-session-error-management §2 P4：同节点连续 recoverable 失败上限。
# 撞上限 → 升格 workflow_failed（防死循环）。对齐哨兵 MAX_ASK=3，不可配（YAGNI）。
_RECOVERABLE_ESCALATE_AT = 3

# SPEC 2026-08-04 §4.4：失败哨兵教学脚注（host contract，恒 append 到 agent 节点 prompt 末尾）。
# 极简、不改节点任务指令语义（与 ask_user routing 脚注 / memory 注入同 host-contract append 模式）。
# 无 schema 节点若无此脚注，agent 不发哨兵直接回 plain 失败文本 → 引擎无抓手静默当成功（§9 R1）。
_FAILURE_SENTINEL_FOOTER = (
    "[Orca 失败协议] 若你无法完成本节点（遇阻 / 缺前置 / 执行出错），不要硬编造产出。\n"
    '返回恰好这个 JSON 作为最终消息：{"blocked_on": "<具体卡点>", '
    '"tried": ["<已试步骤>"],\n'
    '"reason": "<可选诊断>", "_sentinel": "orca_node_failed_v1"}'
)

# ingest 限长常量（SPEC §4.1）。
_SENTINEL_STR_LIMIT = 200
_SENTINEL_TRIED_MAX_ITEMS = 5
_SENTINEL_TRIED_ITEM_LIMIT = 120


class InSessionError(Exception):
    """in-session 推进中的非法状态（fail loud）：状态腐败 / 不支持的节点类型等。

    ``error_kind`` 显式携带 SPEC §2.5 taxonomy 分类（默认 ``internal_error`` 兜底），
    供 cli.py ``_classify_in_session_error`` 直读。每个 raise 处传对应 ``ERR_*`` 常量。
    """

    def __init__(self, message: str, *, error_kind: str = ERR_INTERNAL_ERROR):
        super().__init__(message)
        self.error_kind = error_kind


class RecoverableInSessionError(InSessionError):
    """可恢复的节点产出错误（SPEC 2026-07-23 §3 + 2026-08-04 §3 扩 ``agent_blocked``）。

    与 plain ``InSessionError`` 的区别：``advance_step`` 在 ``output is not None`` 分支
    **自捕** 此子类 —— 不 re-raise，而是 emit ``[node_failed, node_started]`` 重 arm 同节点、
    返 ``StepResult(recoverable=True)``（run 存活，决策权交主 session）。连续 N 次未通过才
    升格 ``workflow_failed``（终态）。

    ``error_kind`` 取值（2026-08-04 §3 扩）：``output_schema_mismatch``（默认，向后兼容既有 raise）
    或 ``agent_blocked``（子代理自报失败哨兵，raise 处显式传）。render_error / state_corrupt /
    unsupported_node_kind / internal_error 仍 plain ``InSessionError`` = irrecoverable。

    ``blocked_on`` / ``tried`` 仅 ``agent_blocked`` 携带（哨兵结构字段，``_node_failed_data``
    additively 读，``output_schema_mismatch`` 路径不传 = 既有 4 字段形态不变）。

    因属 ``InSessionError`` 子类，``cli.next`` 的 ``except InSessionError`` 仍能兜底捕获（防御：
    正常路径 advance_step 自捕不外抛，但若未来调用方直接调 ``_parse_output`` 仍走旧 fail 路径）。
    """

    def __init__(
        self,
        message: str,
        *,
        error_kind: str = ERR_OUTPUT_SCHEMA_MISMATCH,
        blocked_on: str | None = None,
        tried: list[str] | None = None,
    ):
        super().__init__(message, error_kind=error_kind)
        self.blocked_on = blocked_on
        self.tried = tried


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


def consecutive_failures(tape: Tape, node: str) -> list[dict]:
    """当前节点在 tape 末尾的**连续** recoverable 失败 ``node_failed.data`` 记录
    （SPEC 2026-08-04 §4.3，AC13）。

    派生谓词同 ``consecutive_fail_count``（E1 钉死）：遇 ``node_completed(任意节点)`` 归零；
    计 ``node_failed(node)`` 的 ``data``（缺字段 data → ``{}``，消费方 ``.get()`` 防御，AC11/13）。
    正向单次扫描，物化 list O(k) 空间（k ≤ ``_RECOVERABLE_ESCALATE_AT-1`` = 2，N7 权衡可接受）。

    不进 reducer fold（``events/replay.py`` 零改边界）；``advance_step`` / ``_recover_step_result``
    在 recoverable 决策点调它。SSOT 在 tape。
    """
    records: list[dict] = []
    for event in tape.replay():
        if event.type == "node_completed":
            records = []
        elif event.type == "node_failed" and event.node == node:
            records.append(event.data or {})
    return records


def consecutive_fail_count(tape: Tape, node: str) -> int:
    """当前节点在 tape 末尾的**连续** recoverable 失败次数（SPEC 2026-07-23 §4.3，AC9）。

    DRY delegate（2026-08-04 §4.3 / AC11）：``return len(consecutive_failures(...))`` ——
    单一扫描实现，谓词与 ``consecutive_failures`` 完全一致（避免双实现漂移）。
    """
    return len(consecutive_failures(tape, node))


def _coerce_str(x: Any, *, limit: int = _SENTINEL_STR_LIMIT) -> str | None:
    """ingest helper：哨兵字段 str 化 + 截断（SPEC 2026-08-04 §4.1 m3）。

    None / 空串 → None（sentinel ``blocked_on`` 缺/空时认畸形，``_node_failed_data`` 据此省字段）。
    非 str → ``str()`` 化；超 ``limit`` 截断。确定性非 LLM。
    """
    if x is None:
        return None
    s = x if isinstance(x, str) else str(x)
    s = s[:limit]
    return s or None


def _coerce_tried(
    x: Any,
    *,
    max_items: int = _SENTINEL_TRIED_MAX_ITEMS,
    item_limit: int = _SENTINEL_TRIED_ITEM_LIMIT,
) -> list[str] | None:
    """ingest helper：哨兵 ``tried`` list 化 + 元素 str 化 + 截断（SPEC 2026-08-04 §4.1 m3）。

    None → None；非 list → ``[str(x)]``；元素非 str → ``str()`` 化；``≤ max_items`` 项 × 每项
    ``≤ item_limit`` 字符。空 list / coerce 后空 → None（``_node_failed_data`` 据此省字段）。
    确定性非 LLM，永不崩（AC11）。
    """
    if x is None:
        return None
    items = x if isinstance(x, list) else [x]
    coerced = [str(it)[:item_limit] for it in items[:max_items]]
    return coerced or None


def _build_ctx(
    wf: Workflow,
    outputs_acc: dict[str, Any],
    inputs: dict[str, Any],
    run_id: str,
    *,
    workflows_root: Path | None = None,
) -> RunContext:
    """in-session 路径的 RunContext 构造（mirror orchestrator._make_ctx 的子集）。

    point-to-file 协议（SPEC §3.2）：``workflows_root`` 由 ``advance_step`` 经
    ``yaml_path`` 父目录透传；解析 ``workflows_root / "subagents" / wf.name`` 为存在
    目录 → 返绝对路径字符串，否则空串（无 subagents 的 workflow 走空串分支，§3.3）。
    """
    from orca.exec.context import RunContext
    from orca.run.orchestrator import _compute_subagents_root

    if workflows_root is None:
        workflows_root = wf.workflows_root
    return RunContext(
        inputs=inputs, outputs=outputs_acc, run_id=run_id, task=None,
        subagents_root=_compute_subagents_root(workflows_root, wf.name),
    )


def _workflows_root_from_yaml(yaml_path: str | None) -> Path | None:
    """yaml_path → workflows_root 解析（point-to-file 协议 SPEC §3.2 in-session 透传）。

    yaml_path 是 workflow yaml 的绝对路径；workflows_root = yaml 所在目录（与 orchestrator
    ``__init__`` 同源 ``yaml_path.parent``）。None / 空串 → None（向后兼容：daemon 未传
    yaml_path 的旧 caller，subagents_root 落空串，render 层 fail loud 兜底）。
    """
    if not yaml_path:
        return None
    return Path(yaml_path).resolve().parent


def _parse_output(raw: str, node: Any) -> Any:
    """按 node.output_schema 解析宿主回捕的文本输出；无 schema 视为裸字符串。

    失败哨兵检测（SPEC 2026-08-04 §3，M3 钉死）：**首先** try ``json.loads(raw)`` → 若 dict
    且 ``_sentinel=="orca_node_failed_v1"`` → raise ``RecoverableInSessionError(error_kind=
    ERR_AGENT_BLOCKED, ...)``（哨兵优先于 schema 校验，且 peek 在 ``if not schema`` 早返**之前**
    ——无 schema 节点也要检测，否则哨兵静默失效）。peek 成功的解析结果复用给 schema 路径（m7）。

    声明了 output_schema 时做两段校验（确定性，非 LLM validator）：
      1. JSON 解析失败 → ``output_schema_mismatch``（非 JSON）。
      2. ``jsonschema.validate`` 字段校验（缺失/类型错）→ ``output_schema_mismatch``；
         schema 自身畸形（用户 YAML 写错）→ 同样 fail loud（不脏崩溃，D-v8.x-2）。
    缺字段在此被抓（早于下游 render 的 UndefinedError），给清晰错误而非脏崩溃。
    子代理"自我纠正"发生在它自己 turn 内（rendered prompt 文件写明 schema 要求）；
    Orca 层产不对就 fail loud，不做重试循环（in-session 主 session 自己当判官）。
    """
    # peek：失败哨兵优先于 schema 校验（M3）。peek 成功的解析结果复用给 schema 路径（m7，避免双解析）。
    peek_ok = True
    try:
        peeked = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        peek_ok = False
        peeked = None
    if peek_ok and isinstance(peeked, dict) and peeked.get("_sentinel") == "orca_node_failed_v1":
        blocked_on = _coerce_str(peeked.get("blocked_on"))
        tried = _coerce_tried(peeked.get("tried"))
        reason = _coerce_str(peeked.get("reason"))
        msg = blocked_on or reason or "malformed sentinel: missing or empty blocked_on"
        raise RecoverableInSessionError(
            msg, error_kind=ERR_AGENT_BLOCKED,
            blocked_on=blocked_on, tried=tried,
        )

    schema = getattr(node, "output_schema", None)
    if not schema:
        return raw

    if not peek_ok:
        # 结构化预期但拿到非 JSON —— recoverable（SPEC 2026-07-23 §3）：advance_step
        # 自捕 RecoverableInSessionError → 重 arm 同节点 + 回反馈信封，主 session 重派。
        raise RecoverableInSessionError(
            f"节点 {node.name!r} 声明了 output_schema 但宿主输出非 JSON：{raw[:80]!r}",
        )
    parsed = peeked  # m7：复用 peek 结果（避免双解析）

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
    failure_history: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """渲染 prompt 并按交付模式产出 ``(prompt, prompt_file, resources_root)``。

    - ``prompts_dir`` 给定（compact，生产路径）：写文件，返 ``(None, <path>, resources_root)``。
    - ``prompts_dir=None``（inline 回退，单测决策逻辑用）：返 ``(rendered, None, None)``。

    【node-memory】``wf`` / ``project_root`` / ``no_memory`` 给定且 ``node.memory=True`` 时,
    在渲染后、写文件前调 ``inject_memory_prompt`` 把上一轮 MD body + 复用协议拼到 rendered
    末尾(SPEC §4.1)。三 kwarg 默认值保 ``_deliver(node, ctx, prompts_dir)`` 旧调用形态不破
    (单测 inline 路径 / 非记忆节点零行为变更)。

    【failure_history + 教学脚注（2026-08-04 §4.3/§4.4）】渲染后顺序钉死：
    ``rendered = _render_or_fail(...)`` →（``memory=True`` 时）``inject_memory_prompt`` →
    （``failure_history`` 非 None 时）``failure_history + "\\n\\n" + rendered``（prepend，含本次失败）
    →（恒）append ``_FAILURE_SENTINEL_FOOTER``（host contract，首 attempt + 重 arm 均带）。
    最终 prompt = ``[失败历史?] + [节点 prompt + 记忆?] + [哨兵教学脚注]``。

    调用者（m1 / V2-3）：``advance_step`` 内 bootstrap + next-node 两处保 ``failure_history=None``；
    ``_recover_step_result`` + ``advance_step`` 幂等重发分支传计算值。
    """
    rendered = _render_or_fail(node, ctx)
    if getattr(node, "memory", False) and not no_memory and wf is not None and project_root is not None:
        rendered = inject_memory_prompt(node, wf, rendered, project_root=project_root)
    if failure_history:
        rendered = failure_history + "\n\n" + rendered
    rendered = rendered + "\n\n" + _FAILURE_SENTINEL_FOOTER
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
    """构造 ``node_failed`` 的 data（SPEC 2026-07-23 §4.2 + 2026-08-04 §4.2 additive 扩展）。

    下限 4 字段 ``{kind, error_type, message, phase}``（复用 executor 形态 ``exec/interface.py:15``，
    故意不共享 ErrorKind 枚举——失败本体不同）。``kind`` / ``error_type`` 改读 ``exc.error_kind``
    （M1：非硬编码，support ``agent_blocked``）。``phase`` 按 kind 区分（agent_self_report /
    output_validation）。

    ``agent_blocked`` **additively** 携带可选结构字段（N5 钉死）：
      - ``blocked_on`` 非空时存；缺/空时**省略**（不存 None），``message`` 已含 malformed 提示。
      - ``tried`` 非空时存；缺则省略。
    ``output_schema_mismatch`` 不带这两个字段（向后兼容，不变）。reducer 对 node_failed 只置
    ``node_status[node]=failed``，不读 data 字段（C1 不变量，纯可观测）。
    """
    is_blocked = exc.error_kind == ERR_AGENT_BLOCKED
    data: dict[str, Any] = {
        "kind": exc.error_kind,
        "error_type": exc.error_kind,
        "message": str(exc),
        "phase": "agent_self_report" if is_blocked else "output_validation",
    }
    if is_blocked:
        # N5：blocked_on 缺/空（_coerce_str 返 None）→ 省略字段；tried 同理。
        if getattr(exc, "blocked_on", None):
            data["blocked_on"] = exc.blocked_on
        if getattr(exc, "tried", None):
            data["tried"] = exc.tried
    return data


def _kind_breakdown(records: list[dict]) -> str:
    """统计 records 中各 error_kind 出现次数，返 ``"kind1×N, kind2×M"`` 文本（m4/m6）。

    用于升格 reason 让主 session 一眼看清混合 kind 分布（如 ``output_schema_mismatch×2,
    agent_blocked×1``）。缺 kind 字段 → ``?``。
    """
    counts: dict[str, int] = {}
    for d in records:
        k = d.get("kind") or d.get("error_type") or "?"
        counts[k] = counts.get(k, 0) + 1
    return ", ".join(f"{k}×{n}" for k, n in counts.items())


def _render_failure_history(
    records: list[dict], retry_count: int, retry_budget: int,
) -> str | None:
    """kind-aware 有界文本块（SPEC 2026-08-04 §4.3）。

    ``records`` 空 → 返 None（首 attempt 无块，AC2）。非空 → 返有界文本块（caller 传含本次的
    records，``len ≤ _RECOVERABLE_ESCALATE_AT - 1``，m5）。``agent_blocked`` 显示 ``blocked_on``
    （fallback ``message``，N5）+ ``tried``；``output_schema_mismatch`` 显示 ``message``。
    纯文本拼接（不进 Jinja，防注入），作为 literal prepend。缺字段防御性 ``.get()``，永不崩（AC11/13）。
    """
    if not records:
        return None
    lines = [
        f"## ⚠️ 本节点前序尝试失败（本次第 {retry_count}/{_RECOVERABLE_ESCALATE_AT} 次，"
        f"耗尽将终止 run）"
    ]
    for i, d in enumerate(records, 1):
        kind = d.get("kind", "?")
        lines.append(f"\n### Attempt {i} — failed [{kind}]")
        if kind == ERR_AGENT_BLOCKED:
            blocked_on = d.get("blocked_on") or d.get("message", "")
            lines.append(f"blocked_on: {blocked_on}")
            tried = d.get("tried") or []
            if tried:
                lines.append("tried: [" + ", ".join(str(t) for t in tried) + "]")
        else:
            lines.append(str(d.get("message", "")))
    return "\n".join(lines)


def _recover_step_result(
    tape: Tape, wf: Workflow, exc: RecoverableInSessionError, pending: str,
    state: Any, inputs: dict[str, Any], rid: str,
    prompts_dir: Path | None, project_root: Path | None, no_memory: bool,
    workflows_root: Path | None = None,
) -> StepResult:
    """recoverable 自恢复（SPEC 2026-07-23 §4.2 + 2026-08-04 §4.3/§6 含本次 + 升格 kind）。

    emit ``[node_failed, node_started]`` 重 arm 同节点；连续 ``_RECOVERABLE_ESCALATE_AT`` 次
    未通过 → 升格 ``workflow_failed``。

    计数语义（SPEC §4.3）：``count = consecutive_fail_count(tape, pending)`` 是**本次失败
    落 tape 前**的前序连续失败数；本次是第 ``count+1`` 次（1-based ``retry_count``）。
      - ``count+1 < N``：emit ``[nf, ns]`` + 重渲染 prompt + 返 ``StepResult(recoverable=True,
        retry_count=count+1, retry_budget=N-(count+1))``（run 存活，marker 不清）。
      - ``count+1 >= N``：升格——emit 顺序 ``nf → ns → workflow_failed``（E8 钉死：第 N 次失败
        真实记录后再终态，保 ``count 重建 = retry_count`` 不变量）；返 ``done=True``。

    R-N2（含本次失败）：``records = consecutive_failures(tape, pending) + [本次 nf_emit.data]``
    ——本次 ``node_failed`` emit 构造**后**、emit_batch 落 tape **前**计算（从 ``Emit.data`` 取，
    不从 tape 取）。本次是最 relevant 的失败，fresh agent 必须看到（L1 哑管道下主 session 被禁注入，
    引擎是唯一能把本次失败放进 agent prompt 的实体）。下一次失败时 ``consecutive_fail_count``
    已含本次（已落 tape），不重复计数。

    m4/m6（升格 kind）：``make_workflow_failed(exc.error_kind, ...)``；reason 含 ``_kind_breakdown``
    （如 ``output_schema_mismatch×2, agent_blocked×1``）；升格 ``StepResult.error_kind=exc.error_kind``
    （非固定 schema_mismatch，本次 kind 即升格 kind）。

    重渲染用 ``_outputs_acc_from_state(state)``：pending 未 completed（emit 的是 nf 而非 nc），
    其坏 output 不进 context → 上下文与首次 arm 一致（确定性把手，P3）。

    Corner case（code-reviewer 🟢）：未升格分支调 ``_deliver`` 重渲染 prompt 时，若节点 prompt
    自身有 Jinja 错 / 引用上游缺字段（render_error），``_render_or_fail`` 抛 ``InSessionError(render_error)``
    —— 已构的 ``[nf, ns]`` emits 被丢弃，异常透传到 cli ``except InSessionError`` → ``fail_in_session``
    emit ``workflow_failed(render_error)``。即「合法升格被 render_error 短路为 irrecoverable fail loud」
    （render_error 本质 wf-author bug，重跑也修不了；SPEC §3 明示 render_error 全 irrecoverable）。
    此场景下本次 nf 未落 tape，count 不变量不成立 —— 可接受，因 render_error 永远进 irrecoverable 终态、不再循环。
    """
    nodes = _node_by_name(wf)
    count = consecutive_fail_count(tape, pending)
    this_attempt = count + 1
    emits: list[Emit] = [
        Emit("node_failed", _node_failed_data(exc), node=pending),
        Emit("node_started", {"node": pending}, node=pending),
    ]
    # R-N2：含本次失败（从刚构造的 emits[0].data 取，本次尚未落 tape）。
    records_inclusive = consecutive_failures(tape, pending) + [emits[0].data]

    if this_attempt >= _RECOVERABLE_ESCALATE_AT:
        # 升格（E8 + m4/m6）：先 emit 本次 [nf, ns]，再追加 workflow_failed（tape 记录第 N 次真实失败）。
        breakdown = _kind_breakdown(records_inclusive)
        reason = (f"consecutive recoverable exhausted: 节点 {pending!r} 连续 "
                  f"{this_attempt} 次失败（{breakdown}）")
        t, d = make_workflow_failed(exc.error_kind, reason, node=pending)
        emits.append(Emit(t, d))
        logger.warning(
            "节点 %s 连续 %d 次 recoverable 失败，升格 workflow_failed（run=%s）",
            pending, this_attempt, rid,
        )
        return StepResult(emits=emits, done=True, reason=reason,
                          error_kind=exc.error_kind)

    # 未升格 → 重 arm：重渲染 prompt（与正常 next 同形交付，compact/inline 由 prompts_dir 决定）。
    retry_budget = _RECOVERABLE_ESCALATE_AT - this_attempt
    failure_history = _render_failure_history(records_inclusive, this_attempt, retry_budget)
    ctx = _build_ctx(wf, _outputs_acc_from_state(state), inputs, rid,
                     workflows_root=workflows_root)
    prompt, prompt_file, rroot = _deliver(
        nodes[pending], ctx, prompts_dir,
        wf=wf, project_root=project_root, no_memory=no_memory,
        failure_history=failure_history,
    )
    hint = (
        f"节点自报失败/产出不合 schema（第 {this_attempt}/{_RECOVERABLE_ESCALATE_AT} 次）。"
        f"重 arm 的 prompt 已含历次失败原因（含本次）——按你的判断重派（复用同 agent 或 fresh），"
        f"拿产出再 orca next --output（剩余 {retry_budget} 次）"
    )
    logger.info(
        "节点 %s recoverable 失败（第 %d/%d 次），重 arm（run=%s）",
        pending, this_attempt, _RECOVERABLE_ESCALATE_AT, rid,
    )
    return StepResult(
        emits=emits, done=False, node=pending,
        prompt=prompt, prompt_file=prompt_file, resources_root=rroot,
        recoverable=True, retry_count=this_attempt, retry_budget=retry_budget,
        error_kind=exc.error_kind, reason=str(exc), hint=hint,
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
    # 与 Orchestrator resume（SPEC B 后：``replay_for_resume``）同源——均调 events 层
    # ``_replay_state_and_inputs`` / ``apply_event`` 同一 reducer fold 路径抽 inputs。
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
        ctx = _build_ctx(wf, {}, inputs, rid,
                         workflows_root=_workflows_root_from_yaml(yaml_path))
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
                workflows_root=_workflows_root_from_yaml(yaml_path),
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
        ctx = _build_ctx(wf, outputs_acc, inputs, rid,
                         workflows_root=_workflows_root_from_yaml(yaml_path))
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
    # M2 / V2-2：count > 0 时注入 tape 重建的失败历史（cross-session resume，AC10）。
    # 此分支 ``count`` 在所有 emits 落 tape **之后**取值——已含触发上次 re-arm 的那次失败
    # （与 re-arm 路径「本次 nf 未落 tape、count 只含 prior」语义不同）。故 ``count`` 恰等于
    # re-arm 路径的 ``this_attempt``（= re-arm 的 ``count_before + 1``），retry_count 直接用
    # ``count``（非 ``count+1``）、retry_budget 用 ``N-count``。
    count = consecutive_fail_count(tape, pending)
    failure_history = (
        _render_failure_history(
            consecutive_failures(tape, pending), count, _RECOVERABLE_ESCALATE_AT - count,
        )
        if count > 0 else None
    )
    ctx = _build_ctx(wf, _outputs_acc_from_state(state), inputs, rid,
                     workflows_root=_workflows_root_from_yaml(yaml_path))
    prompt, prompt_file, rroot = _deliver(
        nodes[pending], ctx, prompts_dir,
        wf=wf, project_root=project_root, no_memory=no_memory,
        failure_history=failure_history,
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
