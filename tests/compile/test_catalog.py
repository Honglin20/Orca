"""test_catalog.py —— workflow catalog 单元测试（SPEC phase-10 §5.6 / §2.2 + in-session v5 §6.2）。

覆盖：
  - ``list_workflows`` 扫目录 + 返 inputs_schema（无 has_setup，setup 全栈删）
  - ``describe_workflow`` 返 inputs_schema（无 setup 元信息）
  - ``find_workflow_by_name`` first-wins 优先级
  - ``find_workflow_yaml_path`` 反查路径
  - 加载失败的 YAML 跳过（log warning，不中断列表）
  - YAML 含 ``setup:`` 段被 pydantic ``extra="forbid"`` 拒绝（fail loud，§6.2 m13）

设计：monkeypatch ``_workflow_dirs`` 指向 tmp_path/workflows（隔离测试）。

进程内缓存测试（web-perf）：钉「首扫后重复调用命中缓存（不重新 load）、
yaml 内容 / 清单 / 相邻 agent md 变化 → 指纹失配重扫拿新值、first-wins 与
fail-soft 语义在缓存路径下不变、find_workflow 返回独立拷贝（改对象不污染缓存）」。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import orca.compile.catalog as catalog_module
from orca.compile import ConfigurationError
from orca.compile.catalog import (
    describe_workflow,
    find_workflow,
    find_workflow_by_name,
    find_workflow_yaml_path,
    list_workflows,
)


SIMPLE_WF = """
name: simple
description: 简单 workflow
entry: a
nodes:
  - name: a
    kind: script
    command: "echo hi"
    routes:
      - to: $end
"""

# YAML 含 setup: 段 → pydantic extra="forbid" 拒绝（in-session v5 §6.2 m13 fail loud）
SETUP_FORBIDDEN_YAML = """
name: setup_demo
description: legacy setup phase workflow
setup:
  - name: collector
    kind: agent
    prompt: "collect"
entry: a
nodes:
  - name: a
    kind: script
    command: "echo hi"
    routes:
      - to: $end
"""

BAD_YAML = """
this is not: valid: yaml: at all
  bad indent
