"""tests/gates/conftest.py —— gates/ 测试共享 fixtures。

约定（同 tests/run/conftest.py）：本仓库不用 pytest-asyncio，异步统一 ``asyncio.run``。
``make_bus`` / ``run_async`` 在本文件定义，被同包测试以 ``from tests.gates.conftest
import ...`` 引用。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.gates.handler import HumanGateHandler
from orca.gates.types import HumanGate


def run_async(coro):
    """统一 asyncio.run（无 pytest-asyncio）。"""
    return asyncio.run(coro)


def make_bus(tmp_path: Path, run_id: str = "r1") -> tuple[EventBus, Tape]:
    """构造 (EventBus, Tape) pair（Tape 写 tmp_path，不污染 cwd）。"""
    tape = Tape(tmp_path / "events.jsonl", run_id=run_id)
    return EventBus(tape), tape


def make_gate(
    gate_id: str = "g1",
    *,
    prompt: str = "批准？",
    options: list[str] | None = None,
    context: dict | None = None,
    source: str = "tool_permission",
    run_id: str = "r1",
    node: str | None = "n1",
    session_id: str | None = "sess-test",
) -> HumanGate:
    """构造测试用 HumanGate（默认 tool_permission source + allow/deny options）。"""
    return HumanGate(
        id=gate_id,
        prompt=prompt,
        options=options if options is not None else ["allow", "deny"],
        context=context if context is not None else {"tool": "Bash"},
        source=source,  # type: ignore[arg-type]
        run_id=run_id,
        node=node,
        session_id=session_id,
    )


async def _start_handler(handler: HumanGateHandler) -> None:
    """启动 broadcaster（测试 helper，保证 emit resolved 不漏）。"""
    await handler.start()


async def _stop_handler(handler: HumanGateHandler) -> None:
    """停止 broadcaster（测试 helper，防 leaked task 警告）。"""
    await handler.stop()
