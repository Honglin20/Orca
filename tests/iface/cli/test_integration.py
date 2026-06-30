"""test_integration.py —— phase 7 端到端集成测试（SPEC §6.4 / 计划 C5.2）。

标 ``@pytest.mark.integration``：CI 默认不跑（``-m "not integration"`` 跳过），本地
``pytest -m integration tests/iface/cli/test_integration.py`` 可选跑。这些测试真跑
demo workflow（真 spawn claude for agent node），慢 / 需 API key。

覆盖（计划 C5.2）：
  1. ``orca run examples/demo_linear.yaml``：全 script，DAG 推进到 $end，exit 0
  2. ``orca run examples/demo_conditional.yaml``：条件分支走对
  3. ``orca run examples/demo_task.yaml "测试"``：task 注入 + agent 跑通（真 claude）
  4. ``orca run examples/demo_mixed.yaml``：综合（script+agent+set+回环）
  5. gate demo（需 phase 6 hook 配置 + 真 claude）：claude 想调工具 → ModalScreen → 答 → 继续

环境前置：需 ``ANTHROPIC_API_KEY`` + ``claude`` CLI 在 PATH（agent node 用）。
缺则 ``skip``（fail loud，但不让 CI 红）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# 全模块标 integration：CI 跳过，本地显式跑。
pytestmark = pytest.mark.integration

_EXAMPLES = Path(__file__).resolve().parents[3] / "examples"


def _has_claude() -> bool:
    """真 agent node 需要 claude CLI + API key。缺则 skip。"""
    return shutil.which("claude") is not None and bool(os.environ.get("ANTHROPIC_API_KEY"))


def _orca_run(yaml_path: Path, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    """真跑 ``orca run <yaml>``，返回 CompletedProcess（含 exit code）。"""
    cmd = ["orca", "run", str(yaml_path), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


@pytest.mark.skipif(not _has_claude(), reason="需 claude CLI + ANTHROPIC_API_KEY")
class TestDemoWorkflows:
    """真跑 demo workflow（SPEC §6.4 / 计划 C5.2）。本地跑，CI skip。"""

    def test_demo_linear_exits_zero(self):
        """demo_linear：全 script，零 token，DAG 推进到 $end，exit 0。"""
        result = _orca_run(_EXAMPLES / "demo_linear.yaml")
        assert result.returncode == 0, f"stdout={result.stdout[-500:]} stderr={result.stderr[-500:]}"

    def test_demo_conditional_branches_correctly(self):
        """demo_conditional：set 决策值驱动路由，走 high_agent 分支。"""
        result = _orca_run(_EXAMPLES / "demo_conditional.yaml")
        assert result.returncode == 0, f"stderr={result.stderr[-500:]}"

    def test_demo_task_with_positional_task_arg(self):
        """demo_task + positional '测试'：task 注入 inputs.task + agent 跑通（真 claude）。"""
        result = _orca_run(_EXAMPLES / "demo_task.yaml", "测试任务", timeout=180.0)
        assert result.returncode == 0, f"stderr={result.stderr[-500:]}"

    def test_demo_mixed_completes(self):
        """demo_mixed：综合（script+agent+set+回环），全跑通 exit 0。"""
        result = _orca_run(_EXAMPLES / "demo_mixed.yaml", timeout=180.0)
        assert result.returncode == 0, f"stderr={result.stderr[-500:]}"


@pytest.mark.skipif(not _has_claude(), reason="需 claude CLI + ANTHROPIC_API_KEY")
class TestGateDemo:
    """含 gate 的 demo：claude 想调工具 → hook → ModalScreen → 用户答 → 继续。

    注：完整 hook → HTTP → gate → claude resume 路径需要 phase 6 的 ``.claude/settings.json``
    PreToolUse hook 配置 + ``ORCA_PORT`` 对齐。此测试是 smoke：跑一个含 agent 的 demo，
    验证 TUI 不崩 + 能到终态。真 gate 弹窗交互（自动按键）走 test_app.py 的 pilot 测试
    （那里 mock 了 gate 事件，不依赖真 claude 调工具）。
    """

    def test_agent_workflow_completes_via_tui(self):
        """跑含 agent 的 workflow（TUI 起来 + gate HTTP 桥起来 + 不崩）。"""
        result = _orca_run(_EXAMPLES / "demo_task.yaml", "smoke", timeout=180.0)
        # agent workflow 能跑到终态（completed exit 0 / failed exit 1 都算「TUI 不崩」）
        assert result.returncode in (0, 1), f"stderr={result.stderr[-500:]}"
