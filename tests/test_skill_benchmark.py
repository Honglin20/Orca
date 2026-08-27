"""test_skill_benchmark.py —— create-workflow skill benchmark 守门。

benchmark（``orca/skills/create-workflow/benchmark/cases/``）钉死了每个 case 的预期产物。
本测试对**每个** ``expected/workflow.yaml`` 跑 ``load_workflow``（含全部 validate 检查），
schema 演化让某 case 失效时先红——skill 教用户产出的样板本身不能坏。

额外检查 folder-agent 资产迁移不变量（case 11/16）：脚本已迁移 + agent.md 用
``$ORCA_AGENT_RESOURCES`` 引用（skill→文件夹 agent 的核心转换规则）。

元层校验脚本守门（golden 即 ``scripts/check_*.py`` 三脚本自身的验收基准）：
  - case 17 正控：``check_charts`` 清单 ≥1 call site（防忘写 render_chart 的 vacuous pass）；
  - 全 golden：``check_dev_residue`` 扫每 case ``expected/``（教学样板自身洁净）；
  - 全 golden：``check_agent_md_static`` 扫有 workflow.yaml 的 case（folder agent 布局受检）。
"""

from __future__ import annotations

import re
import subprocess
import sys
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


# ── 元层校验脚本守门（subprocess 跑三脚本，golden 即其验收基准）────────────────


_SCRIPTS_DIR = Path(str(files("orca.skills"))) / "create-workflow" / "scripts"


def _all_cases() -> list[tuple[str, Path]]:
    """全部 case 目录（含 agent-pool-only 等无 workflow.yaml 的 case）。"""
    return [(d.name, d) for d in sorted(_benchmark_dir().iterdir()) if d.is_dir()]


def _run_check(script: str, *paths: Path) -> subprocess.CompletedProcess:
    """subprocess 跑元层校验脚本（stdlib-only、随 skill 分发，不 import 进本测试进程）。"""
    return subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / script), *[str(p) for p in paths]],
        capture_output=True, text=True, timeout=120,
    )


def _manifest_count(proc: subprocess.CompletedProcess, unit: str) -> int:
    """解析扫描清单行（``<path> → N files``；charts 为 ``… / M call sites``）的计数。

    单输入路径 → stdout 恰一条清单行；解析不到即 fail（防「零扫描即绿」的 vacuous pass）。
    """
    m = re.search(rf"(\d+)\s*{re.escape(unit)}", proc.stdout)
    assert m, (
        f"stdout 缺扫描清单行（… → N {unit}）：\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    return int(m.group(1))


def test_case17_charts_positive_control():
    """正控守门：case 17 expected/ 跑 check_charts —— exit 0 且清单 ≥1 call site。

    check_charts 对零 call site（无图表 workflow）是合法 exit 0，故必须靠清单计数做正控：
    bench_plot.py 忘写 render_chart 时这里红（而非 vacuous pass）。
    """
    expected = _benchmark_dir() / "17-chart-integration" / "expected"
    assert (expected / "workflow.yaml").is_file(), "case 17 必有 workflow.yaml（golden validate 前提）"
    proc = _run_check("check_charts.py", expected)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert _manifest_count(proc, "call sites") >= 1


@pytest.mark.parametrize("case_name, case_dir", _all_cases(), ids=[n for n, _ in _all_cases()])
def test_golden_expected_no_dev_residue(case_name: str, case_dir: Path):
    """全 golden 守门：每 case 的 expected/ 跑 check_dev_residue —— exit 0 且清单 ≥1 files。

    教学样板自身必须洁净（golden 同时是 check_dev_residue 的验收基准）。case.md/input.txt
    的场景叙事不进扫描——只扫 expected/ 的 yaml/md/py。
    """
    proc = _run_check("check_dev_residue.py", case_dir / "expected")
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert _manifest_count(proc, "files") >= 1


@pytest.mark.parametrize(
    "case_name, yaml_path",
    _workflow_cases(),
    ids=[name for name, _ in _workflow_cases()],
)
def test_golden_workflow_agent_md_static(case_name: str, yaml_path: Path):
    """全 golden 守门：有 workflow.yaml 的 case 跑 check_agent_md_static（传 yaml，扫同级 agents/）。

    exit 0 必须；含 agents/ 的 case 额外断言清单 ≥1 files（folder/file agent 布局受检）；
    inline-only case 清单 0 files 是合法态（脚本契约允许）。
    """
    proc = _run_check("check_agent_md_static.py", yaml_path)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    if (yaml_path.parent / "agents").is_dir():
        assert _manifest_count(proc, "files") >= 1


def test_golden_agent_pool_only_agent_md_static():
    """agent-pool-only case（无 workflow.yaml，不进上面 parametrize）的 agent md 同样受检。

    传 expected/agents 目录直扫（脚本支持目录输入）——agent 池是最纯的 agent 产物，不能漏检。
    """
    agents_dir = _benchmark_dir() / "14-agent-pool-only" / "expected" / "agents"
    assert agents_dir.is_dir(), "case 14 必有 expected/agents/"
    proc = _run_check("check_agent_md_static.py", agents_dir)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert _manifest_count(proc, "files") >= 3