"""


@pytest.fixture(autouse=True)
def _isolate_catalog_cache():
    """每个测试前后清空 catalog 进程内缓存（防跨测试残留 entries/指纹）。"""
    catalog_module._reset_cache()
    yield
    catalog_module._reset_cache()


@pytest.fixture
def catalog_dir(tmp_path, monkeypatch):
    """tmp_path/workflows/ 作为 catalog 目录（隔离测试）。"""
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    monkeypatch.setattr(
        "orca.compile.catalog._workflow_dirs",
        lambda: [wf_dir],
    )
    return wf_dir


# ── list_workflows ───────────────────────────────────────────────────────────


def test_list_workflows_empty_dir(catalog_dir):
    """空 catalog 目录 → 返空列表（不 raise）。"""
    assert list_workflows() == []


def test_list_workflows_returns_metadata(catalog_dir):
    """list_workflows 返 name/description/entry/inputs_count/inputs_schema（无 has_setup）。"""
    (catalog_dir / "simple.yaml").write_text(SIMPLE_WF, encoding="utf-8")

    result = list_workflows()

    assert len(result) == 1
    assert result[0]["name"] == "simple"
    assert result[0]["description"] == "简单 workflow"
    # in-session v5 §6.2：has_setup key 不再返回（setup 全栈删）
    assert "has_setup" not in result[0]
    assert result[0]["entry"] == "a"
    assert result[0]["inputs_count"] == 0
    # v5 §2.3：inputs_schema = [{name,type,description}]（空 inputs → []）
    assert result[0]["inputs_schema"] == []


def test_list_workflows_skips_setup_yaml(catalog_dir):
    """YAML 含 setup: 段 → 加载失败（extra=forbid）→ catalog 跳过（log warning）。"""
    (catalog_dir / "good.yaml").write_text(SIMPLE_WF, encoding="utf-8")
    (catalog_dir / "legacy_setup.yaml").write_text(SETUP_FORBIDDEN_YAML, encoding="utf-8")

    result = list_workflows()

    # 仅合法 simple workflow 进列表；legacy setup workflow 加载失败被跳过
    assert len(result) == 1
    assert result[0]["name"] == "simple"


def test_list_workflows_skips_bad_yaml(catalog_dir):
    """加载失败的 YAML 跳过（log warning，不中断列表）。"""
    (catalog_dir / "good.yaml").write_text(SIMPLE_WF, encoding="utf-8")
    (catalog_dir / "bad.yaml").write_text(BAD_YAML, encoding="utf-8")

    result = list_workflows()

    assert len(result) == 1
    assert result[0]["name"] == "simple"


def test_list_workflows_first_wins(tmp_path, monkeypatch):
    """同名 workflow 在多目录 → first-wins（project-local 优先于 user-global）。

    两个目录都有 ``name: simple`` 的 workflow（不同 description 区分），dir1 先见 → 胜出。
    """
    dir1 = tmp_path / "dir1"
    dir2 = tmp_path / "dir2"
    dir1.mkdir()
    dir2.mkdir()
    (dir1 / "demo.yaml").write_text(SIMPLE_WF, encoding="utf-8")
    # dir2 同 name 不同 description（区分哪个胜出）
    (dir2 / "override.yaml").write_text(
        SIMPLE_WF.replace(
            "description: 简单 workflow",
            "description: from dir2 override",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "orca.compile.catalog._workflow_dirs",
        lambda: [dir1, dir2],
    )

    result = list_workflows()

    assert len(result) == 1
    assert result[0]["name"] == "simple"
    # dir1 先见，胜出（description 是 dir1 的）
    assert result[0]["description"] == "简单 workflow"


# ── describe_workflow ────────────────────────────────────────────────────────


def test_describe_workflow_returns_inputs_schema_no_setup(catalog_dir):
    """describe_workflow 返 name/description/inputs_schema（无 setup/has_setup 字段）。"""
    (catalog_dir / "simple.yaml").write_text(SIMPLE_WF, encoding="utf-8")

    wf = find_workflow_by_name("simple")
    assert wf is not None
    detail = describe_workflow(wf)

    assert detail["name"] == "simple"
    assert detail["description"] == "简单 workflow"
    # in-session v5 §6.2：setup / has_setup key 不再返回
    assert "has_setup" not in detail
    assert "setup" not in detail
    # inputs_schema 是 dict（{key: {type, required, description}}）
    assert "inputs_schema" in detail


# ── find_workflow_by_name / find_workflow_yaml_path ──────────────────────────


def test_find_workflow_by_name_found(catalog_dir):
    """按 name 找到 workflow（返回加载后的 Workflow 对象）。"""
    (catalog_dir / "simple.yaml").write_text(SIMPLE_WF, encoding="utf-8")

    wf = find_workflow_by_name("simple")
    assert wf is not None
    assert wf.name == "simple"


def test_find_workflow_by_name_not_found(catalog_dir):
    """按 name 未找到 → None。"""
    (catalog_dir / "simple.yaml").write_text(SIMPLE_WF, encoding="utf-8")

    assert find_workflow_by_name("nonexistent") is None


def test_find_workflow_yaml_path_found(catalog_dir):
    """按 name 反查 yaml_path（start_workflow 传给 manager 用）。"""
    (catalog_dir / "simple.yaml").write_text(SIMPLE_WF, encoding="utf-8")

    path = find_workflow_yaml_path("simple")
    assert path is not None
    assert path.endswith("simple.yaml")


def test_find_workflow_yaml_path_not_found(catalog_dir):
    """按 name 反查 yaml_path 未找到 → None。"""
    assert find_workflow_yaml_path("ghost") is None


# ── §6.2 m13：YAML setup 段被 pydantic extra=forbid 拒绝（fail loud）─────────────


def test_setup_yaml_rejected_by_extra_forbid(catalog_dir):
    """YAML 含 ``setup:`` 段 → pydantic ``extra="forbid"`` 拒绝（ConfigurationError）。"""
    (catalog_dir / "legacy.yaml").write_text(SETUP_FORBIDDEN_YAML, encoding="utf-8")

    # catalog 扫描时 load_workflow 抛 ConfigurationError → 跳过，find 返 None
    assert find_workflow_by_name("setup_demo") is None

    # 直接 load 也 fail loud（fail loud 铁律，§6.2 m13）
    from orca.compile import load_workflow

    legacy_path = catalog_dir / "legacy.yaml"
    with pytest.raises(ConfigurationError):
        load_workflow(legacy_path)


# ── per-wf 双形态（plan 2026-08-27 批 C：layout.scan_workflow_yamls）──────────


def test_list_workflows_per_dir_layout(catalog_dir):
    """per-wf 形态：``<wf-dir>/workflow.yaml`` 被扫描列出（name 按 yaml 内 name 字段，非目录名）。"""
    bundled = catalog_dir / "bundled"
    bundled.mkdir()
    (bundled / "workflow.yaml").write_text(SIMPLE_WF, encoding="utf-8")

    result = list_workflows()

    assert len(result) == 1
    # 目录名 bundled ≠ name simple：匹配语义不变（yaml 内 name 字段）
    assert result[0]["name"] == "simple"


def test_list_workflows_dual_layout_flat_first_wins(catalog_dir):
    """双形态混存 + 同 name → 平铺优先（scan 列表序即优先级，first-wins 取平铺那份）。"""
    (catalog_dir / "flat.yaml").write_text(SIMPLE_WF, encoding="utf-8")
    bundled = catalog_dir / "bundled"
    bundled.mkdir()
    (bundled / "workflow.yaml").write_text(
        SIMPLE_WF.replace(
            "description: 简单 workflow",
            "description: from per-dir",
        ),
        encoding="utf-8",
    )

    result = list_workflows()

    assert len(result) == 1  # 同 name first-wins（不是两份）
    assert result[0]["description"] == "简单 workflow"  # 平铺先见，胜出


def test_list_workflows_dual_layout_both_listed(catalog_dir):
    """双形态混存 + 不同 name → 平铺与 per-wf 两个 workflow 都列出（互不遮蔽）。"""
    (catalog_dir / "flat.yaml").write_text(SIMPLE_WF, encoding="utf-8")
    bundled = catalog_dir / "bundled"
    bundled.mkdir()
    (bundled / "workflow.yaml").write_text(
        SIMPLE_WF.replace("name: simple", "name: per-dir-wf"),
        encoding="utf-8",
    )

    result = list_workflows()

    assert {item["name"] for item in result} == {"simple", "per-dir-wf"}


def test_find_workflow_by_name_per_dir_layout(catalog_dir):
    """find_workflow 同款双形态：按 name 找到 per-wf 形态并返回其 yaml 路径。"""
    bundled = catalog_dir / "bundled"
    bundled.mkdir()
    (bundled / "workflow.yaml").write_text(SIMPLE_WF, encoding="utf-8")

    wf = find_workflow_by_name("simple")
    assert wf is not None
    assert wf.name == "simple"
    path = find_workflow_yaml_path("simple")
    assert path is not None
    assert path.endswith("workflow.yaml")


# ── 进程内缓存（web-perf：指纹失效 + 语义不变）────────────────────────────────

# agent 引用 workflow（物化 prompt 来自相邻 agents/<name>/agent.md——用于钉
# 「相邻依赖文件纳入指纹」这条失效语义）。
AGENT_WF = """
name: agentwf
description: agent workflow
entry: a
nodes:
  - name: a
    kind: agent
    agent: myagent
    routes:
      - to: $end
