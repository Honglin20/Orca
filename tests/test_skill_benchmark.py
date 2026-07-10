"""test_skill_benchmark.py —— create-workflow skill benchmark 守门。

benchmark（``orca/skills/create-workflow/benchmark/cases/``）钉死了每个 case 的预期产物。
本测试对**每个** ``expected/workflow.yaml`` 跑 ``load_workflow``（含全部 validate 检查），
schema 演化让某 case 失效时先红——skill 教用户产出的样板本身不能坏。

额外检查 folder-agent 资产迁移不变量（case 11/16）：脚本已迁移 + agent.md 用
``$ORCA_AGENT_RESOURCES`` 引用（skill→文件夹 agent 的核心转换规则）。
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pytest

from orca.compile import load_workflow


def _benchmark_dir() -> Path:
    return Path(str(files("orca.skills"))) / "create-workflow" / "benchmark" / "cases"


def _workflow_cases() -> list[tuple[str, Path]]:
    """所有带 expected/workflow.yaml 的 case。"""
    out = []
    for case_dir in sorted(_benchmark_dir().iterdir()):
        yml = case_dir / "expected" / "workflow.yaml"
        if yml.exists():
            out.append((case_dir.name, yml))
    return out


@pytest.mark.parametrize(
    "case_name, yaml_path",
    _workflow_cases(),
    ids=[name for name, _ in _workflow_cases()],
)
def test_benchmark_workflow_validates(case_name: str, yaml_path: Path):
    """每个 benchmark 预期 workflow 必须 0 error 通过 validate（含 agent 解析）。"""
    wf = load_workflow(yaml_path)  # 抛 ConfigurationError 即红
    assert wf.name, f"{case_name}: workflow 加载出空 name"


def test_agent_pool_only_case_has_no_workflow():
    """case 14（只造 agent 池）不应有 workflow.yaml，且必有 agent md。"""
    case = _benchmark_dir() / "14-agent-pool-only"
    assert not (case / "expected" / "workflow.yaml").exists()
    agents = list((case / "expected" / "agents").glob("*.md"))
    assert len(agents) >= 3, f"期望 ≥3 个 agent md，实际 {len(agents)}"


@pytest.mark.parametrize("slug", ["11-skill-with-script", "16-script-folder-agent"])
def test_folder_agent_asset_migration(slug: str):
    """skill→文件夹 agent 的核心转换：脚本迁移到 agents/<name>/scripts/ + agent.md 用 $ORCA_AGENT_RESOURCES 引用。"""
    case = _benchmark_dir() / slug
    agents_dir = case / "expected" / "agents"
    # 找到那个文件夹 agent（含 agent.md + scripts/）
    folder_agents = [d for d in agents_dir.iterdir() if (d / "agent.md").exists()]
    assert folder_agents, f"{slug}: 缺文件夹 agent"
    agent_dir = folder_agents[0]
    # 1) 脚本已迁移
    scripts = list((agent_dir / "scripts").glob("*"))
    assert scripts, f"{slug}: 脚本未迁移到 {agent_dir}/scripts/"
    # 2) agent.md prompt 用 $ORCA_AGENT_RESOURCES 引用（非相对路径）
    body = (agent_dir / "agent.md").read_text()
    assert "$ORCA_AGENT_RESOURCES" in body, f"{slug}: agent.md 未重写为 $ORCA_AGENT_RESOURCES 引用"
