"""tests/compile/test_subagents_md.py —— point-to-file subagent md 校验（SPEC §5.2/§7）。

覆盖：
  - ``_parse_subagent_frontmatter`` strict regex 解析（首块 ``---``）：
    * 合法 frontmatter（三键齐全）→ dict
    * body 后续 ``---`` hr / 表格分隔 → 不误判
    * 缺键 / 整文件无 frontmatter / 非首块 → None
  - ``_check_subagents_md``：目录不存在 → 跳过；缺键 → error；旧协议残留 → warning；
    agent.md 引 ``{{ subagents_root }}`` + tools 缺 Read → error（大小写无关）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orca.compile.validator import (
    ConfigurationError,
    _parse_subagent_frontmatter,
    _check_subagents_md,
    ValidationResult,
)
from orca.schema import AgentNode, Workflow


# ── _parse_subagent_frontmatter ──────────────────────────────────────────────


def test_parse_frontmatter_three_keys_ok():
    """三键齐全的合法 frontmatter → dict（值类型正确）。"""
    text = (
        "---\n"
        "subagent: supernet-evaluator\n"
        "version: 1\n"
        "sentinel: SE7K2A\n"
        "---\n\n"
        "# Supernet Evaluator\n\nbody..."
    )
    fm = _parse_subagent_frontmatter(text)
    assert fm == {"subagent": "supernet-evaluator", "version": 1, "sentinel": "SE7K2A"}


def test_parse_frontmatter_body_hr_not_misparsed():
    """body 后续 ``---``（markdown hr）不被误判（SPEC §5.2 evaluator #13 闭环）。

    strict regex 仅匹配首块 ``^---\\n(.+?\\n)---\\n``。body 内 hr / 表格分隔 / 二级 frontmatter
    均不应进入 yaml_block——consumer 用此函数做 lint，误判会让 hr 被当 yaml 解析。
    """
    text = (
        "---\n"
        "subagent: workflow-verifier\n"
        "version: 2\n"
        "sentinel: WF3QP8\n"
        "---\n\n"
        "# Title\n\n"
        "section A\n\n"
        "---\n\n"  # markdown hr ( thematic break )
        "section B\n\n"
        "| col1 | col2 |\n| --- | --- |\n"  # 表格分隔（--- 行）
    )
    fm = _parse_subagent_frontmatter(text)
    assert fm == {"subagent": "workflow-verifier", "version": 2, "sentinel": "WF3QP8"}


def test_parse_frontmatter_missing_key_returns_none():
    """缺 sentinel 键 → None（调用方按 error 上报）。"""
    text = (
        "---\n"
        "subagent: foo\n"
        "version: 1\n"
        "---\n\nbody"
    )
    assert _parse_subagent_frontmatter(text) is None


def test_parse_frontmatter_missing_block_returns_none():
    """整文件无 frontmatter → None。"""
    assert _parse_subagent_frontmatter("# Title\n\nbody") is None


def test_parse_frontmatter_version_must_be_integer():
    """version 必须是整数（regex ``\\d+``）——非整返 None。"""
    text = (
        "---\n"
        "subagent: foo\n"
        "version: v1\n"  # 非整数
        "sentinel: ABCD12\n"
        "---\n\nbody"
    )
    assert _parse_subagent_frontmatter(text) is None


def test_parse_frontmatter_sentinel_min_length():
    """sentinel 至少 4 位 [A-Za-z0-9]（SPEC §5.2 regex `{4,}`）。"""
    text_ok = (
        "---\nsubagent: foo\nversion: 1\nsentinel: ABCD\n---\n\nbody"
    )
    assert _parse_subagent_frontmatter(text_ok)["sentinel"] == "ABCD"
    text_short = (
        "---\nsubagent: foo\nversion: 1\nsentinel: AB\n---\n\nbody"
    )
    assert _parse_subagent_frontmatter(text_short) is None


# ── _check_subagents_md ──────────────────────────────────────────────────────


def _wf_with_name(name: str = "demo-wf") -> Workflow:
    """最小合法 Workflow（仅含 name 字段；其他字段由 schema 默认 / 不参与本检查）。"""
    return Workflow(name=name, description="d", entry="n1", nodes=[], parallel=[])


def test_check_subagents_md_no_directory_skipped(tmp_path):
    """workflows_root=None 或 subagents/<wf>/ 目录不存在 → 跳过（SPEC §3.3）。"""
    result = ValidationResult()
    _check_subagents_md(_wf_with_name("quant-ptq"), tmp_path, result)
    assert result.errors == []
    assert result.warnings == []

    # None 也跳过
    _check_subagents_md(_wf_with_name(), None, result)
    assert result.errors == []


def test_check_subagents_md_referencing_missing_dir_error(tmp_path):
    """模板引用 ``{{ subagents_root }}`` 但目录不存在 → load 期 error（fail 前移）。

    确定性错误（子 agent body 目录缺失）应在 compile/load 期暴露，而非 run 中途
    render 才炸（历史 bug：in-session ``orca next`` 漏传 yaml_path → 运行期空串）。
    """
    node = AgentNode(
        name="n1",
        kind="agent",
        executor="claude",
        prompt="Read {{ subagents_root }}/helper.md then act.",
    )
    wf = Workflow(name="demo-wf", description="d", entry="n1", nodes=[node], parallel=[])
    result = ValidationResult()
    # workflows_root 显式给但 subagents/demo-wf 目录不存在
    _check_subagents_md(wf, tmp_path, result)
    assert any("subagents_root" in e and "不存在" in e for e in result.errors)


def test_check_subagents_md_valid_frontmatter_no_warning(tmp_path):
    """合法 frontmatter + 无旧协议残留 → 0 errors / 0 warnings。"""
    sub = tmp_path / "subagents" / "demo-wf"
    sub.mkdir(parents=True)
    (sub / "helper.md").write_text(
        "---\nsubagent: helper\nversion: 1\nsentinel: HELP12\n---\n\n# Helper\nbody",
        encoding="utf-8",
    )
    result = ValidationResult()
    _check_subagents_md(_wf_with_name("demo-wf"), tmp_path, result)
    assert result.errors == []
    assert result.warnings == []


def test_check_subagents_md_missing_frontmatter_error(tmp_path):
    """缺 frontmatter → error（SPEC §7 #1 fail loud）。"""
    sub = tmp_path / "subagents" / "demo-wf"
    sub.mkdir(parents=True)
    (sub / "helper.md").write_text("# Helper\nbody without frontmatter", encoding="utf-8")
    result = ValidationResult()
    _check_subagents_md(_wf_with_name("demo-wf"), tmp_path, result)
    assert len(result.errors) == 1
    assert "helper.md" in result.errors[0]
    assert "frontmatter" in result.errors[0]


