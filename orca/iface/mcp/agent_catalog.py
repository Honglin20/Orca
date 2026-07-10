"""agent_catalog.py —— setup agent 元数据 + prompt 借用（SPEC phase-10 §5.7 / §2.2）。

回答「MCP 模式下主 session 怎么替 setup agent 跑？」：调 ``get_agent_prompt`` 拿
setup agent 的原始 prompt 文本，主 session 把 prompt 作为参考注入自己上下文，
用自己的工具跟用户对话，收集到的信息作为 ``setup_outputs`` 传给 ``start_workflow``。

设计约束（§5.7）：
  - ``get_agent_prompt`` **不渲染、不执行**——返回原始 prompt 文本（backend-agnostic）。
  - 复用 phase-14 ``AgentResolver`` 解析（workflow_dir 优先 + cwd/agents 兜底）。
  - 单独成模块（DRY）：``server.py`` 的 ``get_agent_prompt`` 工具 + 未来 Web 端共用。

依赖单向：本模块依赖 ``orca.compile.agents``（AgentResolver）+ ``orca.schema.workflow``
（AgentNode）。不依赖 run/exec/events。纯函数。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from orca.compile.agents import (
    AgentHandle,
    AgentNotFound,
    LocalPoolResolver,
    ResolveContext,
)
from orca.compile.validator import ConfigurationError
from orca.schema.workflow import AgentNode

logger = logging.getLogger(__name__)


def _make_resolver_context(yaml_path: str | None = None) -> ResolveContext:
    """构造 resolver 上下文（同 ``server._agent_resolve_context`` 的独立版，DRY）。

    ``yaml_path`` 给定 → workflow_dir = 其父目录；否则 cwd。两者叠加 cwd/agents。
    """
    cwd = Path.cwd()
    workflow_dir = Path(yaml_path).resolve().parent if yaml_path else cwd
    return ResolveContext(workflow_dir=workflow_dir, cwd=cwd)


def get_setup_agent_prompt(
    agent_node: AgentNode, *, yaml_path: str | None = None
) -> dict[str, Any] | None:
    """从 ``AgentNode`` 提取 prompt 文本（setup phase 用，SPEC §2.2 / §5.7）。

    三态（phase-14 AgentNode）：
      - ``prompt`` 非空（内联或 compile 物化）→ 直接返。
      - ``agent`` 引用名非空 → resolver 解析 → 返 agent.md body。
      - 两者皆 None → 返 ``None``（旧约定 fallback，由 compile.warn 处理；MCP 层不救济）。

    返回 ``{name, prompt, description}``。agent 不存在 / 解析失败 → 返 None（fail soft，
    MCP 层据 None 返 ``Result.err``）。

    **关键**：prompt 必须 backend-agnostic（不写 "use Read tool"，写 "read the user's file"）。
    compile 期已强制；本函数只读取不重写。
    """
    # 优先用物化的 prompt（compile 期 ``_resolve_agents`` 已填）
    if agent_node.prompt:
        return {
            "name": agent_node.name,
            "prompt": agent_node.prompt,
            "description": _extract_short_desc(agent_node.prompt),
        }

    # agent 引用 → resolver 解析
    if agent_node.agent:
        ctx = _make_resolver_context(yaml_path)
        resolver = LocalPoolResolver()
        try:
            handle: AgentHandle = resolver.resolve(
                agent_node.agent, context=ctx
            )
        except (AgentNotFound, ConfigurationError, OSError):
            logger.warning(
                "get_setup_agent_prompt: 解析 agent %r 失败",
                agent_node.agent,
                exc_info=True,
            )
            return None
        return {
            "name": agent_node.name,
            "prompt": handle.prompt,
            "description": handle.meta.description,
        }

    # 双 None（旧约定 / 未物化）→ 不救济
    return None


def _extract_short_desc(prompt: str) -> str:
    """从 prompt 文本提取短描述（首段非空行，截 200 字）。

    给 ``describe_workflow`` 列表展示用，不渲染、不解析 frontmatter。
    """
    for line in prompt.strip().splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return ""
