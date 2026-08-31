"""workflows.py —— workflow / agent 资源只读浏览路由（plan idempotent-churning-lampson）。

回答「前端怎么浏览 workflow 定义 + agent 目录文件？」：
  - ``GET /api/workflows`` → workflow catalog 列表
  - ``GET /api/workflows/{name}`` → workflow 元信息 + 引用到的 agent 名单
  - ``GET /api/workflows/{name}/agents`` → workflow 作用域内可发现的全部 agent（fail-soft）
  - ``GET /api/workflows/{name}/agents/{agent}/tree`` → agent 资源目录递归文件树
  - ``GET /api/workflows/{name}/agents/{agent}/file?path=<rel>`` → 单个文件文本内容
  - ``GET /api/workflows/{name}/tree`` → workflow 目录（yaml parent）全资产递归树（批 G）
  - ``GET /api/workflows/{name}/file?path=<rel>`` → workflow 目录下单文件文本内容（批 G）

**纯只读**：本路由不做任何写入 / 执行 / 编排。仅复用 ``orca.compile`` 现成 loader +
``orca.schema`` 静态结构。无 manager 依赖（仿 ``routes/projects.py:17``）。

**依赖单向（铁律）**：本模块只 import ``orca.compile`` + ``orca.schema`` + fastapi 标准件。
compile/schema 严禁反向 import iface.web。

**fail loud / fail-soft 分工**：
  - ``/{name}`` detail：``ConfigurationError`` → 500（catalog 损坏非用户错，与 list 不一致
    须显式暴露，不能 fail-soft 假装找不到）。
  - ``/agents``：单个 agent resolve 失败 → fail-soft 进 ``missing: true``（list 完整不崩）。
  - ``/{name}`` detail 的 ``subagents`` 键：逐文件 fail-soft（读失败 → 空描述，列表完整不崩）。
  - ``/tree`` / ``/file``：路径越界 / 大文件 / 二进制 → 4xx 显式 detail（不静默吞）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException, Query

from orca.compile import ConfigurationError, catalog
from orca.compile.agents import (
    AgentNotFound,
    AgentResolver,
    LocalPoolResolver,
    ResolveContext,
)
from orca.compile.layout import resolve_subagents_dir
from orca.compile.parser import _iter_agent_nodes
from orca.iface.web.file_text import read_text_file

logger = logging.getLogger(__name__)


def build_router() -> APIRouter:
    """构造 ``/api/workflows`` 路由（无 manager 依赖——纯只读 catalog + 文件系统）。"""
    router = APIRouter(prefix="/api/workflows", tags=["workflows"])

    # ── Endpoint 1：workflow 列表 ──────────────────────────────────────────────
    @router.get("")
    async def list_workflows() -> list[dict]:
        """``catalog.list_workflows()`` 原样返回。

        fail-soft：坏 yaml 在 catalog 层已 log warning + 跳过（不在此处再处理）。
        """
        return catalog.list_workflows()

    # ── Endpoint 2：workflow detail + 引用 agent 名单 ─────────────────────────
    @router.get("/{name}")
    async def get_workflow(name: str) -> dict:
        """workflow 元信息 + ``agents_referenced`` 列表（plan §M3）。

        **docstring 修订（review 闭环 §三）**：``catalog.find_workflow`` 内部对每个 yaml 的
        ``load_workflow`` 已用广 ``except (ConfigurationError, Exception)`` fail-soft 跳过
        坏 yaml（catalog.py:113-122）。故本 endpoint 实际不会 500 fail loud——坏 yaml 在
        list 与 detail 端**一致**地从 catalog 静默消失（双双 fail-soft 到 404）。
        这避免了「list 显示了 detail 却 404」的不一致；plan §Endpoint 2 原本设想的 500 暴露
        路径在 catalog 已 fail-soft 的情况下不可达，按 Rule 7 选「描述现状」路线（YAGNI）。
        """
        found = catalog.find_workflow(name)
        if found is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        wf, yaml_path = found

        referenced: list[str] = []
        for node, _is_body, _parent in _iter_agent_nodes(wf):
            # 仅取 compile 期物化了 resources_root 的真实 agent 引用，跳过内联 prompt 节点。
            if getattr(node, "resources_root", None):
                referenced.append(node.agent or node.name)
        # 去重保序（dict.fromkeys 是 stable dedup idiom）
        referenced = list(dict.fromkeys(referenced))

        detail = catalog.describe_workflow(wf)
        return {
            "name": wf.name,
            "description": wf.description,
            "entry": wf.entry,
            "inputs_schema": detail["inputs_schema"],
            "agents_referenced": referenced,
            # 批 G：subagents 进 detail response（SPEC 钉死，不设独立列表端点）。
            "subagents": _list_subagents(Path(yaml_path).parent, wf.name),
        }

    # ── Endpoint 3：workflow 作用域内可发现的全部 agent（fail-soft）──────────────
    @router.get("/{name}/agents")
    async def list_workflow_agents(name: str) -> list[dict]:
        """列出 workflow 作用域下全部 agent（plan §M4/M5 fail-soft）。

        每个 agent 单独 try/except ``(AgentNotFound, ConfigurationError)``：
          - resolve 成功 → ``{name, is_folder, description, missing: false}``
          - resolve 失败 → ``{name, is_folder, description: "", missing: true}``（fail-soft）

        TOCTOU（discover 命中但 resolve 时被删） / 坏 frontmatter 都落到 fail-soft 分支，
        保证列表完整不崩（仿 ``projects.py:27`` stale fail-soft 模式）。
        """
        ctx = _resolve_context_for(name)
        if ctx is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        resolver: AgentResolver = LocalPoolResolver()

        result: list[dict] = []
        for name_, is_folder in resolver.discover(context=ctx):
            try:
                handle = resolver.resolve(name_, context=ctx)
                result.append(
                    {
                        "name": name_,
                        "is_folder": is_folder,
                        "description": handle.meta.description or "",
                        "missing": False,
                    }
                )
            except (AgentNotFound, ConfigurationError) as e:
                logger.warning(
                    "workflows/agents: agent %s resolve failed (fail-soft): %s",
                    name_,
                    e,
                )
                result.append(
                    {
                        "name": name_,
                        "is_folder": is_folder,
                        "description": "",
                        "missing": True,
                    }
                )
        return result

    # ── Endpoint 4：agent 资源目录递归文件树 ──────────────────────────────────
    @router.get("/{name}/agents/{agent}/tree")
    async def get_agent_tree(name: str, agent: str) -> dict:
        """递归遍历 agent resources_root，返回文件树（plan §M1 schema + m4 过滤）。

        过滤规则：name 以 ``.`` 开头（hidden）/ 等于 ``__pycache__`` / 以 ``.pyc`` 结尾 → 跳过。
        排序：目录先于文件；同类按 name 字典序（稳定可读）。
        """
        resources_root = _resolve_agent_root(name, agent)
        return {
            "agent": agent,
            "root": str(resources_root),
            "nodes": _build_tree(resources_root, rel=""),
        }

    # ── Endpoint 5：单个文件文本内容 ──────────────────────────────────────────
    @router.get("/{name}/agents/{agent}/file")
    async def get_agent_file(
        name: str,
        agent: str,
        path: str = Query(..., description="相对 agent resources_root 的 POSIX 路径"),
    ) -> dict:
        """读文件文本（plan §M2 envelope + M6 size cap + 二进制检测）。

        守卫顺序：路径越界/symlink/非文件 → 404；超 1MB → 422；二进制 → 422。
        守卫 + 读取段共享 ``file_text.read_text_file``（批 G：与 workflow file 端点
        同构 envelope，DRY；W1-T3 进一步抽到 ``iface.web.file_text`` 供 runs 路由复用）。
        """
        resources_root = _resolve_agent_root(name, agent)
        return read_text_file(resources_root, path)

    # ── Endpoint 6：workflow 目录全资产递归树（批 G）──────────────────────────
    @router.get("/{name}/tree")
    async def get_workflow_tree(name: str) -> dict:
        """递归遍历 workflow 目录（yaml parent），返回全资产树（批 G）。

        root = ``_resolve_context_for(name).workflow_dir``（yaml parent）：per-wf 形态
        即 ``<wf-dir>``；旧平铺形态下 root 是 workflows 根本身（SPEC 公式字面，过渡期
        可接受——测试钉死该语义防歧义）。TreeNode 与 agent tree 完全同构（复用
        ``_build_tree``，过滤/排序规则一致）。
        """
        ctx = _resolve_context_for(name)
        if ctx is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        root = ctx.workflow_dir
        return {
            "workflow": name,
            "root": str(root),
            "nodes": _build_tree(root, rel=""),
        }

    # ── Endpoint 7：workflow 目录下单文件文本内容（批 G）──────────────────────
    @router.get("/{name}/file")
    async def get_workflow_file(
        name: str,
        path: str = Query(..., description="相对 workflow root 的 POSIX 路径"),
    ) -> dict:
        """读 workflow 目录下单文件——workflow.yaml / scripts / 共享脚本资产（批 G）。

        不复用 agent file 端点：其 root 是 agent resources_root，workflow.yaml /
        ``agents/_xxx_scripts`` 多数不在任何 agent root 下。守卫 + 读取与 agent file
        端点共享 ``file_text.read_text_file``（envelope 同构）。
        """
        ctx = _resolve_context_for(name)
        if ctx is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        return read_text_file(ctx.workflow_dir, path)

    return router


# ── 内部 helper（模块级，便于单测复用）────────────────────────────────────────


def _resolve_context_for(name: str) -> ResolveContext | None:
    """按 workflow name 构造 ResolveContext（plan §ResolveContext 构造）。

    ``workflow_dir`` = yaml 所在目录（per-wf 形态即 ``<wf-dir>``；旧平铺形态为
    ``workflows/`` 根），``_search_bases`` 第一项 ``<wf-dir>/agents``（旧平铺
    ``workflows/agents/``）命中。``cwd`` 用 ``Path.cwd()``——与 ``parser.load_workflow``
    默认行为一致，不在 route 层 monkeypatch cwd（blast radius）。

    找不到 workflow → None（caller 决定 404）。
    ``ConfigurationError`` 透传（caller 决定 500 fail loud）。
    """
    found = catalog.find_workflow(name)
    if found is None:
        return None
    _wf, yaml_path = found
    return ResolveContext(
        workflow_dir=Path(yaml_path).parent,
        cwd=Path.cwd(),
    )


def _resolve_agent_root(name: str, agent: str) -> Path:
    """解析 agent resources_root；失败 raise HTTPException（404 fail loud）。

    workflow 不存在 → 404 "workflow not found"。
    agent 不存在 / 坏 frontmatter → 404 "agent not found"（保持简洁；TOCTOU 在 list
    endpoint 才 fail-soft，单查时缺失即 404）。
    """
    ctx = _resolve_context_for(name)
    if ctx is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    try:
        handle = LocalPoolResolver().resolve(agent, context=ctx)
    except (AgentNotFound, ConfigurationError) as e:
        logger.warning("workflows/tree: agent %s resolve failed: %s", agent, e)
        raise HTTPException(status_code=404, detail="agent not found") from e
    return handle.resources_root


def _list_subagents(wf_dir: Path, wf_name: str) -> list[dict]:
    """列出 workflow 的 subagents（批 G：detail response 的 ``subagents`` 键）。

    双形态目录解析复用 ``orca.compile.layout.resolve_subagents_dir``（单一真相源，
    validator / orchestrator 同源——web 不得自抄公式）。逐文件 fail-soft：读失败 /
    编码错 → ``{name: stem, description: ""}`` + warning（detail 整体不崩，仿
    agents 列表模式）。目录缺失（双形态均未命中）→ ``[]``（无 subagents 的 wf 正常）。
    排序：``sorted(glob("*.md"))`` 文件名字典序（稳定，golden 可测）。
    """
    sub_dir = resolve_subagents_dir(wf_dir, wf_name)
    if sub_dir is None:
        return []
    result: list[dict] = []
    for md in sorted(sub_dir.glob("*.md")):
        if not md.is_file():
            continue
        try:
            description = _subagent_description(md.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(
                "workflows/detail: subagent %s read failed (fail-soft): %s",
                md.stem,
                e,
            )
            description = ""
        result.append({"name": md.stem, "description": description})
    return result


def _subagent_description(text: str) -> str:
    """抽 subagent md frontmatter 的 ``description`` 键（宽松解析，批 G）。

    不复用 ``compile.agents._parse_meta_yaml``（``AgentMeta`` 未知字段 TypeError）
    也不复用 ``validator._parse_subagent_frontmatter``（strict 三键协议、无
    description）——真实 subagent md 只有 subagent/version/sentinel 三键，直接复用
    会把全部真实文件打进 fail-soft。frontmatter 未闭合 / YAMLError / 无键 / 非
    str → ``""``（**不取 body 首行**——正文语义劫持，plan §3 批 G 钉死兜底）。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for idx in range(1, len(lines)):
        if lines[idx].strip() != "---":
            continue
        try:
            data = yaml.safe_load("\n".join(lines[1:idx]))
        except yaml.YAMLError:
            return ""
        if isinstance(data, dict) and isinstance(data.get("description"), str):
            return data["description"]
        return ""
    return ""  # frontmatter 未闭合


