"""tests/compile/test_agents.py —— phase-14 agent 池解析层单测。

覆盖 SPEC §8.1 矩阵：
  - LocalPoolResolver：文件夹优先 / 单文件兜底 / 查找顺序 / not found 聚合 / discover
  - frontmatter：无头 / 有头 / 未闭合 / 未知字段 / 坏 YAML / 类型错
  - _resolve_agents：物化 prompt+resources_root / 合并优先级(node>meta) / 互斥 / foreach body 双 None
  - deprecation warn（旧约定 name-fallback）
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from orca.compile.agents import (
    AgentNotFound,
    AgentMeta,
    LocalPoolResolver,
    ResolveContext,
)
from orca.compile.parser import _resolve_agents, load_workflow
from orca.compile.validator import ConfigurationError
from orca.schema import AgentNode, Workflow


def _ctx(workflow_dir: Path, cwd: Path | None = None, extra: tuple[Path, ...] = ()) -> ResolveContext:
    return ResolveContext(workflow_dir=workflow_dir, cwd=cwd or workflow_dir, extra_roots=extra)


# ── LocalPoolResolver：形态优先级 + 查找顺序 ───────────────────────────────────


def test_resolve_folder_preferred_over_single(tmp_path: Path):
    """同目录下 <name>/agent.md（文件夹）优先于 <name>.md（单文件）。"""
    base = tmp_path / "agents"
    (base / "dup").mkdir(parents=True)
    (base / "dup" / "agent.md").write_text("folder body", encoding="utf-8")
    (base / "dup.md").write_text("single body", encoding="utf-8")
    h = LocalPoolResolver().resolve("dup", context=_ctx(tmp_path))
    assert h.prompt == "folder body"
    assert h.is_folder is True


def test_resolve_single_file_fallback(tmp_path: Path):
    """无文件夹时回落到单文件 <name>.md。"""
    base = tmp_path / "agents"
    base.mkdir()
    (base / "solo.md").write_text("single body", encoding="utf-8")
    h = LocalPoolResolver().resolve("solo", context=_ctx(tmp_path))
    assert h.prompt == "single body"
    assert h.is_folder is False


def test_resolve_search_order_workflow_dir_before_cwd(tmp_path: Path):
    """workflow_dir/agents 优先于 cwd/agents（project-local 最高）。"""
    wf_dir = tmp_path / "wf"
    cwd_dir = tmp_path / "cwd"
    (wf_dir / "agents").mkdir(parents=True)
    (wf_dir / "agents" / "x.md").write_text("from-workflow-dir", encoding="utf-8")
    (cwd_dir / "agents").mkdir(parents=True)
    (cwd_dir / "agents" / "x.md").write_text("from-cwd", encoding="utf-8")
    h = LocalPoolResolver().resolve("x", context=_ctx(wf_dir, cwd=cwd_dir))
    assert h.prompt == "from-workflow-dir"


def test_resolve_extra_roots(tmp_path: Path):
    """workflow_dir/cwd 都没有 → 查 extra_roots（phase-15 多 pool 前瞻）。"""
    pool = tmp_path / "pool"
    (pool / "agents").mkdir(parents=True)
    (pool / "agents" / "p.md").write_text("from-pool", encoding="utf-8")
    h = LocalPoolResolver().resolve(
        "p", context=_ctx(tmp_path, extra=(pool / "agents",))
    )
    assert h.prompt == "from-pool"


def test_resolve_not_found_lists_searched(tmp_path: Path):
    """AgentNotFound 携带搜过的所有路径（_resolve_agents 聚合用）。"""
    with pytest.raises(AgentNotFound) as ei:
        LocalPoolResolver().resolve("ghost", context=_ctx(tmp_path))
    assert "ghost" in str(ei.value)
    assert ei.value.searched  # 非空


# ── frontmatter 解析（fail loud 各路径）────────────────────────────────────────


def test_frontmatter_no_frontmatter(tmp_path: Path):
    """无 frontmatter（首行非 ---）→ 整文件 body，meta 全默认。"""
    base = tmp_path / "agents"
    base.mkdir()
    (base / "plain.md").write_text("# plain\n\n纯 prompt 无元数据。", encoding="utf-8")
    h = LocalPoolResolver().resolve("plain", context=_ctx(tmp_path))
    assert h.prompt.startswith("# plain")
    assert h.meta == AgentMeta()


def test_frontmatter_with_meta(tmp_path: Path):
    """有 frontmatter → 解析 meta + body 分离。"""
    base = tmp_path / "agents"
    base.mkdir()
    (base / "rich.md").write_text(
        "---\ndescription: 优化器\nmodel: deepseek-v4-flash\ntools: [Bash, Read]\n---\n# body",
        encoding="utf-8",
    )
    h = LocalPoolResolver().resolve("rich", context=_ctx(tmp_path))
    assert h.meta.description == "优化器"
    assert h.meta.model == "deepseek-v4-flash"
    assert h.meta.tools == ["Bash", "Read"]
    assert h.prompt == "# body"


def test_frontmatter_unclosed_raises(tmp_path: Path):
    """首行 --- 但无闭合 --- → ConfigurationError（fail loud）。"""
    base = tmp_path / "agents"
    base.mkdir()
    (base / "bad.md").write_text("---\nmodel: x\n无闭合", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="无闭合"):
        LocalPoolResolver().resolve("bad", context=_ctx(tmp_path))


def test_frontmatter_unknown_field_raises(tmp_path: Path):
    """frontmatter 含未知字段 → ConfigurationError（防拼写错误静默忽略）。"""
    base = tmp_path / "agents"
    base.mkdir()
    (base / "u.md").write_text("---\nunknown_field: x\n---\nbody", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="字段错误"):
        LocalPoolResolver().resolve("u", context=_ctx(tmp_path))


def test_frontmatter_bad_yaml_raises(tmp_path: Path):
    """frontmatter YAML 语法错 → ConfigurationError。"""
    base = tmp_path / "agents"
    base.mkdir()
    (base / "yaml.md").write_text("---\nmodel: [unclosed\n---\nbody", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="YAML 解析失败"):
        LocalPoolResolver().resolve("yaml", context=_ctx(tmp_path))


def test_frontmatter_type_error_raises(tmp_path: Path):
    """frontmatter 字段类型错（model 非 str）→ ConfigurationError（C6 类型校验）。"""
    base = tmp_path / "agents"
    base.mkdir()
    (base / "t.md").write_text("---\nmodel: 123\n---\nbody", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="类型错误"):
        LocalPoolResolver().resolve("t", context=_ctx(tmp_path))


def test_frontmatter_body_horizontal_line_not_misread(tmp_path: Path):
    """body 内的 ---（markdown 水平线）不被误判为 frontmatter 边界（C6 精确算法）。"""
    base = tmp_path / "agents"
    base.mkdir()
    (base / "hr.md").write_text("正文\n\n---\n\n水平线后正文", encoding="utf-8")
    h = LocalPoolResolver().resolve("hr", context=_ctx(tmp_path))
    # 首行非 --- → 整文件 body（含中间的 --- 水平线）
    assert "水平线后正文" in h.prompt
    assert h.meta == AgentMeta()


# ── discover（MCP list_agents / CLI list --agents 用）──────────────────────────


def test_discover_lists_folder_and_single(tmp_path: Path):
    """discover 列出文件夹 + 单文件 agent，去重（先见优先）。"""
    base = tmp_path / "agents"
    (base / "folder").mkdir(parents=True)
    (base / "folder" / "agent.md").write_text("f", encoding="utf-8")
    (base / "single.md").write_text("s", encoding="utf-8")
    items = dict(LocalPoolResolver().discover(context=_ctx(tmp_path)))
    assert items == {"folder": True, "single": False}


# ── _resolve_agents：物化 + 合并 + 互斥 + foreach body ──────────────────────────


def _wf_with_agent(agent_yaml: str, tmp_path: Path) -> Workflow:
    """构造最小 workflow（含一个 agent node），yaml 写到 tmp_path/main.yaml。"""
    (tmp_path / "main.yaml").write_text(agent_yaml, encoding="utf-8")
    return load_workflow(tmp_path / "main.yaml")


def test_resolve_agents_materializes_prompt_and_resources(tmp_path: Path):
    """agent 引用 → compile 物化 node.prompt + node.resources_root。"""
    base = tmp_path / "agents"
    base.mkdir()
    (base / "worker").mkdir()
    (base / "worker" / "agent.md").write_text(
        "---\ndescription: d\n---\n你是 worker。", encoding="utf-8"
    )
    wf = _wf_with_agent(
        "name: t\nentry: w\nnodes:\n  - name: w\n    kind: agent\n    agent: worker\n    routes:\n      - to: $end\n",
        tmp_path,
    )
    node = wf.nodes[0]
    assert node.prompt == "你是 worker。"
    assert node.resources_root is not None
    assert node.resources_root.endswith("worker")  # 文件夹 agent 根


def test_merge_meta_node_inline_wins(tmp_path: Path):
    """node 内联字段 > agent frontmatter 默认（SPEC §0.1 #7）。"""
    base = tmp_path / "agents"
    base.mkdir()
    (base / "m.md").write_text(
        "---\nmodel: deepseek-v4-flash\ntools: [Bash]\n---\nbody", encoding="utf-8"
    )
    wf = _wf_with_agent(
        "name: t\nentry: w\nnodes:\n  - name: w\n    kind: agent\n    agent: m\n"
        "    model: claude-sonnet-4-6\n    tools: [Read]\n    routes:\n      - to: $end\n",
        tmp_path,
    )
    node = wf.nodes[0]
    assert node.model == "claude-sonnet-4-6"  # node 内联压 meta
    assert node.tools == ["Read"]  # node 内联压 meta


