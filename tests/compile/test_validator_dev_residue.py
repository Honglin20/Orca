"""tests/compile/test_validator_dev_residue.py —— agent.md body 开发期残留 lint 单测。

覆盖意图（非仅行为）：
- 每类 pattern 命中 → 恰好 1 条 warning（类别去重）；
- 干净 prompt → 0 warning；
- operational 合法串（``orca.chart.render_chart`` / ``$ORCA_AGENT_RESOURCES`` 等）不命中；
- inline prompt（``resources_root is None``）跳过——不扫，因 inline 无 agent.md、不适用本契约；
- foreach body agent 同样扫描。

受众分离契约（``orca/skills/create-workflow/reference/agent-prompt-cleanliness-contract.md``）
的执行靠人；本 lint 是 deterministic 兜底，永远 warning 不 error。
"""

from __future__ import annotations

from orca.compile.validator import ValidationResult, _check_prompt_dev_residue
from orca.schema import AgentNode, ForeachNode, Workflow


def _wf_with_agent_prompt(prompt: str, *, name: str = "a") -> Workflow:
    """构造单 AgentNode workflow，模拟 folder-agent 物化（resources_root 已填）。"""
    node = AgentNode(name=name, kind="agent", prompt=prompt,
                     resources_root="/tmp/fake-agent-resources")
    return Workflow(
        name="w",
        entry=name,
        nodes=[node],
        parallel=[],
        outputs={},
        inputs={},
    )


def _warnings(wf: Workflow) -> list[str]:
    result = ValidationResult()
    _check_prompt_dev_residue(wf, result)
    return list(result.warnings)


# ── 各 pattern 命中 ──


def test_pattern_plan_section_letter():
    wf = _wf_with_agent_prompt("参见 plan §N1 节，照此执行。")
    ws = _warnings(wf)
    assert len(ws) == 1
    assert "plan 编号" in ws[0]
    assert "agent 'a'.prompt" in ws[0]


def test_pattern_plan_section_number():
    wf = _wf_with_agent_prompt("执行前读 plan §9 里的约束。")
    ws = _warnings(wf)
    assert len(ws) == 1
    assert "plan 编号" in ws[0]


def test_pattern_bare_section_number():
    # 没有 plan 前缀，仅 §9.1
    wf = _wf_with_agent_prompt("按 §9.1 流程跑。")
    ws = _warnings(wf)
    assert len(ws) == 1
    assert "spec/plan 节号" in ws[0]


def test_pattern_issue_breadcrumb_chinese_paren():
    wf = _wf_with_agent_prompt("修复（I10）提到的 case。")
    ws = _warnings(wf)
    assert len(ws) == 1
    assert "issue breadcrumb" in ws[0]


def test_pattern_issue_breadcrumb_ascii_paren_no_close():
    # (N1 不闭合也算命中（breadcrumb 常省略闭合括号）
    wf = _wf_with_agent_prompt("见 issue (N1 描述。")
    ws = _warnings(wf)
    assert len(ws) == 1
    assert "issue breadcrumb" in ws[0]


def test_pattern_issue_breadcrumb_b_prefix():
    wf = _wf_with_agent_prompt("对应（B2 阻塞点。")
    ws = _warnings(wf)
    assert len(ws) == 1
    assert "issue breadcrumb" in ws[0]


def test_pattern_orca_source_path_with_line():
    wf = _wf_with_agent_prompt("参考 orca/exec/env.py:91 的逻辑。")
    ws = _warnings(wf)
    assert len(ws) == 1
    assert "Orca 源码路径" in ws[0]
    assert "orca/exec/env.py:91" in ws[0]


def test_pattern_orca_source_path_no_line():
    wf = _wf_with_agent_prompt("看 orca/compile/validator.py 的实现。")
    ws = _warnings(wf)
    assert len(ws) == 1
    assert "Orca 源码路径" in ws[0]


