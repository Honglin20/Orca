"""test_skills_bundle.py —— 随包 skill 产物守门。

防回归：确保 ``orca/skills/create-workflow/`` 下的 SKILL.md / reference / examples
随包打进 wheel 并能被 ``importlib.resources`` 定位（``teams skill install`` 据此拷贝）。
若 pyproject 打包规则漂移漏文件，此测试先红。
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def _skill_dir() -> Path:
    return Path(str(files("orca.skills"))) / "create-workflow"


def test_skill_md_present():
    assert (_skill_dir() / "SKILL.md").is_file()


def test_contract_reference_present():
    ref = _skill_dir() / "reference" / "orca-workflow-contract.md"
    assert ref.is_file()
    # 契约参考要点不缺失（粗粒度）
    text = ref.read_text()
    assert " cheatsheet" in text.lower()
    assert "validate" in text.lower()


def test_crib_examples_present():
    examples = list((_skill_dir() / "examples").glob("*.yaml"))
    assert len(examples) >= 3, f"期望 ≥3 个 crib yaml，实际 {len(examples)}"


def _crib_yaml_paths() -> list[str]:
    return sorted(str(p) for p in (_skill_dir() / "examples").glob("*.yaml"))


import pytest

from orca.compile import load_workflow


@pytest.mark.parametrize("yaml_path", _crib_yaml_paths(), ids=lambda p: Path(p).name)
def test_crib_examples_validate_clean(yaml_path: str):
    """SKILL.md 强制生成的 yaml 必须过 ``orca validate``——自带 crib 样板自己得先过。

    schema 演化让 crib yaml 失效时，此测试先红（skill 教用户抄的样板不能坏）。
    load_workflow 内部跑全部 validate 检查，invalid → 抛 ConfigurationError。
    """
    wf = load_workflow(yaml_path)  # 抛 ConfigurationError 即测试红
    assert wf.name  # 确认真加载出来了，不是空对象
