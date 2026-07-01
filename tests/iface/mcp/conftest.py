"""tests/iface/mcp/conftest.py —— mcp/ 测试共享 fixtures + helpers（D1 单元测试用）。

约定（同 tests/run/conftest.py / tests/gates/conftest.py）：本仓库不用 pytest-asyncio，
异步统一 ``asyncio.run``。``run_async`` / ``make_tape`` 在本文件定义，被同包测试引用。

D1 测试只覆盖三个纯函数 / manager 方法（pending_gates_from_tape / run_summary /
cancel_run），不涉及 MCP server / transport / stdio（那是 D2-D5 的事）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from orca.events.tape import Tape


def run_async(coro):
    """统一 asyncio.run（无 pytest-asyncio）。"""
    return asyncio.run(coro)


def make_tape(tmp_path: Path, run_id: str = "r1", name: str = "events.jsonl") -> Tape:
    """构造空 Tape（写 tmp_path，不污染 cwd）。调用方自己 append 事件。"""
    return Tape(tmp_path / name, run_id=run_id)
