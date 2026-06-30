"""tests/exec/conftest.py —— exec 测试共享 fixtures。

约定（同 tests/events/test_bus.py）：本仓库不用 pytest-asyncio，异步统一 ``asyncio.run``。

只放 autouse 的 ``_reset_profiles_registry``（pytest 自动发现，无需 import）和
``full_stream_lines`` fixture（同上）。**不**放 helper 函数 —— 本仓库 ``tests`` 非包
（无 ``__init__.py``），跨目录 import helper 会失败；故 ``FakeRunner`` / ``run_async``
等 helper 在需要的测试文件内就地定义（轻微重复，可接受 —— 测试代码不发布）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orca.profiles.registry import _reset_for_test

# 真实 stream-json fixture（42 行 bash 调用流，从 AgentHarness 录制，只读拷贝）。
CLAUDE_FIXTURE = (
    Path(__file__).resolve().parents[1] / "profiles" / "fixtures" / "sample_with_bash.jsonl"
)


@pytest.fixture(autouse=True)
def _reset_profiles_registry():
    """每个测试前重置 profiles 注册表（隔离全局状态，与 tests/profiles 一致）。

    autouse 覆盖 tests/exec/ 全部测试：factory / executor / e2e 都依赖注册表干净态。
    """
    _reset_for_test()
    yield
    _reset_for_test()


@pytest.fixture(scope="module")
def full_stream_lines() -> list[str]:
    """完整 42 行 fixture（每行一个 stream-json）。"""
    return [ln for ln in CLAUDE_FIXTURE.read_text(encoding="utf-8").splitlines() if ln.strip()]
