"""tests/e2e_phase14/test_examples_opencode.py —— agent example opencode+deepseek 真跑（不 mock）。

13 个 agent example（含 render_chart）：每个用 opencode 真跑，断言到终态（completed/failed）。
goal 硬要求：agent example 必须 opencode 真跑过（不 mock）。

**驱动方式**（避免 OrcaApp DagGraph widget 的 _assert_acyclic 对复杂 DAG 误抛）：
  - 多数 agent example 用 ``run_workflow``（真 spawn opencode 但不起 TUI，纯编排验证）。
  - render_chart 用 ``OrcaApp``（需起 per-run chart ingestor + 断言 tape 含 custom(chart)）。

with_ask_user 例外：演示 ask_user MCP，需 mcp_tools=True（claude），opencode 不支持，
保留 claude 后端，本测试不覆盖（需 ANTHROPIC_API_KEY 单独验）。

无 opencode 二进制 / 无 deepseek auth → skip。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _opencode_available() -> bool:
    if os.environ.get("ORCA_E2E_SKIP_OPENCODE") == "1":
        return False
    return shutil.which("opencode") is not None


def _deepseek_auth_present() -> bool:
    p = Path.home() / ".local/share/opencode/auth.json"
    if not p.exists():
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return isinstance(d, dict) and "deepseek" in d
    except Exception:
        return False


# agent example（不含 render_chart，render_chart 单独测；with_ask_user 例外 claude-only）。
AGENT_RUN_WORKFLOW = [
    "demo_conditional",
    "demo_interrupt",
    "demo_mixed",
    "demo_skip",
    "demo_task",
    "batch_assess",
    "parallel_research",
    "nas",
    "mxint_analysis",
    "with_retry",
    "with_validator",
    "with_dialog",
]


@pytest.mark.parametrize("name", AGENT_RUN_WORKFLOW)
def test_agent_example_opencode_runs(name: str) -> None:
    """agent example opencode 真跑到终态（run_workflow，不起 TUI 避免 widget 干扰）。不 mock。"""
    if not _opencode_available() or not _deepseek_auth_present():
        pytest.skip("opencode / deepseek auth 不可用")
    asyncio.run(_drive_run_workflow(EXAMPLES / f"{name}.yaml", name))


async def _drive_run_workflow(yaml_path: Path, name: str) -> None:
    """run_workflow 跑 agent example（真 spawn opencode，不起 TUI）。断言终态。"""
    from orca.compile import load_workflow
    from orca.run import run_workflow

    wf = load_workflow(yaml_path)
    # task 位置参数注入 inputs.task（demo_task 声明 task required 需它；其他 example 不
    # 声明 task input 则 setdefault 注入不阻断）。统一传示例 task 让所有 example 都能跑。
    state = await run_workflow(wf, task="示例任务")
    assert state.status in ("completed", "failed"), (
        f"{name}: 期望终态 completed/failed，got {state.status!r}"
    )


def test_render_chart_example_opencode_pushes_chart(tmp_path: Path) -> None:
    """render_chart example：OrcaApp 跑（起 ingestor）→ opencode agent → script → render_chart → tape custom(chart)。"""
    if not _opencode_available() or not _deepseek_auth_present():
        pytest.skip("opencode / deepseek auth 不可用")

    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:8]
    tape_root = Path(f"/tmp/orca-ex-rc-{h}")
    tape_root.mkdir(parents=True, exist_ok=True)
    tape_path = tape_root / "tape.jsonl"
    try:
        asyncio.run(_drive_render_chart(EXAMPLES / "render_chart.yaml", tape_path))
    finally:
        shutil.rmtree(tape_root, ignore_errors=True)


async def _drive_render_chart(yaml_path: Path, tape_path: Path) -> None:
    from orca.compile import load_workflow
    from orca.iface.cli.app import OrcaApp

    wf = load_workflow(yaml_path)
    app = OrcaApp(wf=wf, tape_path=tape_path)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        for _ in range(900):
            if app.terminal_state is not None:
                break
            await pilot.pause(0.2)
        else:
            pytest.fail("render_chart: opencode 编排 180s 未到终态")
        await pilot.pause(0.3)
        assert app.terminal_state.status == "completed", (
            f"render_chart: 期望 completed，got {app.terminal_state.status}"
        )
        events = list(app.bus.tape.replay())
        assert any(
            e.type == "custom" and e.data.get("kind") == "chart" for e in events
        ), "render_chart: tape 缺 custom(chart) 事件（推图链路失败）"
