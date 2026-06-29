"""validator.py —— 语义校验层（SPEC §4 的 8 项 + warnings）。

结构校验（字段/类型/extra/discriminator）由 schema 层 pydantic 完成；本模块只做
**语义校验**：图结构（name 唯一/entry/引用/环/可达）+ Jinja2 引用浅校验。

设计原则：
- **聚合**：8 个 `_check_*` 全部往同一个 `ValidationResult` 加，最后统一 raise，
  绝不第一个错就抛（SPEC §1 决策 1-B，LLM 生成 YAML 常多处错，一次报全）。
- **fail loud + 精确**：每个错误指明哪个 node / 哪条边 / 哪个引用错了。
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
    _check_names_unique(wf, result)            # ①
    _check_entry_exists(wf, result)            # ②
    _check_after_refs_valid(wf, result)        # ③
    _check_route_refs_valid(wf, result)        # ④
    _check_after_acyclic(wf, result)           # ⑤
    _check_entry_reachable_to_end(wf, result)  # ⑥
    _check_jinja2_refs(wf, result)             # ⑦
    _check_foreach_source(wf, result)          # ⑧
    return result.raise_if_errors()


# ── helpers：顶层 node 名集合 ─────────────────────────────────────────────────


def _top_level_names(wf: Workflow) -> list[str]:
    """顶层 node 的 name（foreach 的无名 body 不在 wf.nodes，天然排除）。"""
    return [n.name for n in wf.nodes if n.name]


def _name_set(wf: Workflow) -> set[str]:
    return set(_top_level_names(wf))


# ── ① name 非空 + 全局唯一 ───────────────────────────────────────────────────


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
    for name, count in counts.items():
        if count > 1:
            result.add_error(f"node 名重复：'{name}' 出现 {count} 次")


# ── ② entry 存在 ─────────────────────────────────────────────────────────────


def _check_entry_exists(wf: Workflow, result: ValidationResult) -> None:
    if wf.entry not in _name_set(wf):
        result.add_error(f"entry '{wf.entry}' 不存在于 nodes 中")


# ── ③ after 引用有效 ─────────────────────────────────────────────────────────


def _check_after_refs_valid(wf: Workflow, result: ValidationResult) -> None:
    names = _name_set(wf)
    for node in wf.nodes:
        if not node.name:
            continue
        for dep in node.after:
            if dep not in names:
                result.add_error(
                    f"node '{node.name}' 的 after 引用了不存在的 node '{dep}'"
                )


# ── ④ routes[].to 引用有效 ───────────────────────────────────────────────────


def _check_route_refs_valid(wf: Workflow, result: ValidationResult) -> None:
    names = _name_set(wf)
    for node in wf.nodes:
        if not node.name:
            continue
        for route in node.routes:
            if route.to != "$end" and route.to not in names:
                result.add_error(
                    f"node '{node.name}' 的 route 引用了不存在的目标 '{route.to}'"
                )


# ── ⑤ after 静态边无环（Kahn 拓扑）──────────────────────────────────────────


def _check_after_acyclic(wf: Workflow, result: ValidationResult) -> None:
    """仅 after 静态边构图；routes 是条件边（回指=合法循环），不参与环检测（SPEC §4⑤）。"""
    names = _top_level_names(wf)
    name_set = set(names)

    # preds[X] = {A : X 依赖 A（A in X.after）}；graph[A] = {X : A→X}
    preds: dict[str, set[str]] = {n: set() for n in names}
    for node in wf.nodes:
        if not node.name:
            continue
        for dep in node.after:
            if dep in name_set:
                preds[node.name].add(dep)
    graph: dict[str, set[str]] = {n: set() for n in names}
    for x, deps in preds.items():
        for a in deps:
            graph[a].add(x)

    # Kahn 拓扑排序：入度=前置依赖数
    indeg = {x: len(preds[x]) for x in names}
    queue = [x for x in names if indeg[x] == 0]
    consumed = 0
    while queue:
        n = queue.pop()
        consumed += 1
        for m in graph[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)

    if consumed != len(names):
        # 剩余节点即在环中；DFS 取一条具体环路径写进消息（Kahn 只能判存在性）
        remaining = {n for n in names if indeg[n] > 0}
        cycle = _find_cycle_path(graph, remaining)
        result.add_error(f"检测到 after 静态依赖环：{' → '.join(cycle)}")


def _find_cycle_path(
    graph: dict[str, set[str]], remaining: set[str]
) -> list[str]:
    """在 remaining 子图里 DFS 找一条环，返回闭合路径 [a, b, ..., a]。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {n: WHITE for n in remaining}
    stack: list[str] = []

    def dfs(u: str) -> list[str] | None:
        color[u] = GRAY
        stack.append(u)
        for v in graph[u]:
            if v not in remaining:
                continue
            if color[v] == GRAY:
                idx = stack.index(v)
                return [*stack[idx:], v]  # 闭合环
            if color[v] == WHITE:
                found = dfs(v)
                if found:
                    return found
        color[u] = BLACK
        stack.pop()
        return None

    for n in remaining:
        if color[n] == WHITE:
            found = dfs(n)
            if found:
                return found
    return sorted(remaining)  # 兜底：理论上不可达（Kahn 已判定有环）