def test_merge_meta_tools_none_uses_meta(tmp_path: Path):
    """C3：node.tools is None（未声明）→ 用 meta.tools；显式 [] = 禁工具（保留）。"""
    base = tmp_path / "agents"
    base.mkdir()
    (base / "mt.md").write_text("---\ntools: [Bash, Read]\n---\nbody", encoding="utf-8")
    wf = _wf_with_agent(
        "name: t\nentry: w\nnodes:\n  - name: w\n    kind: agent\n    agent: mt\n    routes:\n      - to: $end\n",
        tmp_path,
    )
    assert wf.nodes[0].tools == ["Bash", "Read"]  # node.tools=None → 用 meta


def test_exclusive_prompt_agent_raises(tmp_path: Path):
    """prompt + agent 同时声明 → ConfigurationError（互斥违反，物化前预检）。"""
    base = tmp_path / "agents"
    base.mkdir()
    (base / "e.md").write_text("body", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="互斥"):
        _wf_with_agent(
            "name: t\nentry: w\nnodes:\n  - name: w\n    kind: agent\n    agent: e\n"
            "    prompt: inline\n    routes:\n      - to: $end\n",
            tmp_path,
        )


def test_foreach_body_double_none_raises(tmp_path: Path):
    """C9：foreach body 的 agent node 双 None（无 name 不能 fallback）→ error。"""
    with pytest.raises(ConfigurationError, match="foreach"):
        _wf_with_agent(
            "name: t\nentry: loop\nnodes:\n  - name: loop\n    kind: foreach\n"
            "    source: inputs.items\n    body:\n      kind: agent\n"
            "      routes:\n        - to: $end\n",
            tmp_path,
        )


