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

from dataclasses import dataclass, field
from typing import Iterable

from jinja2 import Environment
from jinja2.exceptions import TemplateSyntaxError
from jinja2.meta import find_undeclared_variables
from jinja2.nodes import Const, Getattr, Getitem, Name

from orca.schema import (
    AgentNode,
    ForeachNode,
    ParallelGroup,
    ScriptNode,
    SetNode,
    Workflow,
)

# 单例 Environment：仅用于 parse + meta 解析，绝不调用 render（渲染归 run/）。
_ENV = Environment()


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


def validate_workflow(wf: Workflow) -> list[str]:
    """全部语义校验。返回 warnings；有 errors 抛 ConfigurationError（SPEC §4）。"""
    result = ValidationResult()
    _check_names_unique(wf, result)            # ①（含 parallel 组名）
    _check_entry_exists(wf, result)            # ②
    _check_entry_is_node(wf, result)           # ⑬ entry 非 parallel 组
    _check_route_refs_valid(wf, result)        # ④（node + parallel 组 routes）
    _check_entry_reachable_to_end(wf, result)  # ⑥（routes 前向边 + parallel 展开）
    _check_parallel_groups(wf, result)         # ⑩ parallel 组结构
    _check_route_fallback_last(wf, result)     # ⑪ 兜底 route 位置（node + parallel 组）
    _check_jinja2_refs(wf, result)             # ⑦
    _check_foreach_source(wf, result)          # ⑧
    _check_profiles(wf, result)                # ⑨ capability 校验（profiles/validate）
    return result.raise_if_errors()


# ── helpers：命名空间（node 名 + parallel 组名）──────────────────────────────


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


# ── ⑦ Jinja2 引用浅校验 ──────────────────────────────────────────────────────


def _iter_templates(
    wf: Workflow,
) -> Iterable[tuple[str, str, bool, set[str]]]:
    """产出 (位置, 文本, 是否裸表达式, 额外合法 root)。

    覆盖所有 Jinja2 模板字段（plan §7-B 裁决：不止 prompt/when/outputs）：
    AgentNode.prompt / ScriptNode.command / SetNode.values / Route.when（node 与
    parallel 组两侧）/ Workflow.outputs / foreach body 的 prompt·command。
    额外合法 root：when 允许 ``output``（当前 node 自身输出）；foreach body 允许
    ``item_var`` / ``index_var``。
    """
    for node in wf.nodes:
        if isinstance(node, AgentNode) and node.prompt:
            yield (f"node '{node.name}'.prompt", node.prompt, False, set())
        elif isinstance(node, ScriptNode) and node.command:
            yield (f"node '{node.name}'.command", node.command, False, set())
        elif isinstance(node, SetNode):
            for key, expr in node.values.items():
                yield (f"node '{node.name}'.values.{key}", expr, False, set())

        for route in node.routes:
            if route.when:
                # when 是裸表达式（无 {{ }}），允许引用本节点自身输出 output
                yield (
                    f"node '{node.name}'.route.when",
                    route.when,
                    True,
                    {"output"},
                )

        if isinstance(node, ForeachNode):
            body_extras = {node.item_var, node.index_var}
            body = node.body
            if isinstance(body, AgentNode) and body.prompt:
                yield (
                    f"foreach '{node.name}'.body.prompt",
                    body.prompt,
                    False,
                    body_extras,
                )
            elif isinstance(body, ScriptNode) and body.command:
                yield (
                    f"foreach '{node.name}'.body.command",
                    body.command,
                    False,
                    body_extras,
                )

    # parallel 组的 route.when 与 node 走相同 ⑦ 校验（组完成后路由的 Jinja2 引用
    # 也需浅校验，避免静默放行坏引用）。
    for group in wf.parallel:
        for route in group.routes:
            if route.when:
                yield (
                    f"parallel 组 '{group.name}'.route.when",
                    route.when,
                    True,
                    {"output"},
                )

    for key, expr in wf.outputs.items():
        yield (f"workflow.outputs.{key}", expr, False, set())


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
    for location, text, is_expr, extras in _iter_templates(wf):
        ast, err = _parse_for_meta(text, is_expr)
        if err is not None:
            result.add_error(f"{location}：{err}")
            continue
        # inputs 是 render._namespace 暴露的顶层变量（{{ inputs.x }}）
        valid_roots = names | {"workflow", "inputs"} | extras
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


# ── ⑧ foreach.source 的 node 存在（浅校验）──────────────────────────────────


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
