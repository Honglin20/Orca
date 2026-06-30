"""tests/run/conftest.py —— run/ 测试共享 fixtures + FakeExecutor helper。

约定（同 tests/exec/conftest.py）：本仓库不用 pytest-asyncio，异步统一 ``asyncio.run``。
``FakeExecutor`` / ``make_bus`` / ``run_async`` 在本文件定义，被同包测试以
``from tests.run.conftest import ...`` 引用（``tests/`` 与 ``tests/run/`` 均为包）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import pytest

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.exec.context import RunContext
from orca.exec.interface import Executor
from orca.schema import Event


def run_async(coro):
    """统一 asyncio.run（无 pytest-asyncio）。"""
    return asyncio.run(coro)


def make_bus(tmp_path: Path, run_id: str = "r1") -> tuple[EventBus, Tape]:
    """构造 (EventBus, Tape) pair（Tape 写 tmp_path，不污染 cwd）。"""
    tape = Tape(tmp_path / "events.jsonl", run_id=run_id)
    return EventBus(tape), tape


def ev(type_: str, data: dict | None = None, *, node: str = "n", session_id: str = "s1") -> Event:
    """构造占位 Event（seq=0，bus.emit 内部重分配）。"""
    return Event(
        seq=0,
        type=type_,  # type: ignore[arg-type]
        timestamp=0.0,
        node=node,
        session_id=session_id,
        data=data or {},
    )


class FakeExecutor(Executor):
    """注入确定事件流的假 executor（不 spawn）。

    用法：
        FakeExecutor.produces({"x": 1})     # node_completed with output {"x":1}
        FakeExecutor.failing("timeout", ...) # node_failed
        FakeExecutor.events([ev(...), ...])  # 完全自定义事件序列
    """

    def __init__(self, events: list[Event], *, node_name: str = "n"):
        self._events = events
        self._node_name = node_name

    @classmethod
    def produces(cls, output: Any, *, node_name: str = "n", kind: str = "fake") -> FakeExecutor:
        """产出 [node_started, node_completed(output)]（成功路径）。"""
        return cls(
            [
                ev("node_started", {"kind": kind}, node=node_name),
                ev("node_completed", {"output": output, "elapsed": 0.0}, node=node_name),
            ],
            node_name=node_name,
        )

    @classmethod
    def failing(
        cls,
        *,
        error_type: str = "ExecTimeout",
        message: str = "失败",
        phase: str = "timeout",
        node_name: str = "n",
    ) -> FakeExecutor:
        """产出 [node_started, node_failed]（失败路径）。"""
        return cls(
            [
                ev("node_started", {}, node=node_name),
                ev(
                    "node_failed",
                    {"error_type": error_type, "message": message, "phase": phase},
                    node=node_name,
                ),
            ],
            node_name=node_name,
        )

    @classmethod
    def events(cls, events: Iterable[Event], *, node_name: str = "n") -> FakeExecutor:
        return cls(list(events), node_name=node_name)

    async def exec(self, node, ctx: RunContext) -> AsyncIterator[Event]:  # type: ignore[override]
        for e in self._events:
            yield e


@pytest.fixture(autouse=True)
def _reset_profiles_registry():
    """每个 run 测试前重置 profiles 注册表（与 tests/exec 一致，隔离全局状态）。"""
    from orca.profiles.registry import _reset_for_test

    _reset_for_test()
    yield
    _reset_for_test()