def test_deprecation_warn_old_convention(tmp_path: Path, recwarn):
    """旧约定（prompt=None + name 匹配 md）→ DeprecationWarning（C1 通道）。"""
    base = tmp_path / "agents"
    base.mkdir()
    (base / "legacy.md").write_text("legacy prompt", encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        _wf_with_agent(
            "name: t\nentry: legacy\nnodes:\n  - name: legacy\n    kind: agent\n"
            "    routes:\n      - to: $end\n",
            tmp_path,
        )
    deps = [w for w in recwarn.list if issubclass(w.category, DeprecationWarning)]
    assert any("legacy" in str(w.message) for w in deps)


def test_resolve_agents_aggregates_missing(tmp_path: Path):
    """多个 agent 引用缺失 → 一次 ConfigurationError 列全（聚合，SPEC 铁律 8）。"""
    with pytest.raises(ConfigurationError) as ei:
        _wf_with_agent(
            "name: t\nentry: a\nnodes:\n  - name: a\n    kind: agent\n    agent: ghost1\n"
            "    routes:\n      - to: b\n  - name: b\n    kind: agent\n    agent: ghost2\n"
            "    routes:\n      - to: $end\n",
            tmp_path,
        )
    msg = str(ei.value)
    assert "ghost1" in msg and "ghost2" in msg  # 两个都列出


def test_resolve_agents_resources_root_absolute(tmp_path: Path):
    """resources_root 物化为绝对路径（executor 注入 env 用，不依赖 cwd）。"""
    base = tmp_path / "agents"
    (base / "f").mkdir(parents=True)
    (base / "f" / "agent.md").write_text("body", encoding="utf-8")
    wf = _wf_with_agent(
        "name: t\nentry: w\nnodes:\n  - name: w\n    kind: agent\n    agent: f\n    routes:\n      - to: $end\n",
        tmp_path,
    )
    assert Path(wf.nodes[0].resources_root).is_absolute()
