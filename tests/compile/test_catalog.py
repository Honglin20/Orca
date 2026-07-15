"""test_catalog.py —— workflow catalog 单元测试（SPEC phase-10 §5.6 / §2.2 + in-session v5 §6.2）。

覆盖：
  - ``list_workflows`` 扫目录 + 返 inputs_schema（无 has_setup，setup 全栈删）
  - ``describe_workflow`` 返 inputs_schema（无 setup 元信息）
  - ``find_workflow_by_name`` first-wins 优先级
  - ``find_workflow_yaml_path`` 反查路径
  - 加载失败的 YAML 跳过（log warning，不中断列表）
  - YAML 含 ``setup:`` 段被 pydantic ``extra="forbid"`` 拒绝（fail loud，§6.2 m13）

设计：monkeypatch ``_workflow_dirs`` 指向 tmp_path/workflows（隔离测试）。
"""

from __future__ import annotations

import pytest

from orca.compile import ConfigurationError
from orca.compile.catalog import (
    describe_workflow,
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
