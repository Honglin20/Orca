"""parser.py —— YAML → 校验过的 Workflow（SPEC §3 + phase-14 agent 一等化）。

四步流水线：读 YAML → pydantic 结构校验 → **agent 引用解析物化** → 语义校验。
对外只暴露 ``load_workflow``（对外极简，内部校验要全——学 Conductor）。

phase-14：``_resolve_agents`` 替代旧 ``_load_prompts``。agent 引用（``agent: <name>``）
经 ``AgentResolver`` 物化进 ``node.prompt`` + ``node.resources_root``，合并 frontmatter meta。

**物化时序（实现期裁定，修正 SPEC §4.1 隐含缺陷）**：互斥预检（prompt+agent 同时非空 →
error）与 foreach body 双 None 预检（body 无 name 不能 fallback → error）**必须在物化前**——
因为物化会把 agent 引用的 ``node.prompt`` 从 None 填成内容，若互斥校验放物化后的
``validate_workflow``，会因"prompt+agent 都非空"对合法 agent 引用**误报**互斥违反。
故这两个与物化时序强相关的预检放 ``_resolve_agents`` 的同一遍历（先预检再物化），
``validate_workflow`` 只做物化后的语义校验（Route.output 等）。

依赖单向：本模块 → ``orca.compile.agents``（AgentResolver）+ ``orca.compile.validator``
（validate_workflow + ConfigurationError）+ ``orca.schema``。零反向依赖：不 import run/exec/events/iface。
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from pathlib import Path

import yaml
from pydantic import ValidationError

from orca.compile.agents import (
    AgentHandle,
    AgentNotFound,
    AgentResolver,
    LocalPoolResolver,
    ResolveContext,
)
from orca.compile.validator import ConfigurationError, validate_workflow
from orca.schema import AgentNode, ForeachNode, Workflow


def load_workflow(path: str | Path, resolver: AgentResolver | None = None) -> Workflow:
    """YAML 文件 → 校验过的 Workflow。失败抛 ConfigurationError（含所有 errors+warnings）。

    phase-14：可选注入 ``resolver``（默认 ``LocalPoolResolver``）。旧约定（prompt 省略 +
    name 匹配 md）经 ``warnings.warn(DeprecationWarning)`` 发出——CLI 用
    ``catch_warnings(record=True)`` 捕获展示，测试用 ``recwarn``（SPEC §4.1 / §7.2）。

    失败模式（全部 fail loud，SPEC §3）：
      - YAML 语法错 → ``yaml.YAMLError`` 透传
      - pydantic 结构错 → 包装成 ConfigurationError（对外单一错误类型）
      - agent 引用缺失 / 互斥违反 / foreach body 双 None → ConfigurationError（聚合）
      - 语义校验失败 → ConfigurationError（含所有 errors + warnings）
    """
    yaml_path = Path(path)
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))

    try:
        wf = Workflow(**raw)
    except ValidationError as e:
        # 对外只暴露一种错误类型：把 pydantic 结构错包装成 ConfigurationError
        raise ConfigurationError([f"结构校验失败：{e}"], []) from e

    if resolver is None:
        resolver = LocalPoolResolver()
    context = ResolveContext(
        workflow_dir=yaml_path.parent,
        cwd=Path.cwd(),
        extra_roots=(),
    )
    _resolve_agents(wf, resolver, context)  # 物化 + 物化前预检（互斥 / body 双 None）
    validate_workflow(wf)                    # 物化后语义校验（Route.output + 现有 9 项）
    return wf


def _iter_agent_nodes(
    wf: Workflow,
) -> Iterator[tuple[AgentNode, bool, str | None]]:
    """遍历所有 AgentNode（顶层 + foreach body），yield ``(node, is_body, parent_name)``。

    foreach body 的 AgentNode 无独立 name（``is_body=True``，``parent_name``=foreach 名），
    旧约定 name-fallback 不适用（body 必须显式 ``agent:`` 或内联 ``prompt:``）。
    """
    for node in wf.nodes:
        if isinstance(node, AgentNode):
            yield (node, False, None)
        elif isinstance(node, ForeachNode):
            body = node.body
            if isinstance(body, AgentNode):
                yield (body, True, node.name)


def _resolve_agents(
    wf: Workflow, resolver: AgentResolver, context: ResolveContext
) -> None:
    """物化 agent 引用 → ``node.prompt`` + ``node.resources_root``；合并 frontmatter meta。

    与物化时序强相关的预检（**必须在物化前**，因物化填 ``node.prompt``）：
      - 互斥违反（prompt + agent 同时非空）→ error（SPEC §7.1）
      - foreach body 双 None（body 无 name 不能 fallback）→ error（SPEC §7.1 / C9）
      - 旧约定（顶层 prompt + agent 皆 None）→ ``DeprecationWarning``，内部当 ``agent=name``

    ``AgentNotFound`` 聚合（一次列全所有缺失名 + 搜过路径）。物化后**保留** ``node.agent``
    字段（互斥已在物化前预检，``validate_workflow`` 不会再因 prompt+agent 都非空误报）。
    """
    errors: list[str] = []
    missing: list[str] = []
    for node, is_body, parent in _iter_agent_nodes(wf):
        # ── 物化前预检（时序敏感，必须在填 prompt 前）──────────────────────────
        if node.prompt is not None and node.agent is not None:
            loc = f"foreach '{parent}'.body" if is_body else f"node '{node.name}'"
            errors.append(f"{loc}：prompt 与 agent 互斥（不可同时声明）")
            continue
        if node.prompt is None and node.agent is None:
            if is_body:
                # C9：foreach body 无 name，不能走旧约定 name-fallback
                errors.append(
                    f"foreach '{parent}'.body 的 agent node 必须显式 `agent: <name>` "
                    "或内联 `prompt:`（body 无 name，不能走旧约定 name-fallback）"
                )
                continue
            # 顶层旧约定 → DeprecationWarning + 当 agent=name（SPEC §7.2）
            warnings.warn(
                f"agent '{node.name}' 使用旧约定（prompt 省略 + name 匹配 agents/<name>.md）。"
                f"请改为显式引用：在 node 上设 `agent: {node.name}`。旧约定将在未来版本移除。",
                DeprecationWarning,
                stacklevel=2,
            )
            name: str = node.name
        elif node.agent is not None:
            name = node.agent
        else:
            continue  # prompt 非空（内联），无需物化

        # ── 物化（resolver 解析 → 填 prompt + resources_root + 合并 meta）─────────
        try:
            handle: AgentHandle = resolver.resolve(name, context=context)
        except AgentNotFound as e:
            missing.append(str(e))
            continue

        node.prompt = handle.prompt
        node.resources_root = str(handle.resources_root.resolve())
        _merge_meta(node, handle)

    if errors or missing:
        raise ConfigurationError(errors + missing, [])


def _merge_meta(node: AgentNode, handle: AgentHandle) -> None:
    """合并 agent frontmatter meta → node（node 内联字段优先，SPEC §0.1 #7）。

    - ``model``：``node.model is None`` → ``meta.model``
    - ``tools``（C3 None 消歧）：``None``=未声明（用 ``meta.tools`` 或全开默认）；
      显式 ``[]``=禁工具（保留）；非空 list=白名单（保留）。合并：``node.tools is None
      且 meta.tools 非空`` → ``meta.tools``，否则保留 node.tools
    - ``executor``：``node.executor == "claude"``（schema 默认值）且 ``meta.executor`` 非空 →
      ``meta.executor``。注意：无法区分"用户显式写 claude"与"默认值"，故 executor 合并较弱
      （文档建议 agent 级 executor 在 frontmatter 声明 + node 不写 executor）
    """
    meta = handle.meta
    if node.model is None and meta.model is not None:
        node.model = meta.model
    if node.tools is None and meta.tools is not None:
        node.tools = meta.tools
    if node.executor == "claude" and meta.executor is not None:
        node.executor = meta.executor