def _build_tree(path: Path, *, rel: str) -> list[dict]:
    """递归构建 TreeNode 列表（plan §M1 schema + m4 过滤 + 排序规则）。

    ``rel`` 是当前目录相对 root 的 POSIX 路径（``""`` = root 本身）；子节点 path 拼接前缀。
    """
    if not path.is_dir():
        return []
    children: list[tuple[str, Path, bool]] = []
    for sub in path.iterdir():
        name = sub.name
        # m4 过滤：hidden（. 开头）/ __pycache__ / .pyc 后缀
        if name.startswith("."):
            continue
        if name == "__pycache__":
            continue
        if name.endswith(".pyc"):
            continue
        children.append((name, sub, sub.is_dir()))

    # 排序：目录先于文件；同类按 name 字典序（稳定输出，便于 golden 测试）
    children.sort(key=lambda item: (not item[2], item[0]))

    nodes: list[dict] = []
    for name, sub, is_dir in children:
        child_rel = f"{rel}/{name}" if rel else name
        if is_dir:
            nodes.append(
                {
                    "path": child_rel,
                    "name": name,
                    "is_dir": True,
                    "size": 0,
                    "children": _build_tree(sub, rel=child_rel),
                }
            )
        else:
            try:
                size = sub.stat().st_size
            except OSError:
                size = 0
            nodes.append(
                {
                    "path": child_rel,
                    "name": name,
                    "is_dir": False,
                    "size": size,
                    "children": None,
                }
            )
    return nodes
