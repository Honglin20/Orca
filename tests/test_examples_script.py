"""tests/test_examples_script.py —— 纯 script example 跑通验证（零 token，确定性）。

8 个纯 script/set/wait example（不 spawn agent）：用 run_workflow 跑，断言到终态
（completed 或 failed——demo_failure/demo_max_iter 故意 failed 是预期跑通）。
不烧 API，秒级。goal 验收：script example 全跑通。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orca.compile.parser import load_workflow
from orca.run import run_workflow

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# 纯 script demo（零 token）。demo_failure 故意 failed（演示失败冒泡）；
# demo_max_iter 可能 failed（超迭代）—— 都算"跑通到终态"。
SCRIPT_EXAMPLES = [
    "demo_linear",
    "demo_loop",
    "demo_parallel",
    "demo_foreach",
    "demo_failure",
    "demo_max_iter",
    "terminate",
    "with_wait",
]


@pytest.mark.parametrize("name", SCRIPT_EXAMPLES)
def test_script_example_reaches_terminal_state(name: str, tmp_path: Path, monkeypatch) -> None:
    """script example 跑到终态（completed/failed）。零 token，确定性。"""
    monkeypatch.chdir(tmp_path)
    wf = load_workflow(EXAMPLES / f"{name}.yaml")
    state = asyncio.run(run_workflow(wf, None))
    assert state.status in ("completed", "failed"), (
        f"{name}: 期望终态 completed/failed，got {state.status!r}（卡住/崩溃=未跑通）"
    )