# ── ⑥ entry 可达终态（$end）──────────────────────────────────────────────────


def _check_entry_reachable_to_end(wf: Workflow, result: ValidationResult) -> None:
    """从 entry 沿 after+routes 前向边走，必须能到终态。死胡同=error，孤立=warning。

    裁决（plan §7-A）：``routes`` 为空的节点视为隐式终态——否则 parallel_research/
    batch_assess 的 sink 节点会被误判死胡同（SPEC §6.2 要求这 3 个 example 通过）。
    """
    names = _top_level_names(wf)
    by_name = {n.name: n for n in wf.nodes if n.name}
    if wf.entry not in by_name:
        return  # ② 已报，避免级联

    def successors(node) -> set[str]:
        """前向边：route 目标（非 $end）+ after 反向边（谁依赖本节点）。"""
        out = {r.to for r in node.routes if r.to != "$end"}
        for other in wf.nodes:
            if other.name and node.name in other.after:
                out.add(other.name)
        return out

    def is_terminal(node) -> bool:
        # 无 route = 隐式终态；否则要有显式 to="$end"
        return (not node.routes) or any(r.to == "$end" for r in node.routes)

    # can_end 不动点：terminal 或存在可到终态的后继（route 可成环，不动点自然收敛）
    can_end: dict[str, bool] = {n: is_terminal(by_name[n]) for n in names}
    changed = True
    while changed:
        changed = False
        for n in names:
            if can_end[n]:
                continue
            for m in successors(by_name[n]):
                if m in can_end and can_end[m]:
                    can_end[n] = True
                    changed = True
                    break

    # 从 entry BFS 求可达集
    reachable: set[str] = set()
    queue = [wf.entry]
    while queue:
        n = queue.pop()
        if n in reachable or n not in by_name:
            continue
        reachable.add(n)
        queue.extend(successors(by_name[n]))

    # 可达却到不了终态 = 死胡同（error）。合并为一条消息列出所有死胡同节点，
    # 避免路由环场景下对每个节点重复报近义错误。
    dead = sorted(n for n in names if n in reachable and not can_end[n])
    if dead:
        result.add_error(
            f"从 entry 无法到达 $end（死胡同节点：{', '.join(dead)}）"
        )
    # 从 entry 不可达 = 孤立（warning，不阻止）
    for n in names:
        if n not in reachable:
            result.add_warning(
                f"孤立节点：'{n}' 从 entry 不可达（可能忘了接线）"
            )


# ── ⑦ Jinja2 引用浅校验 ──────────────────────────────────────────────────────


def _iter_templates(
    wf: Workflow,
) -> Iterable[tuple[str, str, bool, set[str]]]:
    """产出 (位置, 文本, 是否裸表达式, 额外合法 root)。

    覆盖所有 Jinja2 模板字段（plan §7-B 裁决：不止 prompt/when/outputs）：
    AgentNode.prompt / ScriptNode.command / SetNode.values / Route.when /
    Workflow.outputs / foreach body 的 prompt·command。
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


def _check_jinja2_refs(wf: Workflow, result: ValidationResult) -> None:
    """浅校验：每个 undeclared 变量的 root 必须是真实 node / workflow / 上下文合法变量。

    不校验 ``.output.field`` 字段级（运行时归 run/，SPEC §4⑦）。``workflow.input.X``
    的 X 未声明 → warning（非致命）。
    """
    names = _name_set(wf)
    for location, text, is_expr, extras in _iter_templates(wf):
        ast, err = _parse_for_meta(text, is_expr)
        if err is not None:
            result.add_error(f"{location}：{err}")
            continue
        valid_roots = names | {"workflow"} | extras
        for var in sorted(find_undeclared_variables(ast)):
            if var not in valid_roots:
                result.add_error(
                    f"{location} 引用了不存在的 node/变量 '{var}'"
                )
        for key in _workflow_input_keys(ast):
            if key not in wf.inputs:
                result.add_warning(
                    f"{location} 引用了未声明的 workflow input '{key}'"
                )


# ── ⑧ foreach.source 的 node 存在（浅校验）──────────────────────────────────


def _check_foreach_source(wf: Workflow, result: ValidationResult) -> None:
    """source 形如 ``finder.output.candidates`` 的 dotted 路径，首段必须是真实 node。

    不校验字段是否存在/是否数组（运行时归 run/，SPEC §4⑧）。
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
