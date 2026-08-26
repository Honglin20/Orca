"""validator.py —— 语义校验层（SPEC §4 的 9 项 + warnings）。

结构校验（字段/类型/extra/discriminator）由 schema 层 pydantic 完成；本模块只做
**语义校验**：图结构（name 唯一含组名 / entry 非组 / routes 引用 / parallel 组结构 /
死锁检测）+ Jinja2 引用浅校验。

phase 5 单轨化迁移后校验项重排（9 项：①②④⑥⑦⑧⑨⑩⑪⑬，③⑤ 已废）：
  ① name 非空 + 全局唯一（node 名 + parallel 组名共享命名空间）
  ② entry 存在
  ⑬ entry 不是 parallel 组（必须 node）—— 合并进调用顺序紧跟 ②
  ④ routes.to 引用有效（node 名 / parallel 组名 / $end）—— node 与 parallel 组都校验
  ⑥ entry 可达终态（沿 routes 前向边 + parallel 组展开；无 route = 隐式终态）
  ⑦ Jinja2 引用浅校验
  ⑧ foreach.source 首段是真实 node
  ⑨ profiles capability 校验
  ⑩ parallel 组结构校验（branches ≥2 / 已定义 / 无重复 / 不自引用）
  ⑪ 兜底 route 位置校验（when=None 必须最后一条）—— node 与 parallel 组都校验
（③ after 引用有效、⑤ after 无环 随 after 字段删除而废除。）

设计原则：
- **聚合**：9 个 `_check_*` 全部往同一个 `ValidationResult` 加，最后统一 raise，
  绝不第一个错就抛（SPEC §1 决策 1-B，LLM 生成 YAML 常多处错，一次报全）。
- **fail loud + 精确**：每个错误指明哪个 node / parallel 组 / 哪条边 / 哪个引用错了。
- **零反向依赖**：只依赖 `orca.schema` + jinja2（meta 解析，不 render）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from jinja2 import Environment
from jinja2.exceptions import TemplateSyntaxError
from jinja2.meta import find_undeclared_variables
from jinja2.nodes import And, CondExpr, Const, Getattr, Getitem, If, Name, Test

from orca.schema import (
    AgentNode,
    ForeachNode,
    ParallelGroup,
    ScriptNode,
    SetNode,
    TerminateNode,
    Workflow,
)

# 单例 Environment：仅用于 parse + meta 解析，绝不调用 render（渲染归 run/）。
_ENV = Environment()


# ── 保留字黑名单（SPEC v3 §2.2，MS1 闭环）──────────────────────────────────────
# ``orca <wf-name>`` 用 wf 名作裸顶层子命令；wf 名取了固定命令名（list/next/status/...）
# 就会让 ``orca status`` 在「跑 status 命令」vs「bootstrap 名为 status 的 wf」间歧义。
# compile 期硬拒（fail loud），保 ``orca <wf>`` 语法糖无冲突。
# 名单 = orca 7 命令 + tars 后端命令名 + ORCA_BACKEND_CMD 默认值（tars）+ 内部命令。
RESERVED_WF_NAMES: frozenset[str] = frozenset({
    # orca 7 命令（SPEC §2.1）
    "list", "next", "status", "stop", "open", "doctor",
    # orca 内部 / deprecated（仍占顶层命令槽）
    "bootstrap", "start", "serve",
    # tars 后端命令（SPEC §3.1）——归 tars entry point 但保 wf 名不撞
    "run", "ps", "logs", "wait", "resume", "install", "validate", "mcp",
    "executor", "skill",
    # ORCA_BACKEND_CMD 默认值（tars）；env 改名后 operator 应扩此集合（保守默认）
    "tars",
})


def _is_reserved_wf_name(name: str) -> bool:
    """wf 名是否撞保留字（小写比较，与 ``_slugify`` 无关——直接按字面拒）。"""
    return name.lower() in RESERVED_WF_NAMES


# ── errors / warnings 模型（SPEC §1）──────────────────────────────────────────


class ConfigurationError(Exception):
    """workflow 校验失败。含所有 errors（非致命 warnings 不阻止，但一并带上供 CLI 展示）。"""

    def __init__(self, errors: list[str], warnings: list[str]):
        self.errors = list(errors)
        self.warnings = list(warnings)
        super().__init__(self._format())

    def _format(self) -> str:
        lines = ["Workflow 校验失败："]
        for e in self.errors:
            lines.append(f"  ❌ {e}")
        if self.warnings:
            lines.append("警告（非致命）：")
            for w in self.warnings:
                lines.append(f"  ⚠️  {w}")
        return "\n".join(lines)


@dataclass
class ValidationResult:
    """内部承载 errors + warnings。跑完所有校验后由 raise_if_errors 统一裁决。"""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def raise_if_errors(self) -> list[str]:
        """有 errors 抛 ConfigurationError（含 warnings）；无则返回 warnings。"""
        if self.errors:
            raise ConfigurationError(self.errors, self.warnings)
        return self.warnings


# ── 对外入口 ─────────────────────────────────────────────────────────────────


def validate_workflow(
    wf: Workflow, workflows_root: Path | None = None
) -> list[str]:
    """全部语义校验。返回 warnings；有 errors 抛 ConfigurationError（SPEC §4）。

    ``workflows_root``（point-to-file subagent 协议 SPEC §3.2/§7，可选）：workflow yaml
    所在目录。提供时，``_check_subagents_md`` 校验 ``workflows_root / "subagents" / wf.name``
    内的子 agent md frontmatter 完整性 + body 旧协议残留。``None`` → 回退 ``wf.workflows_root``
    （``load_workflow`` 加载期绑定，单一真源）。目录不存在时：无模板引用 ``{{ subagents_root }}``
    → 跳过（如 quant-* 无子 agent 的 workflow，SPEC §3.3 正常）；有引用 → load 期 error
    （确定性错误前移，而非 run 中途 render 才炸）。
    """
    result = ValidationResult()
    _check_workflow_name_reserved(wf, result)  # §2.2 保留字黑名单（先于一切）
    _check_required_inputs_no_default(wf, result)  # 必填 input 不得带 default（KD-NAS latency_provider 铁律）
    _check_names_unique(wf, result)            # ①（含 parallel 组名）
    _check_entry_exists(wf, result)            # ②
    _check_entry_is_node(wf, result)           # ⑬ entry 非 parallel 组
    _check_route_refs_valid(wf, result)        # ④（node + parallel 组 routes）
    _check_entry_reachable_to_end(wf, result)  # ⑥（routes 前向边 + parallel 展开）
    _check_parallel_groups(wf, result)         # ⑩ parallel 组结构
    _check_route_fallback_last(wf, result)     # ⑪ 兜底 route 位置（node + parallel 组）
    _check_route_output_only_at_end(wf, result)  # phase-14：route.output 仅 $end 生效（非 $end warn）
    _check_jinja2_refs(wf, result)             # ⑦
    # 引用合规校验（⑦ 浅校验之上的深度校验；catch struct {%raw%} 误删类 bug）
    _check_self_reference(wf, result)          # prompt/command/values 禁引用自身 output（render 期崩）
    _check_output_schema_field_alignment(wf, result)  # strict schema 字段拼写对齐
    _check_folder_agent_scripts_exist(wf, result)     # $ORCA_AGENT_RESOURCES/scripts/<f> 存在性
    _check_input_tier_labels(wf, result)       # input description 三档标签（contract §6）
    _check_prompt_dev_residue(wf, result)      # agent.md body 禁开发期残留（受众分离契约）
    _check_subagents_md(wf, workflows_root, result)   # point-to-file subagent md frontmatter + 残留
    _check_foreach_source(wf, result)          # ⑧
    _check_terminate_constraints(wf, result)   # terminate step 约束（routes 空 / 非entry / 非parallel branch / 非foreach body）
    _check_execute_phase_no_gate_tools(wf, result)  # 铁律 7：execute phase 永不中断
    _check_profiles(wf, result)                # ⑨ capability 校验（profiles/validate）
    return result.raise_if_errors()


# ── helpers：命名空间（node 名 + parallel 组名）──────────────────────────────


def _check_workflow_name_reserved(wf: Workflow, result: ValidationResult) -> None:
    """SPEC v3 §2.2（MS1）：wf.name 禁取保留字（orca/tars 命令名）。

    ``orca <wf-name>`` 裸顶层语法糖要求 wf 名不与固定命令冲突；撞名 → compile fail loud，
    保 ``orca <wf>`` 无歧义。不接受 ``list/next/status/.../tars`` 等。
    """
    if not wf.name:
        # 空 name 由 schema 层/pydantic 拦；此处不重复报。
        return
    if _is_reserved_wf_name(wf.name):
        result.add_error(
            f"workflow name {wf.name!r} 是 Orca 保留字（命令名 / 后端变量名），"
            f"请改名以保 `orca <wf>` 语法糖无冲突。保留字名单："
            f"{sorted(RESERVED_WF_NAMES)}"
        )


def _check_required_inputs_no_default(wf: Workflow, result: ValidationResult) -> None:
    """必填 input 不应同时带 default（逻辑矛盾 + 静默失效风险）—— warning（非 error）。

    ``InputDef.required`` 默认 True，故「带 default 但省略 required」的常见可选 input 也会
    落入此模式；为避免误伤生态里大量此类 input，本检查降为 **warning**（``tars validate`` 可见，
    不阻断）。铁律性必填 input（如 KD-NAS 的 ``latency_provider``）的真正护栏是：
      (a) YAML 显式 ``required: true`` 且**不给 default``（KD-NAS 已如此）；
      (b) 运行时 ``orchestrator.py`` 必填缺失 + 无 default → fail loud。
    本 warning 只为在编译期暴露「required 却给了 default」的配置异味（BLK-10 可见性目标）。
    """
    for name, idef in wf.inputs.items():
        if idef.required and idef.default is not None:
            result.add_warning(
                f"input '{name}' 同时声明了 required: true 与 default（矛盾：default 会使 "
                f"required 失效；铁律性必填 input 不要给 default）"
            )


def _top_level_names(wf: Workflow) -> list[str]:
    """顶层 node 的 name（foreach 的无名 body 不在 wf.nodes，天然排除）。"""
    return [n.name for n in wf.nodes if n.name]


def _name_set(wf: Workflow) -> set[str]:
    """仅 node 名集合（⑩ branch 校验、foreach source 等只认 node 名）。"""
    return set(_top_level_names(wf))


def _jinja_root_set(wf: Workflow) -> set[str]:
    """Jinja2 引用合法 root：node 名 + parallel 组名 + ``workflow`` + ``inputs``。

    parallel 组名也合法：orchestrator 把组的聚合输出存进 ``ctx.outputs[group.name]``
    （与 node 同形 ``{"output": raw}``），故模板可 ``{{ group.output.outputs.x }}``
    引用组聚合结果。
    """
    return _name_set(wf) | _parallel_group_names(wf)


def _parallel_group_names(wf: Workflow) -> set[str]:
    """parallel 组名集合。"""
    return {g.name for g in wf.parallel}


def _all_names(wf: Workflow) -> set[str]:
    """node 名 ∪ parallel 组名（共享命名空间，①④⑥⑩⑬ 的合法集合）。"""
    return _name_set(wf) | _parallel_group_names(wf)


def _group_by_name(wf: Workflow) -> dict[str, ParallelGroup]:
    return {g.name: g for g in wf.parallel}


# ── ① name 非空 + 全局唯一（node 名 + parallel 组名共享命名空间）──────────────


def _check_names_unique(wf: Workflow, result: ValidationResult) -> None:
    counts: dict[str, int] = {}
    for idx, node in enumerate(wf.nodes):
        if not node.name:
            # 顶层 node 必须命名（"" 仅给 foreach 无名 body 用，body 不在此处）
            result.add_error(
                f"第 {idx} 个顶层 node（kind={node.kind}）缺少 name"
            )
            continue
        counts[node.name] = counts.get(node.name, 0) + 1
    for g in wf.parallel:
        # 组名也参与全局唯一计数；空名同样非法
        if not g.name:
            result.add_error("parallel 组缺少 name")
            continue
        counts[g.name] = counts.get(g.name, 0) + 1
    for name, count in counts.items():
        if count > 1:
            result.add_error(f"名称重复：'{name}' 出现 {count} 次（node 名与 parallel 组名共享命名空间）")


# ── ② entry 存在 ─────────────────────────────────────────────────────────────


def _check_entry_exists(wf: Workflow, result: ValidationResult) -> None:
    # entry 必须在「node 名 ∪ 组名」中存在；是否为组由 ⑬ 单独裁决
    if wf.entry not in _all_names(wf):
        result.add_error(f"entry '{wf.entry}' 不存在于 nodes / parallel 中")


# ── ⑬ entry 不是 parallel 组（必须 node）──────────────────────────────────────


def _check_entry_is_node(wf: Workflow, result: ValidationResult) -> None:
    """entry 只能是 node 名；指向 parallel 组 → error（单指针从 node 起步）。"""
    if wf.entry in _parallel_group_names(wf):
        result.add_error(f"entry '{wf.entry}' 不能是 parallel 组，必须是 node")


# ── ④ routes[].to 引用有效（node + parallel 组，target ∈ node名/组名/$end）──────


def _check_route_refs_valid(wf: Workflow, result: ValidationResult) -> None:
    names = _all_names(wf)
    for node in wf.nodes:
        if not node.name:
            continue
        for route in node.routes:
            if route.to != "$end" and route.to not in names:
                result.add_error(
                    f"node '{node.name}' 的 route 引用了不存在的目标 '{route.to}'"
                )
    for group in wf.parallel:
        for route in group.routes:
            if route.to != "$end" and route.to not in names:
                result.add_error(
                    f"parallel 组 '{group.name}' 的 route 引用了不存在的目标 '{route.to}'"
                )


# ── ⑥ entry 可达终态（沿 routes 前向边 + parallel 组展开）────────────────────


def _check_entry_reachable_to_end(wf: Workflow, result: ValidationResult) -> None:
    """从 entry 沿 routes 前向边走（parallel 组展开为 branches），必须能到终态。

    死胡同=error，孤立=warning。

    单轨模型裁决（SPEC §2.2⑥）：
    - node 的 successors = route.to（非 $end）；若 route.to 指向 parallel 组名，
      展开为该组的 branches（组 → 分支：分支是 node，组完成后才推进，所以可达性里
      组的下一跳是其分支）。
    - parallel 组也是可达性实体：组的 successors = 组的 route.to（非 $end）。
    - ``routes`` 为空的 node 视为隐式终态（保留裁决：parallel_research/batch_assess
      的 sink 节点需要）；parallel 组同理（无 routes 即组完成后隐式结束）。
    """
    node_names = _top_level_names(wf)
    group_names = _parallel_group_names(wf)
    node_by_name = {n.name: n for n in wf.nodes if n.name}
    group_by_name = _group_by_name(wf)
    if wf.entry not in node_by_name:
        return  # ②⑬ 已报，避免级联

    def successors_of(name: str) -> set[str]:
        """前向边：node 的 route.to（parallel 组名 → 组名本身 + 展开为 branches）；
        parallel 组的下一跳 = 组的 route.to。

        组名本身也标记可达：a→split 表示 a 路由到 split 组（组会被执行），故 split
        是可达实体；其 branches 是组的执行内容，同样可达；组完成后推进到组 routes。
        """
        out: set[str] = set()
        if name in group_by_name:
            # parallel 组的下一跳 = 组的 route.to（非 $end）
            for r in group_by_name[name].routes:
                if r.to != "$end":
                    out.add(r.to)
            return out
        node = node_by_name.get(name)
        if node is None:
            return out
        for r in node.routes:
            if r.to == "$end":
                continue
            if r.to in group_by_name:
                # route 指向 parallel 组 → 组本身可达 + 展开其 branches（branches 是
                # 组的执行内容，组完成后才推进到组 routes）
                out.add(r.to)
                out.update(group_by_name[r.to].branches)
            else:
                out.add(r.to)
        return out

    def is_terminal(name: str) -> bool:
        # 无 route = 隐式终态；否则要有显式 to="$end"
        if name in group_by_name:
            routes = group_by_name[name].routes
        else:
            node = node_by_name.get(name)
            routes = node.routes if node is not None else []
        return (not routes) or any(r.to == "$end" for r in routes)

    all_entities = set(node_names) | set(group_names)

    # can_end 不动点：terminal 或存在可到终态的后继（route 可成环，不动点自然收敛）
    can_end: dict[str, bool] = {n: is_terminal(n) for n in all_entities}
    changed = True
    while changed:
        changed = False
        for n in all_entities:
            if can_end[n]:
                continue
            for m in successors_of(n):
                if m in can_end and can_end[m]:
                    can_end[n] = True
                    changed = True
                    break

    # 从 entry BFS 求可达集（跨 node 与 parallel 组）
    reachable: set[str] = set()
    queue = [wf.entry]
    while queue:
        n = queue.pop()
        if n in reachable:
            continue
        if n not in all_entities:
            continue
        reachable.add(n)
        queue.extend(successors_of(n))

    # 可达却到不了终态 = 死胡同（error）。合并为一条消息列出所有死胡同实体。
    dead = sorted(n for n in all_entities if n in reachable and not can_end[n])
    if dead:
        result.add_error(
            f"从 entry 无法到达 $end（死胡同：{', '.join(dead)}）"
        )
    # 从 entry 不可达 = 孤立（warning，不阻止）
    for n in sorted(all_entities):
        if n not in reachable:
            kind = "parallel 组" if n in group_names else "node"
            result.add_warning(
                f"孤立{kind}：'{n}' 从 entry 不可达（可能忘了接线）"
            )


# ── ⑩ parallel 组结构校验 ─────────────────────────────────────────────────────


def _check_parallel_groups(wf: Workflow, result: ValidationResult) -> None:
    """parallel 组结构：branches ≥2 / 已定义 / 无重复 / 不自引用。

    - branches 长度 ≥ 2（少于 2 不是并行）。
    - branches 每项必须是已定义的 node 名（不能指向组——组内不嵌套组）。
    - branches 内无重复（同一 node 不能在同一组里出现两次）。
    - 组的 route 不能指向自己（自引用死锁）。
    组名唯一性归 ①，组 routes 引用合法归 ④，entry 非组归 ⑬。
    """
    node_names = _name_set(wf)
    for group in wf.parallel:
        # ⑩-1 branches 长度 ≥ 2
        if len(group.branches) < 2:
            result.add_error(
                f"parallel 组 '{group.name}' 的 branches 长度 < 2"
                f"（实际 {len(group.branches)}，并行至少需 2 个分支）"
            )
        # ⑩-2 branches 每项 ∈ node 名（不能是组名）
        for b in group.branches:
            if b not in node_names:
                result.add_error(
                    f"parallel 组 '{group.name}' 的 branch '{b}' 不是已定义的 node"
                )
        # ⑩-3 branches 无重复
        seen: set[str] = set()
        for b in group.branches:
            if b in seen:
                result.add_error(
                    f"parallel 组 '{group.name}' 的 branch '{b}' 重复出现"
                )
            seen.add(b)
        # ⑩-4 组不自引用（route.to 不能指向自己 → 否则组完成后路由回自己，死锁）
        for r in group.routes:
            if r.to == group.name:
                result.add_error(
                    f"parallel 组 '{group.name}' 的 route 自引用（指向自己）"
                )


# ── ⑪ 兜底 route 位置（when=None 必须最后一条）──────────────────────────────


def _check_route_fallback_last(wf: Workflow, result: ValidationResult) -> None:
    """无 when 的兜底 route（catch-all）必须是 routes 列表最后一条。

    否则其后 route 的 when 永远不会被求值（first-match-wins 命中兜底即返回）→ 死代码。
    node 与 parallel 组的 routes 都校验。
    """
    for node in wf.nodes:
        _check_fallback_last(node.routes, f"node '{node.name}'", result)
    for group in wf.parallel:
        _check_fallback_last(group.routes, f"parallel 组 '{group.name}'", result)


def _check_fallback_last(
    routes, location: str, result: ValidationResult
) -> None:
    for i, route in enumerate(routes):
        if route.when is None and i != len(routes) - 1:
            result.add_error(
                f"{location} 的无条件兜底 route 不是最后一条，其后的 route 永远不可达"
            )


# ── phase-14：Route.output 仅在 to="$end" 生效（非 $end 死代码 warn）──────────


def _check_route_output_only_at_end(wf: Workflow, result: ValidationResult) -> None:
    """``Route.output`` 仅在 ``to="$end"`` 生效；非 ``$end`` route 的 output 是死代码 → warn。

    语义（SPEC §0.1 #5 / §5）：``output`` 是 workflow 到达终点时的输出变换模板；
    route 到中间 node 时 output 无消费点（orchestrator 只在命中 ``$end`` 时取
    ``end_route.output``），故非 ``$end`` 的 output 会被静默忽略 → 编译期 warn 提示
    （非 error：未来若扩展中间节点输出变换，此 warn 可移除）。
    """
    for node in wf.nodes:
        if not node.name:
            continue
        for route in node.routes:
            if route.output and route.to != "$end":
                result.add_warning(
                    f"node '{node.name}' 的 route.output 仅在 to=\"$end\" 生效"
                    f"（当前 to='{route.to}'，此 output 是死代码将被忽略）"
                )
    for group in wf.parallel:
        for route in group.routes:
            if route.output and route.to != "$end":
                result.add_warning(
                    f"parallel 组 '{group.name}' 的 route.output 仅在 to=\"$end\" 生效"
                    f"（当前 to='{route.to}'，此 output 是死代码将被忽略）"
                )


# ── ⑦ Jinja2 引用浅校验 ──────────────────────────────────────────────────────


def _iter_templates(
    wf: Workflow,
) -> Iterable[tuple[str, str | None, str, bool, set[str]]]:
    """产出 (位置, self_name, 文本, 是否裸表达式, 额外合法 root)。

    覆盖所有 Jinja2 模板字段（plan §7-B 裁决：不止 prompt/when/outputs）：
    AgentNode.prompt / ScriptNode.command / SetNode.values / Route.when（node 与
    parallel 组两侧）/ Workflow.outputs / foreach body 的 prompt·command。
    额外合法 root：when 允许 ``output``（当前 node 自身输出）；foreach body 允许
    ``item_var`` / ``index_var``。

    ``self_name`` 用于自引用检测：模板**在所属节点跑之前**渲染的字段（prompt /
    command / values / foreach body）填**当前节点名**——这些位置引用 ``<self>.output``
    会触发 UndefinedError（render 期自身不在 ctx.outputs）。模板**在节点跑之后**评估
    的字段（route.when / route.output / workflow.outputs / terminate.reason /
    terminate.outputs）填 ``None``——此时 self.output 已在 ctx，引用合法。
    """
    for node in wf.nodes:
        if isinstance(node, AgentNode) and node.prompt:
            yield (f"node '{node.name}'.prompt", node.name, node.prompt, False, set())
        elif isinstance(node, ScriptNode) and node.command:
            yield (f"node '{node.name}'.command", node.name, node.command, False, set())
        elif isinstance(node, SetNode):
            for key, expr in node.values.items():
                yield (
                    f"node '{node.name}'.values.{key}",
                    node.name,
                    expr,
                    False,
                    set(),
                )
        elif isinstance(node, TerminateNode):
            # terminate 的 reason / outputs 都是 Jinja2 模板（同 set_node 渲染机制），
            # 同样需浅校验未声明引用（fail loud 在 compile 期而非 run 期）。
            # self_name=None：terminate 触达时本节点无 auto-output，self.output 无意义但
            # 非本检测目标（业务上不会写），归到「评估期 self=None」一档避免误报。
            if node.reason:
                yield (f"node '{node.name}'.reason", None, node.reason, False, set())
            for key, expr in node.outputs.items():
                yield (f"node '{node.name}'.outputs.{key}", None, expr, False, set())

        # route.when / route.output 在节点跑完后评估，self.output 合法 → self_name=None。
        for route in node.routes:
            if route.when:
                yield (
                    f"node '{node.name}'.route.when",
                    None,
                    route.when,
                    True,
                    {"output"},
                )
            # phase-14：Route.output 每 key 是 Jinja2 模板（到 $end 时的输出变换），同 wf.outputs 形
            if route.output:
                for key, expr in route.output.items():
                    yield (
                        f"node '{node.name}'.route.output.{key}",
                        None,
                        expr,
                        False,
                        set(),
                    )

        if isinstance(node, ForeachNode):
            body_extras = {node.item_var, node.index_var}
            body = node.body
            # body 在 foreach 执行期内逐项跑，foreach 自身尚未完成 → 引用 foreach.output 崩。
            # 故 body 的 self_name = foreach 节点名（catch body 里写 {{ foreach.output.X }}）。
            if isinstance(body, AgentNode) and body.prompt:
                yield (
                    f"foreach '{node.name}'.body.prompt",
                    node.name,
                    body.prompt,
                    False,
                    body_extras,
                )
            elif isinstance(body, ScriptNode) and body.command:
                yield (
                    f"foreach '{node.name}'.body.command",
                    node.name,
                    body.command,
                    False,
                    body_extras,
                )

    # parallel 组的 route.when 与 node 走相同 ⑦ 校验（组完成后路由的 Jinja2 引用
    # 也需浅校验，避免静默放行坏引用）。组的 self_name=None（组聚合输出在评估期可用）。
    for group in wf.parallel:
        for route in group.routes:
            if route.when:
                yield (
                    f"parallel 组 '{group.name}'.route.when",
                    None,
                    route.when,
                    True,
                    {"output"},
                )
            # phase-14：parallel 组 route.output 同 node（到 $end 输出变换）
            if route.output:
                for key, expr in route.output.items():
                    yield (
                        f"parallel 组 '{group.name}'.route.output.{key}",
                        None,
                        expr,
                        False,
                        set(),
                    )

    for key, expr in wf.outputs.items():
        yield (f"workflow.outputs.{key}", None, expr, False, set())


def _parse_for_meta(text: str, is_expression: bool):
    """parse 模板为 AST。裸表达式包进 {{ }} 再 parse。

    返回 (ast, None) 或 (None, 错误消息)——语法错视为校验错误（fail loud）。
    """
    if is_expression and "{{" not in text and "{%" not in text:
        src = "{{ " + text + " }}"
    else:
        src = text
    try:
        return _ENV.parse(src), None
    except TemplateSyntaxError as e:
        return None, f"模板语法错误：{e.message}"


def _workflow_input_keys(ast) -> list[str]:
    """提取 ``workflow.input.<key>`` 的 <key>（dotted 与 Getitem 字面量两种写法）。"""
    keys: list[str] = []
    # workflow.input.key —— Getattr(Getattr(Name('workflow'),'input'),'key')
    for n in ast.find_all(Getattr):
        inner = n.node
        if (
            isinstance(inner, Getattr)
            and isinstance(inner.node, Name)
            and inner.node.name == "workflow"
            and inner.attr == "input"
        ):
            keys.append(n.attr)
    # workflow.input['key'] —— Getitem(Getattr(Name('workflow'),'input'), Const('key'))
    # 注意 jinja2 Getitem 的索引字段是 .arg（不是 .index）
    for n in ast.find_all(Getitem):
        inner = n.node
        if (
            isinstance(inner, Getattr)
            and isinstance(inner.node, Name)
            and inner.node.name == "workflow"
            and inner.attr == "input"
            and isinstance(n.arg, Const)
        ):
            keys.append(n.arg.value)
    return keys


def _inputs_top_keys(ast) -> list[str]:
    """提取 ``inputs.<key>`` 与 ``inputs['<key>']`` 的 <key>（render 暴露的顶层 inputs）。

    与 ``_workflow_input_keys`` 平行：``inputs.X`` 是 render._namespace 暴露的等价写法
    （见 orca/exec/render.py）。提取 <key> 用于「X 是否在 wf.inputs 声明」的 warning 校验。
    """
    keys: list[str] = []
    # inputs.key —— Getattr(Name('inputs'), 'key')
    for n in ast.find_all(Getattr):
        inner = n.node
        if isinstance(inner, Name) and inner.name == "inputs":
            keys.append(n.attr)
    # inputs['key'] —— Getitem(Name('inputs'), Const('key'))
    for n in ast.find_all(Getitem):
        inner = n.node
        if isinstance(inner, Name) and inner.name == "inputs" and isinstance(n.arg, Const):
            keys.append(n.arg.value)
    return keys


def _check_jinja2_refs(wf: Workflow, result: ValidationResult) -> None:
    """浅校验：每个 undeclared 变量的 root 必须是真实 node / workflow / 上下文合法变量。

    不校验 ``.output.field`` 字段级（运行时归 run/，SPEC §4⑦）。``workflow.input.X``
    与 ``inputs.X`` 的 X 未声明 → warning（非致命）。

    ``inputs`` 是 render 层 ``_namespace`` 暴露的顶层变量（``{{ inputs.x }}``，
    见 orca/exec/render.py），与 ``workflow.input.X`` 等价 —— 两种写法都允许，
    X 未在 ``wf.inputs`` 声明 → warning（非致命，允许运行时注入未声明的 key）。
    """
    names = _jinja_root_set(wf)
    for location, _self_name, text, is_expr, extras in _iter_templates(wf):
        ast, err = _parse_for_meta(text, is_expr)
        if err is not None:
            result.add_error(f"{location}：{err}")
            continue
        # inputs 是 render 层合法顶层变量（{{ inputs.x }}）；subagents_root 是 point-to-file
        # 协议（SPEC §3.2/§4）render 层 ``_namespace`` 暴露的顶层变量（{{ subagents_root }}）。
        valid_roots = names | {"workflow", "inputs", "subagents_root"} | extras
        for var in sorted(find_undeclared_variables(ast)):
            if var not in valid_roots:
                result.add_error(
                    f"{location} 引用了不存在的 node/变量 '{var}'"
                )
        # workflow.input.X 与 inputs.X 的声明校验（warning）
        for key in _workflow_input_keys(ast) + _inputs_top_keys(ast):
            if key not in wf.inputs:
                result.add_warning(
                    f"{location} 引用了未声明的 workflow input '{key}'"
                )


# ── 引用合规深度校验（⑦ 之上的层；catch {%raw%} 误删类 bug）────────────────────


def _output_field_refs(ast) -> list[tuple[str, str | None]]:
    """提取 AST 里所有 ``<X>.output[.<field>]`` 一级引用，返回 ``[(node_name, field_or_None), ...]``。

    识别 4 种字面写法（dotted + subscript 两两组合）：

    - ``{{ X.output }}``              → ``(X, None)``  整段引用
    - ``{{ X['output'] }}``           → ``(X, None)``
    - ``{{ X.output.foo }}``          → ``(X, 'foo')`` 一级字段
    - ``{{ X['output']['foo'] }}``    → ``(X, 'foo')``
    - ``{{ X.output.foo.bar }}``      → ``(X, 'foo')`` 只取一级（bar 不归静态对齐管）

    覆盖自引用检测（``X == self``）+ output_schema 字段对齐（``foo ∈ schema.properties``）。

    不深拷嵌套字段（``foo.bar`` 的 ``bar`` 留给运行时；JSON schema 嵌套对齐 brittle）。
    ``{% raw %}`` 包裹的内容 Jinja2 parse 时记为 ``Const`` 文本节点，**不**进 ``find_all``，
    天然不在此函数返回值里 —— raw 包裹的自引用提及不会被误报。

    **双重发射（已知）**：同一条 ``X.output.foo`` 引用可能同时产 ``(X, 'foo')`` 与
    ``(X, None)`` 两条（``Getattr(X, output)`` 自身命中 branch 1，``Getattr(foo, …)``
    命中 branch 2）。消费者需按需过滤 ``field is None``（``_check_output_schema_field_alignment``
    显式跳过 ``field is None``；``_check_self_reference`` 另走 ``_unguarded_self_output_refs``
    递归 walker，不消费本函数）。
    """
    refs: list[tuple[str, str | None]] = []
    # outer 是 Getattr：field = outer.attr
    for outer in ast.find_all(Getattr):
        inner = outer.node
        if outer.attr == "output" and isinstance(inner, Name):
            # {{ X.output }} 整段引用（outer 即 output 根）
            refs.append((inner.name, None))
            continue
        if (
            isinstance(inner, Getattr)
            and inner.attr == "output"
            and isinstance(inner.node, Name)
        ):
            # {{ X.output.<outer.attr> }}
            refs.append((inner.node.name, outer.attr))
            continue
        if (
            isinstance(inner, Getitem)
            and isinstance(inner.arg, Const)
            and inner.arg.value == "output"
            and isinstance(inner.node, Name)
        ):
            # {{ X['output'].<outer.attr> }}
            refs.append((inner.node.name, outer.attr))
            continue
    # outer 是 Getitem：field = outer.arg.value（仅字面字符串索引）
    for outer in ast.find_all(Getitem):
        if not isinstance(outer.arg, Const) or not isinstance(outer.arg.value, str):
            continue
        field = outer.arg.value
        inner = outer.node
        if isinstance(inner, Name) and field == "output":
            # {{ X['output'] }} 整段引用
            refs.append((inner.name, None))
            continue
        if (
            isinstance(inner, Getattr)
            and inner.attr == "output"
            and isinstance(inner.node, Name)
        ):
            # {{ X.output['<field>'] }}
            refs.append((inner.node.name, field))
            continue
        if (
            isinstance(inner, Getitem)
            and isinstance(inner.arg, Const)
            and inner.arg.value == "output"
            and isinstance(inner.node, Name)
        ):
            # {{ X['output']['<field>'] }}
            refs.append((inner.node.name, field))
            continue
    return refs


def _check_self_reference(wf: Workflow, result: ValidationResult) -> None:
    """禁**无守卫的**自引用：prompt / command / values / foreach body 引用 ``<self>.output[.X]`` → error。

    语义（contract §3）：``prompt`` / ``command`` / ``values`` 在所属节点跑**之前**渲染，
    render context 只含上游节点的 ``ctx.outputs``，自身尚未产出 → ``<self>.output`` 必
    UndefinedError 崩。``route.when`` 与 ``route.output`` 在节点跑**之后**评估，self.output
    合法（``when: "output.json.kind == 'A'"``），故 self_name=None 不进此检查。

    动机：终审发现 ``agent-struct-exploration.yaml`` 的 ``{% raw %}`` 被误删 → setup prompt
    自引用 ``{{ setup.output.X }}`` → StrictUndefined 崩。``{% raw %}`` 修复后此规则零误报，
    且永久 catch 此类误删（raw 包裹的提及不进 AST 的 ref 集合，见 ``_output_field_refs``）。

    **守卫豁免（回环累加惯用法）**：set/agent 节点用 ``{{ self.output.n if self is defined
    and self.output is defined else fallback }}``（或 ``{% if %}`` 块）跨轮累加上轮 output
    是 runtime 合法的——首轮 self.output 未定义时，``is defined`` 短路使成立分支不评估，
    StrictUndefined 不崩。``_unguarded_self_output_refs`` 识别「成立分支 + test 合取含
    ``self.output is defined`` 守卫」的访问并放行；仅 ``self is defined`` 不够（不保护
    ``.output`` 子访问），部分守卫仍报错。``defined``/``undefined`` test 的被测表达式本身是
    守卫材料，不计违规。
    """
    for location, self_name, text, is_expr, _extras in _iter_templates(wf):
        if self_name is None:
            continue
        ast, err = _parse_for_meta(text, is_expr)
        if err is not None or ast is None:
            continue  # 语法错已由 ⑦ 报；此处不重复
        if _unguarded_self_output_refs(ast, self_name):
            result.add_error(
                f"{location} 自引用 '{self_name}.output'："
                f"prompt/command/values 在节点跑之前渲染，自身无 output 可读"
                f"（用 route.when 引用本节点 output，或改上游节点传递，"
                f"或用 `{{{{ {self_name} is defined and {self_name}.output is defined }}}}` "
                f"守卫的回环累加写法）"
            )


def _unguarded_self_output_refs(ast, self_name: str) -> list:
    """返回 AST 中**未受 ``is defined`` 守卫保护**的 ``self.output`` 访问节点。

    递归遍历（``_walk_self_ref``）维护 ``safe`` 上下文：

    - 命中 ``self.output`` 访问（``_is_self_output_access``）且非 ``safe`` → 记违规。
    - ``Test(name in 'defined'/'undefined')`` 的被测表达式是守卫材料 → 以 ``safe=True`` 下钻
      （不计违规；这两类 test 在 Undefined 上不崩，其余 ``is <X>`` 会崩 → 不豁免，正常计入）。
    - ``CondExpr`` / ``If``：若其 test 合取含 ``self.output is defined``（``_test_guards_self_output``）
      → 成立分支（``expr1`` / ``body``）标 ``safe``，否则分支不标。

    保守取舍（false-positive > false-negative）：识别不了的守卫形态一律不放行，仍报错。
    """
    return _walk_self_ref(ast, self_name, safe=False)


def _walk_self_ref(node, self_name: str, safe: bool) -> list:
    """``_unguarded_self_output_refs`` 的递归内核（见其 docstring）。"""
    violations: list = []
    if node is None:
        return violations
    if _is_self_output_access(node, self_name):
        if not safe:
            violations.append(node)
        return violations  # 子节点是 Name(self)，非访问，无需下钻
    # is defined / is undefined 的被测表达式 = 守卫材料，标 safe 不计违规
    if isinstance(node, Test) and node.name in ("defined", "undefined"):
        violations += _walk_self_ref(node.node, self_name, safe=True)
        return violations
    # 内联条件 A if B else C：成立分支 expr1 受 test 守卫则 safe，否则分支 expr2 不受
    if isinstance(node, CondExpr):
        guarded = _test_guards_self_output(node.test, self_name)
        violations += _walk_self_ref(node.test, self_name, safe)
        violations += _walk_self_ref(node.expr1, self_name, safe or guarded)
        violations += _walk_self_ref(node.expr2, self_name, safe)
        return violations
    # {% if %} 块：body 受 test 守卫则 safe；else_/elif_ 不受本 test 保护（elif 自带 test，
    # 递归时由其自身 If 分支按它的 test 判定，语境等同 outer 的否则分支）。
    if isinstance(node, If):
        guarded = _test_guards_self_output(node.test, self_name)
        violations += _walk_self_ref(node.test, self_name, safe)
        for n in node.body:
            violations += _walk_self_ref(n, self_name, safe or guarded)
        for n in node.else_:
            violations += _walk_self_ref(n, self_name, safe)
        for n in node.elif_:
            violations += _walk_self_ref(n, self_name, safe)
        return violations
    # 默认：下钻所有子节点，传递当前 safe
    for child in node.iter_child_nodes():
        violations += _walk_self_ref(child, self_name, safe)
    return violations


def _is_self_output_access(node, self_name: str) -> bool:
    """node 是否为 ``self.output`` 访问根（``X.output`` dotted 或 ``X['output']`` subscript）。"""
    if (
        isinstance(node, Getattr)
        and node.attr == "output"
        and isinstance(node.node, Name)
        and node.node.name == self_name
    ):
        return True
    return (
        isinstance(node, Getitem)
        and isinstance(node.arg, Const)
        and node.arg.value == "output"
        and isinstance(node.node, Name)
        and node.node.name == self_name
    )


def _test_guards_self_output(test, self_name: str) -> bool:
    """条件 test 的合取子句中是否含 ``self.output is defined`` 守卫。

    仅 ``self.output is defined``（直接测试 output 子访问）才算——它使首轮 self.output
    未定义时成立分支被短路跳过；单独 ``self is defined`` 不保护后续 ``.output`` 子访问，
    不算守卫。
    """
    for clause in _conjunction_clauses(test):
        if (
            isinstance(clause, Test)
            and clause.name == "defined"
            and _is_self_output_access(clause.node, self_name)
        ):
            return True
    return False


def _conjunction_clauses(node):
    """展平 ``And`` 合取为叶子子句序列（Jinja2 ``And.left/right`` 直接为操作数）。"""
    if isinstance(node, And):
        yield from _conjunction_clauses(node.left)
        yield from _conjunction_clauses(node.right)
    else:
        yield node


def _check_output_schema_field_alignment(
    wf: Workflow, result: ValidationResult
) -> None:
    """strict output_schema 字段对齐：模板引用 ``{{ X.output.foo }}``，``foo`` 必须 ∈ schema。

    规则（contract §2 agent output_schema）：当 ``X.output_schema`` 存在且
    ``additionalProperties: false``，模板引用的**一级字段** ``foo`` 必须 ∈
    ``output_schema.properties``，否则拼错必 render 后被 schema 拒 / UndefinedError。

    跳过情形：
    - ``X`` 无 output_schema（AgentNode 自由文本 / ScriptNode 固定字段 stdout/stderr/
      exit_code + parse_json 的 ``json``）。ScriptNode schema 字段不在 ``schema.py`` 里，
      恒为 None → 天然落入此跳过（``output.json.<X>`` 运行时解析，静态无法对齐）。
    - ``X.output_schema.additionalProperties`` 非 ``False``（schema 默认放行）。
    - 字段链 ``X.output`` 整段引用（``field=None``）—— 不字段级对齐，跳过。

    只对齐**一级**字段（``foo.bar`` 的 ``bar`` 留给运行时；嵌套 schema brittle 易误报）。
    """
    # X 名 → output_schema（仅 AgentNode 有此字段；ScriptNode/parallel 组不进表，自然跳过）。
    schema_map: dict[str, dict] = {}
    for node in wf.nodes:
        if not node.name:
            continue
        if isinstance(node, AgentNode) and node.output_schema is not None:
            schema_map[node.name] = node.output_schema

    for location, _self_name, text, is_expr, _extras in _iter_templates(wf):
        ast, err = _parse_for_meta(text, is_expr)
        if err is not None or ast is None:
            continue
        reported: set[tuple[str, str]] = set()  # 同模板内同(X,field)去重
        for node_name, field in _output_field_refs(ast):
            if field is None:
                continue  # 整段引用，不字段级对齐
            schema = schema_map.get(node_name)
            if schema is None:
                continue  # X 无 strict output_schema（AgentNode 自由文本 / ScriptNode / parallel 组）
            if schema.get("additionalProperties") is not False:
                continue  # schema 未强制关闭 additionalProperties
            properties = schema.get("properties") or {}
            if field in properties:
                continue
            key = (node_name, field)
            if key in reported:
                continue
            reported.add(key)
            result.add_error(
                f"{location} 引用了 node '{node_name}'.output 不存在的字段 '{field}'"
                f"（output_schema additionalProperties:false，合法字段："
                f"{sorted(properties.keys())}）"
            )


# folder-agent body 里 ``$ORCA_AGENT_RESOURCES/scripts/<file>`` 的 <file> 提取。
# allow-list 字符类 ``[A-Za-z0-9_\-./]`` 截断尾部（引号/反引号/空白/行尾等自然停止匹配），
# 支持子目录路径。**文本级正则非 AST 感知**：``{% raw %}`` 包裹的文档化示例（如 prompt
# 里写 "示例：$ORCA_AGENT_RESOURCES/scripts/example.py"）也会被检——已知限制，可接受
# （folder agent prompt 实际不会用 raw 包裹真实脚本路径）。
_AGENT_RESOURCE_SCRIPT_RE = re.compile(
    r"\$ORCA_AGENT_RESOURCES/scripts/([A-Za-z0-9_][A-Za-z0-9_\-./]*)"
)


def _check_folder_agent_scripts_exist(
    wf: Workflow, result: ValidationResult
) -> None:
    """文件夹/文件 agent 的 body 引用 ``$ORCA_AGENT_RESOURCES/scripts/<file>`` 必须存在。

    语义（contract §4 + create-workflow H1）：folder agent spawn 时 executor 注入
    ``ORCA_AGENT_RESOURCES`` 指向 agent 资源目录（resolver 物化为 ``node.resources_root``）；
    body 引用 ``$ORCA_AGENT_RESOURCES/scripts/<file>`` 等价读 ``<resources_root>/scripts/<file>``。
    缺失 → spawn 后 agent Bash 工具实际执行时崩（FileNotFoundError）。

    仅对 ``node.resources_root`` 已物化（agent 引用，由 resolver 填）的节点检查；
    内联 prompt（无 agent 引用）``resources_root=None``，无 ORCA_AGENT_RESOURCES 语义，跳过。
    只检查 ``node.prompt``（resolver 物化进来的 body）—— 不递归 .md 引用的二级文件
    （如 agent.md body 指向 SKILL.md，SKILL.md 再引用脚本；递归静态分析 brittle 超出范围）。
    """
    for node in wf.nodes:
        if isinstance(node, AgentNode):
            _check_scripts_exist_one(node, f"node '{node.name}'", result)
        elif isinstance(node, ForeachNode):
            body = node.body
            if isinstance(body, AgentNode):
                _check_scripts_exist_one(
                    body, f"foreach '{node.name}'.body", result
                )


def _check_scripts_exist_one(
    agent_node: AgentNode, location: str, result: ValidationResult
) -> None:
    """单 AgentNode 的 prompt + resources_root 一致性检查（DRY 提取）。"""
    if not agent_node.prompt or not agent_node.resources_root:
        return
    resources_root = Path(agent_node.resources_root)
    reported: set[str] = set()
    for match in _AGENT_RESOURCE_SCRIPT_RE.finditer(agent_node.prompt):
        rel = match.group(1)
        if rel in reported:
            continue
        reported.add(rel)
        # resolve 但不要求路径存在（existence 是本检查的判定，不是 resolve 的前提）
        target = resources_root / "scripts" / rel
        if not target.is_file():
            result.add_error(
                f"{location} 引用了 $ORCA_AGENT_RESOURCES/scripts/{rel}"
                f" 但脚本不存在（resources_root={resources_root}，"
                f"期望路径 {target}）"
            )


# input description 三档标签（contract §6 / create-workflow SKILL.md）。
# description 必须以 [ask]/[infer]/[default]/[advanced] 起头。
_TIER_LABELS = ("[ask]", "[infer]", "[default]", "[advanced]")


def _check_input_tier_labels(wf: Workflow, result: ValidationResult) -> None:
    """input description 必须以三档标签起头（contract §6 强制，warning 非阻断）。

    语义：三档标签是 in-session 编排器 / tars skill 读取 input 分类的机器可读前缀；
    缺标签 = input 不会被正确路由到 ask/infer/default 处理流。warning（非 error）：
    旧 workflow 可能缺标签，阻断会破坏既有可用性；用 warning 提示作者补齐。
    """
    for name, idef in wf.inputs.items():
        desc = idef.description or ""
        if not desc.startswith(_TIER_LABELS):
            result.add_warning(
                f"input '{name}' 的 description 未以三档标签起头"
                f"（[ask]/[infer]/[default]/[advanced]，contract §6）"
            )


# ── agent.md body 禁开发期残留（受众分离契约）──────────────────────────────────


# 开发期残留 pattern 表（无歧义开发上下文，运行时 prompt 用不到）。
# ``category`` 是面向作者的简短分类（用于 warning message）。
# 顺序即报告顺序（保持稳定，便于回归）。
#
# 不变量（critical）：每个 pattern 内部必须用 ``(?:...)`` 非捕获组——下方
# ``_DEV_RESIDUE_RE`` 用 ``(?P<c{i}>...)`` 把每个 pattern 整体命名为 ``c0``/``c1``/...，
# ``m.lastgroup`` 据此反查 ``_DEV_RESIDUE_PATTERNS[cat_idx]`` 取类别文案。若 pattern
# 内出现捕获组（去 ``?:``），``lastgroup`` 会指向内部组，类别映射 silent 错位。
_DEV_RESIDUE_PATTERNS: tuple[tuple[str, str], ...] = (
    # 引用 plan 编号（plan §9.1 / plan §N1 / plan §B2）。trailing 字符类吞掉完整编号
    # （N1 / 9.1 等），避免 warning 文案只显示截断的 "plan §N"。
    ("plan 编号", r"plan\s*§\s*[0-9INBivx][0-9A-Za-z.]*"),
    # spec/plan 节号（§9.1 / §2.3）—— 要求 N.M 形（避免误报枚举编号「§9 items」类）
    ("spec/plan 节号", r"§\s*[0-9]+\.[0-9]+"),
    # issue-tracker breadcrumb（中英文括号；I/N/B 前缀 + 数字）
    ("issue breadcrumb", r"[（(]\s*[INB]\d+"),
    # Orca 引擎源码路径:行号（运行时 agent 不需要读引擎源码作论据）。白名单子目录**不含**
    # ``skills`` —— agent 可合法 ``Read`` skill 资源（如 ``orca/skills/tars/SKILL.md``），
    # 属 operational 引用；也不含 ``runtime``（runtime/ 是只读辅助层，引用属 operational）。
    ("Orca 源码路径", r"orca/(?:compile|exec|run|iface|events|chart|profiles|schema|gates)/\S+?\.py(?::\d+)?"),
    # 内部 examples 路径作论据（examples/agents/<x>/agent.md）
    ("内部 examples 路径", r"examples/agents/[a-z0-9_-]+/agent\.md"),
)

_DEV_RESIDUE_RE = re.compile(
    "|".join(f"(?P<c{i}>{pat})" for i, (_cat, pat) in enumerate(_DEV_RESIDUE_PATTERNS))
)


def _check_prompt_dev_residue(wf: Workflow, result: ValidationResult) -> None:
    """agent.md body 禁开发期残留（受众分离契约，warning 非阻断）。

    动机（受众分离）：``agent.md`` body 是给 **LLM agent 的运行时指令**（只含 WHAT to do），
    不是给 reviewer / 未来自己的设计论证（WHY 属 commit / release-note / plan）。把 plan 节号
    （``§9.1``）、issue breadcrumb（``（I10）``）、Orca 源码路径（``orca/exec/env.py:91``）、
    内部 examples 路径作论据（``examples/agents/plotter/agent.md``）写进 prompt，等于把开发期
    上下文塞给不关心它的执行 agent——污染注意力 + 长期看让 prompt 漂成考古日志。两类受众各看各的文本。

    pattern 表（无歧义、零业务含义）：
      - ``plan §N`` / ``plan §9.1``             → plan 编号
      - ``§9.1`` / ``§2.3``                     → spec/plan 节号
      - ``（I10）`` / ``(N1`` / ``（B2``         → issue-tracker breadcrumb（中英文括号）
      - ``orca/exec/env.py:91``                 → Orca 引擎源码路径:行号
      - ``examples/agents/plotter/agent.md``    → 内部 examples 路径作论据

    **不 flag**（operational 合法串）：``$ORCA_AGENT_RESOURCES/...``、``orca.chart.render_chart``
    （API 调用，非源码路径——源码路径 regex 要求 ``.py`` 后缀 + 子目录名在白名单）、
    ``orca spawn 注入``、``Git Bash``、``tape``、``output_schema``、``Task(subagent_type=...)``、
    NAS block 库通用示例名（``swin_window`` / ``cswin`` 等）。

    warning-not-error 的取舍：deterministic 检测 + **不破坏现有 workflow**——既有 workflow 可能
    有残留，error 会阻断 ``tars validate`` 使其不可用；warning 让残留可见、作者按契约清理，执行靠
    契约（``orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md``）+ 受众
    翻转通读。

    扫描范围：仅 AgentNode.prompt body（folder-agent / file-agent，即 ``resources_root`` 已物化
    的节点），**跳过** inline 短 prompt（inline 无 agent.md、不适用本契约）；**不扫** references/
    assets/subagent 文件（那些是数据，非 prompt；references/ 与源字节一致，不该改）。foreach body
    agent 同样扫描（body prompt 亦是 LLM 运行时指令）。

    误报排除：源码路径 regex 仅匹配 ``orca/<subdir>/<file>.py[:<line>]``，operational API 路径
    （``orca.chart.render_chart``、``$ORCA_AGENT_RESOURCES/...``）天然不命中——它们不是文件系统
    路径（无 ``.py`` 后缀 / 无白名单子目录前缀）。
    """
    for node in wf.nodes:
        if isinstance(node, AgentNode):
            _check_dev_residue_one(node.prompt, node.resources_root,
                                   f"agent '{node.name}'", result)
        elif isinstance(node, ForeachNode):
            body = node.body
            if isinstance(body, AgentNode):
                _check_dev_residue_one(body.prompt, body.resources_root,
                                       f"foreach '{node.name}'.body agent", result)


def _check_dev_residue_one(
    prompt: str | None,
    resources_root: str | None,
    location: str,
    result: ValidationResult,
) -> None:
    """单 AgentNode 的 prompt 开发期残留扫描（DRY 提取，顶层 + foreach body 共用）。

    跳过 inline prompt（``resources_root is None`` 表示未被 resolver 物化，是内联短 prompt）。
    同一类别在同一节点内只报首条命中（避免刷屏；作者按类别清理即可）。
    """
    if not prompt or resources_root is None:
        return
    reported_cats: set[str] = set()
    for m in _DEV_RESIDUE_RE.finditer(prompt):
        # 命中的类别序号 = group name ``cN`` 中 N
        cat_idx = int(m.lastgroup[1:])
        category = _DEV_RESIDUE_PATTERNS[cat_idx][0]
        if category in reported_cats:
            continue
        reported_cats.add(category)
        matched = m.group(0)
        result.add_warning(
            f"{location}.prompt：含开发期残留 '{matched}'（{category}）"
            f"——agent 运行时 prompt 应只含执行指令；"
            f"设计理由/issue 编号/源码路径移至 commit 或 release-note。"
            f"契约见 orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md"
        )


# ── point-to-file subagent md 校验（SPEC §7）──────────────────────────────────


# strict frontmatter regex：仅匹配首块 ``---\n...---\n``（SPEC §5.2 evaluator #13 闭环）。
# 非整文件 yaml parse——body 后续 ``---``（markdown hr / 表格分隔）不误判。
_SUBAGENT_FRONTMATTER_RE = re.compile(
    r"^---\n(?P<yaml>.+?\n)---\n", re.DOTALL,
)
# frontmatter 内三键的捕获（multiline 容错；每键一行 ``key: value``）。
_FM_SUBAGENT_RE = re.compile(r"^subagent:\s*(?P<v>\S+)\s*$", re.MULTILINE)
_FM_VERSION_RE = re.compile(r"^version:\s*(?P<v>\d+)\s*$", re.MULTILINE)
_FM_SENTINEL_RE = re.compile(
    r"^sentinel:\s*(?P<v>[A-Za-z0-9]{4,})\s*$", re.MULTILINE,
)
# body 旧协议残留（read+embed 时代的 $ORCA_SUBAGENTS_DIR / cat ~/.orca/...subagents/）。
_SUBAGENT_LEGACY_RESIDUE_RE = re.compile(
    r"\$ORCA_SUBAGENTS_DIR\b|cat\s+(?:\"\$HOME|\$HOME)/\.orca/[\w.-]+/subagents/",
)
# ``subagents_root`` 引用 → host 通用类型 tools 含 Read（静态可探：node.tools 显式白名单
# 或 frontmatter meta tools）。tools==None（全开）= 含 Read；显式 list 才校验。
#
# **与 render.py ``_SUBAGENTS_ROOT_TOKEN`` 的关系（刻意不同）**：本 regex 精确匹配
# ``{{ subagents_root }}`` var-ref 形态（compile 期静态校验，假阳性代价高，需精确）；
# render.py 用子串 ``in`` 探测（run 期兜底，确定性优先——任何位置提及都视为依赖，
# 宁可假阳性 fail loud 也不放行）。两者判定集不相等是 design intent。
_SUBAGENTS_ROOT_REF_RE = re.compile(r"\{\{\s*subagents_root\s*\}\}")


def _parse_subagent_frontmatter(text: str) -> dict | None:
    """strict regex 解析 subagent md frontmatter（SPEC §5.2）。

    返 ``{"subagent", "version", "sentinel"}`` dict（首块 frontmatter，body 后续 ``---`` 不误判）；
    无 frontmatter / 缺键 → ``None``（调用方按 error 上报）。consumer（parent 校验回显 / lint）
    统一用此函数——禁用整文件 yaml parse。
    """
    m = _SUBAGENT_FRONTMATTER_RE.match(text)
    if not m:
        return None
    yaml_block = m.group("yaml")
    sub = _FM_SUBAGENT_RE.search(yaml_block)
    ver = _FM_VERSION_RE.search(yaml_block)
    sen = _FM_SENTINEL_RE.search(yaml_block)
    if not (sub and ver and sen):
        return None
    return {
        "subagent": sub.group("v"),
        "version": int(ver.group("v")),
        "sentinel": sen.group("v"),
    }


def _check_subagents_md(
    wf: Workflow, workflows_root: Path | None, result: ValidationResult
) -> None:
    """point-to-file subagent md 校验（SPEC §7，仅当 subagents_root 解析到存在目录时跑）。

    三项校验：
      1. 目录内每个 ``*.md`` 必有合法 frontmatter（``subagent`` / ``version`` / ``sentinel``
         三键）——strict regex 解析（SPEC §5.2，非整文件 yaml parse）。
      2. body 含 ``$ORCA_SUBAGENTS_DIR`` / ``cat ~/.orca/...subagents/`` 等旧协议残留 →
         warning（dev-residue）。
      3. agent.md body 引用 ``{{ subagents_root }}`` 的节点 → 校验 host 通用类型 tools 含
         Read（静态可知则校验；tools=None=全开视为含 Read）。

    ``workflows_root=None`` 时回退 ``wf.workflows_root``（load_workflow 加载期绑定，
    单一真源）。目录不存在时：若没有任何模板引用 ``{{ subagents_root }}`` → 跳过
    （SPEC §3.3：无 subagents 的 workflow 正常）；若引用了 → **load 期 fail loud**
    （确定性错误在确定性阶段暴露，而非 run 中途 render 才炸）。run 期 render 兜底
    （``{{ subagents_root }}`` 但 ctx.subagents_root=""）保留作纵深防御（防程序化
    构造的 wf）。
    """
    if workflows_root is None:
        workflows_root = wf.workflows_root
    if workflows_root is None or not wf.name:
        return
    subagents_root = workflows_root / "subagents" / wf.name
    if not subagents_root.is_dir():
        referencing = [
            location
            for location, _self_name, text, _is_expr, _extras in _iter_templates(wf)
            if text and _SUBAGENTS_ROOT_REF_RE.search(text)
        ]
        if referencing:
            result.add_error(
                f"模板引用了 {{{{ subagents_root }}}} 但解析目录不存在："
                f"{subagents_root}（位置：{'、'.join(referencing)}）。"
                "子 agent body 目录缺失：dev 态请确认 workflow yaml 位于 repo "
                "``workflows/`` 下（subagents/ 与之同级）；已安装环境请先 ``tars install`` "
                "（部署到 ~/.orca/workflows/subagents/）。"
            )
        return
    md_files = sorted(subagents_root.glob("*.md"))
    for md in md_files:
        try:
            text = md.read_text(encoding="utf-8")
        except OSError as e:
            result.add_warning(
                f"读取 subagent md {md.name} 失败（{e}）；跳过其 frontmatter 校验"
            )
            continue
        fm = _parse_subagent_frontmatter(text)
        if fm is None:
            result.add_error(
                f"subagent md '{md.name}' 缺合法 frontmatter（需 ``subagent`` / "
                f"``version`` / ``sentinel`` 三键，strict regex 解析首块 ``---`` 块）。"
                f"详见 SPEC subagent-point-to-file-design-draft §5.2。"
            )
        elif fm["subagent"] != md.stem:
            result.add_error(
                f"subagent md '{md.name}' frontmatter 的 subagent={fm['subagent']!r}"
                f" 与文件名 stem {md.stem!r} 不一致（SPEC §5.2：subagent = 文件名 stem）。"
            )
        if _SUBAGENT_LEGACY_RESIDUE_RE.search(text):
            result.add_warning(
                f"subagent md '{md.name}' body 含旧协议残留（$ORCA_SUBAGENTS_DIR / "
                f"cat ~/.orca/.../subagents/）——point-to-file 协议下子 agent 自读 md，"
                f"无需 env var 或 cat $HOME 路径。"
            )
        # 开发期残留（受众分离契约 §8 #5 扩扫子 agent md）：复用 _DEV_RESIDUE_RE 的 pattern
        # 表（plan §N / §N.M / issue breadcrumb / Orca 源码路径 / 内部 examples 路径）。
        # 与 _check_dev_residue_one 同款「同类别只报首条命中」去重逻辑，但 category 文案一致。
        reported_cats: set[str] = set()
        for m in _DEV_RESIDUE_RE.finditer(text):
            cat_idx = int(m.lastgroup[1:])
            category = _DEV_RESIDUE_PATTERNS[cat_idx][0]
            if category in reported_cats:
                continue
            reported_cats.add(category)
            result.add_warning(
                f"subagent md '{md.name}'：含开发期残留 '{m.group(0)}'（{category}）"
                f"——子 agent md 是运行时指令，设计理由/issue 编号/源码路径移至 commit 或 release-note。"
                f"契约见 orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md"
            )

    # 校验 3：agent.md 引用 {{ subagents_root }} 的节点，host 通用类型 tools 须含 Read。
    for node in wf.nodes:
        if isinstance(node, AgentNode):
            _check_subagent_root_ref_tools(node, f"node '{node.name}'", result)
        elif isinstance(node, ForeachNode):
            body = node.body
            if isinstance(body, AgentNode):
                _check_subagent_root_ref_tools(
                    body, f"foreach '{node.name}'.body agent", result
                )


def _check_subagent_root_ref_tools(
    agent_node: AgentNode, location: str, result: ValidationResult
) -> None:
    """单 AgentNode：若 prompt 引用 ``{{ subagents_root }}`` 则校验 tools 含 Read。

    tools=None（默认全开）= host 通用类型全工具集（含 Read），跳过；显式 list 才校验。
    缺 Read → error（SPEC §5.5 fail loud 前移 compile——render 期子 agent 必须 Read md body）。
    """
    if not agent_node.prompt:
        return
    if not _SUBAGENTS_ROOT_REF_RE.search(agent_node.prompt):
        return
    if agent_node.tools is None:
        return  # 全开（默认），含 Read
    # 大小写无关匹配：opencode 工具名小写（``read``），claude 工具名首字母大写（``Read``）。
    # 三壳共用契约（SPEC §5.5）：任一 host 的 Read 工具名形态都接受。
    tools_lower = [t.lower() for t in agent_node.tools]
    if "read" not in tools_lower:
        result.add_error(
            f"{location} 引用 {{{{ subagents_root }}}}（point-to-file 子 agent 自读 md），"
            f"但其 tools 白名单 {agent_node.tools!r} 缺 'Read'（大小写无关；SPEC §5.5："
            f"host 通用类型须含 Read——opencode 为 read，claude 为 Read）。"
        )





def _check_foreach_source(wf: Workflow, result: ValidationResult) -> None:
    """source 形如 ``finder.output.candidates`` 的 dotted 路径，首段必须是真实 node。

    不校验字段是否存在/是否数组（运行时归 run/，SPEC §4⑧）。

    同时校验 ``max_concurrent >= 1``（编译期 fail loud，避免 run 层
    ``asyncio.Semaphore(max(1, ...))`` 静默把 0 改成 1）。
    """
    names = _name_set(wf)
    for node in wf.nodes:
        if not isinstance(node, ForeachNode):
            continue
        first = node.source.split(".")[0].strip()
        if first not in names:
            result.add_error(
                f"foreach 节点 '{node.name}' 的 source '{node.source}' "
                f"引用了不存在的 node '{first}'"
            )
        if node.max_concurrent < 1:
            result.add_error(
                f"foreach 节点 '{node.name}' 的 max_concurrent={node.max_concurrent} "
                "必须 >= 1（并发上限不能为 0 或负数）"
            )


# ── terminate step 约束校验（routes 空 / 非entry / 非parallel branch / 非foreach body）──


def _check_terminate_constraints(wf: Workflow, result: ValidationResult) -> None:
    """``TerminateNode`` 的 4 项 fail loud 约束（terminate step）：

      1. ``routes`` 必须空（terminate 触达即终止，不评估路由；非空 routes 是死代码 + 语义冲突）
      2. 不能作为 ``wf.entry``（terminate 必须先经业务节点才有意义；entry 即 terminate
         等于 workflow 永远立即终止，是配置错误）
      3. 不能在 ``ParallelGroup.branches`` 里（terminate 表达「整个 workflow 终止」，
         在并行分支里语义不清——其它分支如何处理？同 Conductor 限制）
      4. ``ForeachNode.body`` 不能含 terminate：schema 层已通过 ``ForeachBody`` 判别联合
         （仅 agent/script）拦掉；此处不做兜底（pydantic 解析阶段就 raise，到不了这里）。

    校验动机（铁律 4 fail loud）：terminate 是工作流作者的**显式**业务退出声明；放错位置
    （routes 非空 / 当 entry / 在并行组里）会在运行时产生混乱（routes 被静默忽略 / workflow
    从入口立即结束 / parallel 组因某 branch terminate 而 abort 其它 branch），编译期暴露比
    运行期调试便宜得多。
    """
    # 1) routes 必须空 + 2) 非 entry
    for node in wf.nodes:
        if not isinstance(node, TerminateNode):
            continue
        if node.routes:
            result.add_error(
                f"terminate 节点 '{node.name}' 的 routes 必须为空（terminate 触达即终止，"
                f"不评估路由；当前有 {len(node.routes)} 条 routes 是死代码 + 语义冲突）"
            )
        if wf.entry == node.name:
            result.add_error(
                f"terminate 节点 '{node.name}' 不能作为 workflow.entry（terminate 必须"
                "先经业务节点才有意义；entry 即 terminate 等于 workflow 永远立即终止）"
            )

    # 3) parallel 组的 branches 不能含 terminate node
    # _name_set 已含全部顶层 node 名，但 terminate 是按 name 找——把所有 terminate
    # node 名收集起来，对每个 parallel 组的 branches 做交集判定。
    terminate_names = {
        n.name for n in wf.nodes if isinstance(n, TerminateNode) and n.name
    }
    for group in wf.parallel:
        bad_branches = [b for b in group.branches if b in terminate_names]
        for b in bad_branches:
            result.add_error(
                f"parallel 组 '{group.name}' 的 branch '{b}' 是 terminate 节点"
                "（terminate 表达整个 workflow 的终止，不能在并行分支里）"
            )


# ── 铁律 7：execute phase 永不中断 ─────────────────────────────────────────────


# execute phase 禁配的「中断类」工具名（ask_user / gate）。显式配置 → 编译期 fail loud。
_INTERRUPT_TOOL_NAMES = {"ask_user", "gate"}


def _check_execute_phase_no_gate_tools(
    wf: Workflow, result: ValidationResult
) -> None:
    """§0.1 铁律 7：execute phase（``wf.nodes``）的 AgentNode 不配 ask_user/gate。

    ``AgentNode.tools`` 语义：``None`` = 全开（默认，由 orchestrator 据壳决定实际注入）；
    ``[...]`` = 显式白名单。本检查只对**显式白名单**拦截——若用户显式列了 ask_user/gate，
    说明意图让 execute agent 中断，与铁律 7 冲突 → fail loud。

    ``None``（全开）不拦截：实际是否注入 ask_user/gate 由 orchestrator + 壳模式决定
    （MCP 壳不注入 gate）。compile 层无法静态判定壳模式，故 None 留给 runtime 把关。
    与 phase-12 capability 校验正交（不依赖 CapabilitySet）。

    同时校验 foreach body agent（body 也在 execute phase，同理不可配中断工具）。
    """
    for node in wf.nodes:
        if isinstance(node, AgentNode):
            _check_no_interrupt_tools(node.tools, f"agent '{node.name}'", result)
        elif isinstance(node, ForeachNode):
            body = node.body
            if isinstance(body, AgentNode):
                _check_no_interrupt_tools(
                    body.tools, f"foreach '{node.name}'.body agent", result
                )


def _check_no_interrupt_tools(
    tools: list[str] | None, location: str, result: ValidationResult
) -> None:
    """单个 agent 的 tools 白名单里若含 ask_user/gate → 加 error（DRY 提取）。"""
    if tools is None:
        return
    bad = _INTERRUPT_TOOL_NAMES & set(tools)
    if bad:
        result.add_error(
            f"{location} 配了中断类工具 {sorted(bad)}"
            f"（铁律 7：execute phase 永不中断）"
        )


# ── ⑨ capability 校验（profiles/validate 产出 issue → 汇入 result）──────────────


def _check_profiles(wf: Workflow, result: ValidationResult) -> None:
    """⑨ capability 校验：调 ``profiles.validate_workflow_profiles``，issue 汇入 result。

    单向依赖：``compile → profiles``（profiles 不 import compile，SPEC §4.9）。
    issue.severity 决定 add_error / add_warning，仍走 ``raise_if_errors`` 聚合裁决
    （与其余 8 项结构校验共存，一次报全；含 ⑨ 共 9 项）。

    规则仅基于 AgentNode 真实字段（executor / output_schema / foreach body），不自创字段。
    """
    from orca.profiles import validate_workflow_profiles  # 单向依赖 compile → profiles

    for issue in validate_workflow_profiles(wf):
        msg = f"node '{issue.node}': {issue.message}"
        if issue.severity == "error":
            result.add_error(msg)
        else:
            result.add_warning(msg)