def test_check_subagents_md_subagent_stem_mismatch_error(tmp_path):
    """frontmatter.subagent ≠ 文件名 stem → error（SPEC §5.2：subagent = stem）。"""
    sub = tmp_path / "subagents" / "demo-wf"
    sub.mkdir(parents=True)
    (sub / "helper.md").write_text(
        "---\nsubagent: wrong-name\nversion: 1\nsentinel: HELP12\n---\n\nbody",
        encoding="utf-8",
    )
    result = ValidationResult()
    _check_subagents_md(_wf_with_name("demo-wf"), tmp_path, result)
    assert any("helper.md" in e and "wrong-name" in e for e in result.errors)


def test_check_subagents_md_legacy_residue_warning(tmp_path):
    """body 含 ``$ORCA_SUBAGENTS_DIR`` / ``cat $HOME/.orca/...subagents/`` → warning。"""
    sub = tmp_path / "subagents" / "demo-wf"
    sub.mkdir(parents=True)
    (sub / "helper.md").write_text(
        "---\nsubagent: helper\nversion: 1\nsentinel: HELP12\n---\n\n"
        "# Helper\n\nRun `cat $HOME/.orca/demo-wf/subagents/x.md` to read.\n",
        encoding="utf-8",
    )
    result = ValidationResult()
    _check_subagents_md(_wf_with_name("demo-wf"), tmp_path, result)
    assert result.errors == []
    assert len(result.warnings) == 1
    assert "旧协议残留" in result.warnings[0]


def test_check_subagents_md_dev_residue_warning(tmp_path):
    """body 含 dev-residue（plan §N / orca/<sub>/<file>.py）→ warning（§8 #5 扩扫子 agent md）。"""
    sub = tmp_path / "subagents" / "demo-wf"
    sub.mkdir(parents=True)
    (sub / "helper.md").write_text(
        "---\nsubagent: helper\nversion: 1\nsentinel: HELP12\n---\n\n"
        "# Helper\n\nSee plan §9.1 for context; cf orca/exec/render.py:50.\n",
        encoding="utf-8",
    )
    result = ValidationResult()
    _check_subagents_md(_wf_with_name("demo-wf"), tmp_path, result)
    # 两个类别命中（plan §9.1 = spec/plan 节号；orca/exec/render.py = Orca 源码路径）
    cats = [w for w in result.warnings if "开发期残留" in w]
    assert len(cats) >= 1