"""


def _count_loads(monkeypatch):
    """包装 catalog.load_workflow 计数（命中缓存时不应增加）。"""
    counter = {"n": 0}
    real = catalog_module.load_workflow

    def _counting(path, resolver=None):
        counter["n"] += 1
        return real(path, resolver)

    monkeypatch.setattr(catalog_module, "load_workflow", _counting)
    return counter


def _bump_mtime(p):
    """强制 mtime_ns 变化（防文件系统时间戳粒度粗，rewrite 后指纹不变假阴）。"""
    st = p.stat()
    bumped = st.st_mtime_ns + 1_000_000
    os.utime(p, ns=(bumped, bumped))


def test_cache_hit_no_rescan(catalog_dir, monkeypatch):
    """首扫后重复调用命中缓存：load_workflow 不再被调（list + find 共用同一缓存）。"""
    (catalog_dir / "simple.yaml").write_text(SIMPLE_WF, encoding="utf-8")
    counter = _count_loads(monkeypatch)

    assert len(list_workflows()) == 1
    first = counter["n"]
    assert first >= 1  # 首扫真实加载

    # 重复调用（list / find / 薄 wrapper）全部走缓存，零重新加载
    list_workflows()
    find_workflow("simple")
    find_workflow_by_name("simple")
    find_workflow_yaml_path("simple")
    assert counter["n"] == first


def test_cache_invalidated_on_yaml_content_change(catalog_dir, monkeypatch):
    """yaml 内容改写（mtime/size 变）→ 指纹失配重扫，拿到新值。"""
    (catalog_dir / "simple.yaml").write_text(SIMPLE_WF, encoding="utf-8")
    counter = _count_loads(monkeypatch)

    assert list_workflows()[0]["description"] == "简单 workflow"

    yaml_path = catalog_dir / "simple.yaml"
    yaml_path.write_text(
        SIMPLE_WF.replace("简单 workflow", "改写后的描述"), encoding="utf-8"
    )
    _bump_mtime(yaml_path)

    result = list_workflows()
    assert result[0]["description"] == "改写后的描述"
    assert counter["n"] > 1  # 重扫真实发生


def test_cache_invalidated_on_yaml_add_remove(catalog_dir):
    """yaml 清单变化（新增/删除文件）被指纹感知。"""
    (catalog_dir / "simple.yaml").write_text(SIMPLE_WF, encoding="utf-8")
    assert len(list_workflows()) == 1

    # 删除 → 列表感知
    (catalog_dir / "simple.yaml").unlink()
    assert list_workflows() == []

    # 新增 → 列表感知（按 yaml 内 name 字段）
    (catalog_dir / "another.yaml").write_text(
        SIMPLE_WF.replace("name: simple", "name: another"), encoding="utf-8"
    )
    result = list_workflows()
    assert [item["name"] for item in result] == ["another"]


def test_cache_invalidated_on_adjacent_agent_md_change(catalog_dir):
    """相邻 agent md 改写 → 重扫物化新 prompt（load 结果不止取决于 yaml 自身）。

    钉依赖观察集（``_entry_watch_paths``）把 agent 入口 md 纳入 stat 指纹的失效
    语义：只改 agent md（yaml 不动）也必须拿到新物化结果。
    """
    agents_dir = catalog_dir / "agents" / "myagent"
    agents_dir.mkdir(parents=True)
    (agents_dir / "agent.md").write_text("do X", encoding="utf-8")
    (catalog_dir / "agentwf.yaml").write_text(AGENT_WF, encoding="utf-8")

    wf, _path = find_workflow("agentwf")
    assert wf.nodes[0].prompt == "do X"

    md = agents_dir / "agent.md"
    md.write_text("do Y", encoding="utf-8")
    _bump_mtime(md)

    wf2, _path2 = find_workflow("agentwf")
    assert wf2.nodes[0].prompt == "do Y"


def test_cache_first_wins_preserved(tmp_path, monkeypatch):
    """缓存路径下 first-wins 不变：project-local 同名覆盖 user-global（重复调用亦然）。"""
    dir1 = tmp_path / "dir1"
    dir2 = tmp_path / "dir2"
    dir1.mkdir()
    dir2.mkdir()
    (dir1 / "demo.yaml").write_text(SIMPLE_WF, encoding="utf-8")
    (dir2 / "override.yaml").write_text(
        SIMPLE_WF.replace(
            "description: 简单 workflow", "description: from dir2 override"
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "orca.compile.catalog._workflow_dirs",
        lambda: [dir1, dir2],
    )

    for _ in range(2):  # 第二次走缓存，first-wins 必须同样成立
        result = list_workflows()
        assert len(result) == 1
        assert result[0]["description"] == "简单 workflow"  # dir1 先见胜出


def test_cache_bad_yaml_fixed_after_scan(catalog_dir):
    """坏 yaml fail-soft 不污染缓存：修复（mtime 变）后被重新加载进列表。"""
    (catalog_dir / "good.yaml").write_text(SIMPLE_WF, encoding="utf-8")
    bad = catalog_dir / "bad.yaml"
    bad.write_text(BAD_YAML, encoding="utf-8")

    assert [item["name"] for item in list_workflows()] == ["simple"]

    bad.write_text(
        SIMPLE_WF.replace("name: simple", "name: fixed"), encoding="utf-8"
    )
    _bump_mtime(bad)

    assert {item["name"] for item in list_workflows()} == {"simple", "fixed"}


def test_find_workflow_returns_isolated_copy(catalog_dir):
    """缓存命中返回独立拷贝：调用方改 Workflow 对象不污染缓存（透明性契约）。"""
    (catalog_dir / "simple.yaml").write_text(SIMPLE_WF, encoding="utf-8")

    wf1, _ = find_workflow("simple")
    wf1.description = "hacked by caller"

    wf2, _ = find_workflow("simple")
    assert wf2.description == "简单 workflow"  # 缓存未被上一个调用方污染


def test_dropped_workflow_recovers_without_yaml_change(catalog_dir):
    """AgentNotFound 被 fail-soft 跳过的 wf：agent md 出现（yaml 不动）→ 回到列表。

    钉「坏 yaml 每次校验重试载」的失效语义——修复只动相邻文件时也不留 dropped 盲区。
    """
    (catalog_dir / "broken.yaml").write_text(AGENT_WF, encoding="utf-8")

    assert list_workflows() == []  # 引用的 myagent 缺失 → fail-soft 跳过

    agents_dir = catalog_dir / "agents" / "myagent"
    agents_dir.mkdir(parents=True)
    (agents_dir / "agent.md").write_text("now exists", encoding="utf-8")

    result = list_workflows()
    assert [item["name"] for item in result] == ["agentwf"]


def test_loaded_workflow_drops_when_referenced_script_removed(catalog_dir):
    """已加载 wf 的 agent body 引用脚本被删 → wf 从列表消失（脚本存在性进指纹）。

    钉「脚本引用父目录 mtime」观察项：``$ORCA_AGENT_RESOURCES/scripts/<f>`` 缺失
    是 validator error（fail-soft 跳过），删除/恢复不触碰 yaml 也必须被感知。
    """
    agent_dir = catalog_dir / "agents" / "myagent"
    (agent_dir / "scripts").mkdir(parents=True)
    (agent_dir / "scripts" / "run.py").write_text("print('hi')", encoding="utf-8")
    (agent_dir / "agent.md").write_text(
        "run $ORCA_AGENT_RESOURCES/scripts/run.py", encoding="utf-8"
    )
    (catalog_dir / "agentwf.yaml").write_text(AGENT_WF, encoding="utf-8")

    assert [item["name"] for item in list_workflows()] == ["agentwf"]

    (agent_dir / "scripts" / "run.py").unlink()
    _bump_mtime(agent_dir / "scripts")
    assert list_workflows() == []  # 脚本缺失 → validator error → 跳过

    (agent_dir / "scripts" / "run.py").write_text("print('hi')", encoding="utf-8")
    _bump_mtime(agent_dir / "scripts")
    assert [item["name"] for item in list_workflows()] == ["agentwf"]


def test_no_staleness_freeze_on_write_during_scan(catalog_dir, monkeypatch):
    """扫描期间（load 后、依赖 stat 前）的依赖写入不得固化进缓存。

    竞态窗口：``_catalog_entries`` 在 load 之后才取依赖 stat 戳——若 agent md
    恰在该窗口被改写，stamp 记新 mtime + entries 是旧内容，照常缓存会固化
    过期结果（下次校验 stat 与 stamp 一致 → 永远命中旧值）。钉
    ``_scan_started_before`` 拦截：stat 戳晚于扫描起点 → 本次不缓存，下次
    调用重扫拿新内容（最坏多一次重扫，方向保守）。
    """
    agents_dir = catalog_dir / "agents" / "myagent"
    agents_dir.mkdir(parents=True)
    (agents_dir / "agent.md").write_text("do X", encoding="utf-8")
    (catalog_dir / "agentwf.yaml").write_text(AGENT_WF, encoding="utf-8")

    real_scan = catalog_module._scan_catalog

    def _scan_and_touch_midway():
        entries = real_scan()
        # 模拟竞态窗口内的外部写：load 已完成、依赖 stat 戳尚未记录
        md = agents_dir / "agent.md"
        md.write_text("do Y", encoding="utf-8")
        _bump_mtime(md)
        return entries

    monkeypatch.setattr(catalog_module, "_scan_catalog", _scan_and_touch_midway)

    wf, _ = find_workflow("agentwf")
    assert wf.nodes[0].prompt == "do X"  # 本次返回的是扫描时刻快照（合法）

    # 中途恢复真实扫描路径（不能等 monkeypatch teardown——那是测试结束后；
    # 第二次调用此刻就得走真实 _scan_catalog，否则又被包裹层写一次 md）
    monkeypatch.setattr(catalog_module, "_scan_catalog", real_scan)
    wf2, _ = find_workflow("agentwf")
    assert wf2.nodes[0].prompt == "do Y"  # 未固化：下次调用重扫拿新物化结果


def test_cache_degrades_to_direct_scan_on_stat_error(catalog_dir, monkeypatch):
    """指纹 stat 失败（权限等 OSError）→ 降级直扫不缓存，数据仍正确（fail loud 不吞）。

    钉「缓存不吞错」契约的执行路径：观察集 stat 抛非 FileNotFoundError 的
    OSError → 本次结果不落缓存（下次重试），绝不能静默返回空 / 崩掉调用方。
    """
    (catalog_dir / "simple.yaml").write_text(SIMPLE_WF, encoding="utf-8")

    def _denied(_p):
        raise PermissionError("stat denied (simulated)")

    monkeypatch.setattr(catalog_module, "_stamp", _denied)

    # 首调：扫描 load 正常（不走 _stamp），写缓存段 stat 炸 → warning + 不缓存
    result = list_workflows()
    assert [item["name"] for item in result] == ["simple"]  # 数据正确
    assert catalog_module._CACHE is None  # 未落缓存（下次仍直扫重试）

    # 再调仍直扫成功——降级不固化、不崩溃
    assert [item["name"] for item in list_workflows()] == ["simple"]


SUBAGENT_MD_OK = """---
subagent: helper
version: 1
sentinel: ab12cd
---
do the sub-thing
"""


def test_cache_invalidated_on_subagents_md_change(catalog_dir):
    """subagents md 内容变化（yaml 不动）→ 失效感知：坏 frontmatter 使 wf 掉出列表。

    validator 读 subagents md 内容做 strict frontmatter 校验（缺三键 = error →
    fail-soft drop），故 md 内容是 load 结果的真实依赖——观察集四大面中此前
    唯一没被测试触碰的面。
    """
    sub_dir = catalog_dir / "subagents" / "agentwf"
    sub_dir.mkdir(parents=True)
    (sub_dir / "helper.md").write_text(SUBAGENT_MD_OK, encoding="utf-8")
    agents_dir = catalog_dir / "agents" / "myagent"
    agents_dir.mkdir(parents=True)
    (agents_dir / "agent.md").write_text("do X", encoding="utf-8")
    (catalog_dir / "agentwf.yaml").write_text(AGENT_WF, encoding="utf-8")

    assert [item["name"] for item in list_workflows()] == ["agentwf"]

    # 改坏 frontmatter（yaml / 目录结构不动）→ validator error → fail-soft 掉出
    md = sub_dir / "helper.md"
    md.write_text("no frontmatter at all", encoding="utf-8")
    _bump_mtime(md)
    assert list_workflows() == []

    # 恢复 → 回到列表
    md.write_text(SUBAGENT_MD_OK, encoding="utf-8")
    _bump_mtime(md)
    assert [item["name"] for item in list_workflows()] == ["agentwf"]


def test_cache_single_file_agent_md_change(catalog_dir):
    """单文件形态 agent（``agents/<name>.md``）改写 → 失效拿新 prompt。

    钉双形态入口双候选观察：base 下散置的非法 ``agents/agent.md`` 不得让
    is_file 探测误判文件夹形态而漏观察真入口 ``agents/myagent.md``（改写
    不失效 = 缓存固化 stale prompt）。
    """
    agents_dir = catalog_dir / "agents"
    agents_dir.mkdir()
    (agents_dir / "myagent.md").write_text("single file X", encoding="utf-8")
    # 散置干扰：resolver 对单文件 base 永不读它，但存在性足以误导 is_file 猜形态
    (agents_dir / "agent.md").write_text("stray decoy", encoding="utf-8")
    (catalog_dir / "agentwf.yaml").write_text(AGENT_WF, encoding="utf-8")

    wf, _ = find_workflow("agentwf")
    assert wf.nodes[0].prompt == "single file X"

    md = agents_dir / "myagent.md"
    md.write_text("single file Y", encoding="utf-8")
    _bump_mtime(md)

    wf2, _ = find_workflow("agentwf")
    assert wf2.nodes[0].prompt == "single file Y"


def test_stat_stamps_parallel_branch(catalog_dir):
    """``_stat_stamps`` 大集合（>64）走瞬时线程池分支：序确定、缺失记 None。"""
    (catalog_dir / "simple.yaml").write_text(SIMPLE_WF, encoding="utf-8")
    # 造 70 个真实存在 + 5 个缺失的路径（>64 触发并行分支）
    paths = [str(catalog_dir / f"p{i}.bin") for i in range(70)]
    for p in paths:
        Path(p).write_bytes(b"x")
    paths += [str(catalog_dir / f"missing{i}") for i in range(5)]

    stamps = catalog_module._stat_stamps(tuple(paths))

    assert [p for p, _s in stamps] == paths  # 顺序与输入一致（结果确定）
    assert all(s is not None for p, s in stamps if "missing" not in p)
    assert all(s is None for p, s in stamps if "missing" in p)
