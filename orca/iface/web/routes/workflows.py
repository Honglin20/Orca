"""workflows.py —— workflow / agent 资源只读浏览路由（plan idempotent-churning-lampson）。

回答「前端怎么浏览 workflow 定义 + agent 目录文件？」：
  - ``GET /api/workflows`` → workflow catalog 列表
  - ``GET /api/workflows/{name}`` → workflow 元信息 + 引用到的 agent 名单
  - ``GET /api/workflows/{name}/agents`` → workflow 作用域内可发现的全部 agent（fail-soft）
  - ``GET /api/workflows/{name}/agents/{agent}/tree`` → agent 资源目录递归文件树
  - ``GET /api/workflows/{name}/agents/{agent}/file?path=<rel>`` → 单个文件文本内容

**纯只读**：本路由不做任何写入 / 执行 / 编排。仅复用 ``orca.compile`` 现成 loader +
``orca.schema`` 静态结构。无 manager 依赖（仿 ``routes/projects.py:17``）。

**依赖单向（铁律）**：本模块只 import ``orca.compile`` + ``orca.schema`` + fastapi 标准件。
compile/schema 严禁反向 import iface.web。

**fail loud / fail-soft 分工**：
  - ``/{name}`` detail：``ConfigurationError`` → 500（catalog 损坏非用户错，与 list 不一致
    须显式暴露，不能 fail-soft 假装找不到）。
  - ``/agents``：单个 agent resolve 失败 → fail-soft 进 ``missing: true``（list 完整不崩）。
  - ``/tree`` / ``/file``：路径越界 / 大文件 / 二进制 → 4xx 显式 detail（不静默吞）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from orca.compile import ConfigurationError, catalog
from orca.compile.agents import (
    AgentNotFound,
    AgentResolver,
    LocalPoolResolver,
    ResolveContext,
)
from orca.compile.parser import _iter_agent_nodes

logger = logging.getLogger(__name__)

# 单文件大小上限（plan §M2/M6：1MB；防主线程读取 + 前端 prism 高亮卡死）。
_MAX_FILE_BYTES = 1_000_000


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
        wf, _yaml_path = found

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
        """
        resources_root = _resolve_agent_root(name, agent)
        candidate = _safe_resolve(resources_root, path)
        if candidate is None:
            raise HTTPException(status_code=404, detail="file not found")

        size = candidate.stat().st_size
        if size > _MAX_FILE_BYTES:
            raise HTTPException(
                status_code=422,
                detail=f"file too large: {size} bytes (limit {_MAX_FILE_BYTES})",
            )
        with candidate.open("rb") as f:
            if b"\x00" in f.read(2048):
                raise HTTPException(status_code=422, detail="binary file")
        text = candidate.read_text(encoding="utf-8")
        ext = candidate.suffix.lstrip(".")
        return {
            "path": path,
            "text": text,
            "ext": ext,
            "size": size,
            "truncated": False,
        }

    return router


# ── 内部 helper（模块级，便于单测复用）────────────────────────────────────────


def _resolve_context_for(name: str) -> ResolveContext | None:
    """按 workflow name 构造 ResolveContext（plan §ResolveContext 构造）。

    ``workflow_dir`` = yaml 所在目录（即 ``workflows/``），``_search_bases`` 第一项
    ``workflows/agents/`` 命中。``cwd`` 用 ``Path.cwd()``——与 ``parser.load_workflow``
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


def _safe_resolve(root: Path, rel: str) -> Path | None:
    """路径越界 / symlink / 非文件守卫（plan §m3 闭环，抄 ``run_manager.py:277-300``）。

    整段步骤包 try/except ``(ValueError, OSError)``：null byte / 盘符 / 其它 FS 错都 → None。
    返回 None = 拒绝（caller 404）；返回 Path = 合法且是普通文件。
    """
    try:
        rel = (rel or "").strip()
        if not rel:
            return None
        root = root.resolve()
        unresolved = root / rel
        if unresolved.is_symlink():
            return None
        candidate = unresolved.resolve()
        candidate.relative_to(root)  # ValueError → 越界
        if candidate.is_symlink():
            return None
        if not candidate.is_file():
            return None
        return candidate
    except (ValueError, OSError):
        return None


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