def test_pattern_examples_agents_path():
    wf = _wf_with_agent_prompt("参考 examples/agents/plotter/agent.md。")
    ws = _warnings(wf)
    assert len(ws) == 1
    assert "内部 examples 路径" in ws[0]


# ── 同节点多类别聚合 ──


def test_multiple_categories_in_one_prompt():
    # 三类不同残留同时出现 → 三条 warning（每类各一）
    prompt = (
        "参见 plan §9.1；"
        "修 issue（I10）；"
        "看 orca/exec/env.py:91。"
    )
    wf = _wf_with_agent_prompt(prompt)
    ws = _warnings(wf)
    assert len(ws) == 3
    cats = {w for w in ws}
    assert any("plan 编号" in w for w in cats)
    assert any("issue breadcrumb" in w for w in cats)
    assert any("Orca 源码路径" in w for w in cats)


def test_same_category_dedup_within_node():
    # 同类别多次出现只报一次
    prompt = "见 §9.1 与 §2.3 两节。"
    wf = _wf_with_agent_prompt(prompt)
    ws = _warnings(wf)
    assert len(ws) == 1
    assert "spec/plan 节号" in ws[0]


# ── 干净 prompt ──


def test_clean_prompt_no_warning():
    prompt = (
        "你是一个 NAS 结构搜索 agent。读取 setup.output 里的 project_root，"
        "调用 orca.chart.render_chart 画图。脚本在 $ORCA_AGENT_RESOURCES/scripts/run.py。"
        "Git Bash 下执行；产出 tape 文件 + output_schema。"
        "swin_window / cswin 是 NAS block 库通用示例名。"
    )
    wf = _wf_with_agent_prompt(prompt)
    ws = _warnings(wf)
    assert ws == []


def test_operational_api_path_not_flagged():
    # orca.chart.render_chart 是 API 调用，非源码路径（无 .py 后缀）
    wf = _wf_with_agent_prompt("调用 orca.chart.render_chart(data) 出图。")
    assert _warnings(wf) == []


def test_operational_resource_var_not_flagged():
    # $ORCA_AGENT_RESOURCES 是 spawn 注入 env，合法
    wf = _wf_with_agent_prompt("跑 $ORCA_AGENT_RESOURCES/scripts/foo.py。")
    assert _warnings(wf) == []


def test_orca_subdir_not_in_whitelist_not_flagged():
    # orca/skills/ 不在白名单子目录里 → 不命中
    wf = _wf_with_agent_prompt("看 orca/skills/tars/SKILL.md。")
    assert _warnings(wf) == []


def test_plain_word_orca_not_flagged():
    # 「orca spawn 注入」非源码路径
    wf = _wf_with_agent_prompt("orca spawn 注入 env 给 agent。")
    assert _warnings(wf) == []


# ── inline prompt 跳过 ──


def test_inline_prompt_skipped():
    # resources_root 未物化（None）= inline prompt，本 lint 不适用
    node = AgentNode(name="a", kind="agent",
                     prompt="见 plan §9.1 + 或 orca/exec/env.py:91",
                     resources_root=None)
    wf = Workflow(name="w", entry="a", nodes=[node], parallel=[],
                  outputs={}, inputs={})
    assert _warnings(wf) == []


def test_empty_prompt_skipped():
    node = AgentNode(name="a", kind="agent", prompt="", resources_root="/tmp/x")
    wf = Workflow(name="w", entry="a", nodes=[node], parallel=[],
                  outputs={}, inputs={})
    assert _warnings(wf) == []


def test_none_prompt_skipped():
    node = AgentNode(name="a", kind="agent", prompt=None,
                     resources_root="/tmp/x")
    wf = Workflow(name="w", entry="a", nodes=[node], parallel=[],
                  outputs={}, inputs={})
    assert _warnings(wf) == []


# ── foreach body agent 同样扫描 ──