def test_check_subagents_md_agent_node_referencing_subagents_root_no_read_error(
    tmp_path,
):
    """agent.md 引 ``{{ subagents_root }}`` + 显式 tools 缺 read → error（大小写无关）。"""
    sub = tmp_path / "subagents" / "demo-wf"
    sub.mkdir(parents=True)
    (sub / "helper.md").write_text(
        "---\nsubagent: helper\nversion: 1\nsentinel: HELP12\n---\n\nbody", encoding="utf-8"
    )
    node = AgentNode(
        name="n1",
        kind="agent",
        executor="claude",
        prompt="Read {{ subagents_root }}/helper.md",
        tools=["bash", "edit"],  # 显式白名单无 read
    )
    wf = Workflow(name="demo-wf", description="d", entry="n1", nodes=[node], parallel=[])
    result = ValidationResult()
    _check_subagents_md(wf, tmp_path, result)
    errs = [e for e in result.errors if "Read" in e and "n1" in e]
    assert len(errs) == 1


def test_check_subagents_md_lowercase_read_accepted(tmp_path):
    """opencode 工具名小写（``read``）——大小写无关匹配通过（SPEC §5.5 三壳共用契约）。"""
    sub = tmp_path / "subagents" / "demo-wf"
    sub.mkdir(parents=True)
    (sub / "helper.md").write_text(
        "---\nsubagent: helper\nversion: 1\nsentinel: HELP12\n---\n\nbody", encoding="utf-8"
    )
    node = AgentNode(
        name="n1",
        kind="agent",
        executor="claude",
        prompt="Read {{ subagents_root }}/helper.md",
        tools=["bash", "read", "edit"],  # opencode 小写 read
    )
    wf = Workflow(name="demo-wf", description="d", entry="n1", nodes=[node], parallel=[])
    result = ValidationResult()
    _check_subagents_md(wf, tmp_path, result)
    assert all("Read" not in e for e in result.errors)


def test_check_subagents_md_tools_none_skips_read_check(tmp_path):
    """tools=None（默认全开，含 Read）→ 不校验（host 通用类型全工具集）。"""
    sub = tmp_path / "subagents" / "demo-wf"
    sub.mkdir(parents=True)
    (sub / "helper.md").write_text(
        "---\nsubagent: helper\nversion: 1\nsentinel: HELP12\n---\n\nbody", encoding="utf-8"
    )
    node = AgentNode(
        name="n1",
        kind="agent",
        executor="claude",
        prompt="Read {{ subagents_root }}/helper.md",
        tools=None,
    )
    wf = Workflow(name="demo-wf", description="d", entry="n1", nodes=[node], parallel=[])
    result = ValidationResult()
    _check_subagents_md(wf, tmp_path, result)
    assert all("Read" not in e for e in result.errors)


def test_check_subagents_md_foreach_body_agent_referencing_subagents_root_no_read_error(
    tmp_path,
):
    """foreach body agent 引 ``{{ subagents_root }}`` + 显式 tools 缺 read → error。

    foreach body agent 是 AgentNode 子集，同样走 _check_subagent_root_ref_tools
    （SPEC §7 #3：foreach body agent 也参与 Read 工具前置校验）。
    """
    from orca.schema import ForeachNode

    sub = tmp_path / "subagents" / "demo-wf"
    sub.mkdir(parents=True)
    (sub / "helper.md").write_text(
        "---\nsubagent: helper\nversion: 1\nsentinel: HELP12\n---\n\nbody", encoding="utf-8"
    )
    body = AgentNode(
        name="",
        kind="agent",
        executor="claude",
        prompt="Read {{ subagents_root }}/helper.md for {{ item }}",
        tools=["bash", "edit"],  # 无 read
    )
    foreach = ForeachNode(
        name="loop1",
        kind="foreach",
        source="upstream.output.items",
        body=body,
    )
    wf = Workflow(name="demo-wf", description="d", entry="loop1", nodes=[foreach], parallel=[])
    result = ValidationResult()
    _check_subagents_md(wf, tmp_path, result)
    errs = [e for e in result.errors if "Read" in e and "loop1" in e]
    assert len(errs) == 1, f"foreach body 缺 Read 应报 error；实际 errors={result.errors}"