def test_foreach_body_agent_scanned():
    body = AgentNode(name="", kind="agent",
                     prompt="参见 plan §9.1",
                     resources_root="/tmp/x")
    fe = ForeachNode(
        name="fe",
        kind="foreach",
        source="src.output.items",
        body=body,
        routes=[{"to": "$end"}],
    )
    wf = Workflow(name="w", entry="fe", nodes=[fe], parallel=[],
                  outputs={}, inputs={})
    ws = _warnings(wf)
    assert len(ws) == 1
    assert "foreach 'fe'.body agent" in ws[0]
    assert "plan 编号" in ws[0]


def test_foreach_body_agent_multi_category():
    """foreach body agent 多类别聚合 + 各 warning 都带 foreach location。"""
    body = AgentNode(name="", kind="agent",
                     prompt="参见 plan §9.1；修（I10）；看 orca/exec/env.py:91",
                     resources_root="/tmp/x")
    fe = ForeachNode(
        name="fe",
        kind="foreach",
        source="src.output.items",
        body=body,
        routes=[{"to": "$end"}],
    )
    wf = Workflow(name="w", entry="fe", nodes=[fe], parallel=[],
                  outputs={}, inputs={})
    ws = _warnings(wf)
    assert len(ws) == 3
    assert all("foreach 'fe'.body agent" in w for w in ws)


# ── 全覆盖矩阵 invariant ──


def test_all_patterns_covered_by_matrix():
    """invariant：构造每类各一条命中的 prompt，断言每类 category 都被至少一条 warning 引用。

    防止 _DEV_RESIDUE_PATTERNS 表新增条目时漏写对应测试 + 防止 regex 内捕获组破坏
    ``lastgroup → tuple 索引`` 类别映射（新增 pattern 用了捕获组会导致 cat_idx 错位 →
    某类 category 永远不出现在 warning 里 → 本断言会失败）。
    """
    prompt_parts = []
    for _cat, pat in _ALL_PATTERNS():
        # 取每条 pattern 的首例样本文本（手工列举，与 _DEV_RESIDUE_PATTERNS 同序）。
        prompt_parts.append(_SAMPLE_FOR_PATTERN[pat])
    wf = _wf_with_agent_prompt("；".join(prompt_parts))
    ws = _warnings(wf)
    expected_cats = {cat for cat, _ in _ALL_PATTERNS()}
    reported_cats = set()
    for w in ws:
        for cat in expected_cats:
            if cat in w:
                reported_cats.add(cat)
                break
    assert reported_cats == expected_cats, (
        f"未覆盖类别：{expected_cats - reported_cats}；"
        f"多余类别：{reported_cats - expected_cats}"
    )


def _ALL_PATTERNS():
    """从 validator 导入 pattern 表（保证与实现同步）。"""
    from orca.compile.validator import _DEV_RESIDUE_PATTERNS
    return _DEV_RESIDUE_PATTERNS


# 与 _DEV_RESIDUE_PATTERNS 同序的样本（每条命中恰好对应类别）。
# 若改了 pattern 表，本表也需同步——``test_all_patterns_covered_by_matrix`` 会兜底。
_SAMPLE_FOR_PATTERN = {
    r"plan\s*§\s*[0-9INBivx][0-9A-Za-z.]*": "见 plan §9.1",
    r"§\s*[0-9]+\.[0-9]+": "见 §2.3",
    r"[（(]\s*[INB]\d+": "见（I10",
    r"orca/(?:compile|exec|run|iface|events|chart|profiles|schema|gates)/\S+?\.py(?::\d+)?":
        "见 orca/exec/env.py:91",
    r"examples/agents/[a-z0-9_-]+/agent\.md": "见 examples/agents/plotter/agent.md",
}


# ── 跳过 inline body ──


def test_foreach_body_inline_skipped():
    body = AgentNode(name="", kind="agent",
                     prompt="见 orca/exec/env.py:91",
                     resources_root=None)
    fe = ForeachNode(
        name="fe",
        kind="foreach",
        source="src.output.items",
        body=body,
        routes=[{"to": "$end"}],
    )
    wf = Workflow(name="w", entry="fe", nodes=[fe], parallel=[],
                  outputs={}, inputs={})
    assert _warnings(wf) == []
