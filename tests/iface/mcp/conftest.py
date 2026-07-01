"""tests/iface/mcp/conftest.py —— mcp/ 测试共享 fixtures + helpers。

约定（同 tests/run/conftest.py / tests/gates/conftest.py / tests/iface/web/conftest.py）：
本仓库不用 pytest-asyncio，异步统一 ``asyncio.run``。``run_async`` / ``make_tape``
（D1 用）+ ``orca_mcp_subprocess`` / ``orca_mcp_inprocess``（D5 E2E 用）在本文件定义。

D5 E2E 两个核心 fixture（SPEC phase-10 §D5.1）：
  - ``orca_mcp_subprocess``：spawn 真 ``orca mcp`` 子进程 + ``stdio_client`` 连接，
    真 stdio round-trip（SPEC §6.3 硬验证「端到端」字面意义）。用于 E2E-1 / E2E-4 / E2E-5。
  - ``orca_mcp_inprocess``：真 RunManager + OrcaMcpServer（不 spawn 子进程），测试侧
    直接访问 handle fire 合成 gate。用于 E2E-2 / E2E-3（需直接调 handler.request / resolve）。

铁律（SPEC §6.3 硬 invariant）：
  - 不 mock 引擎：两个 fixture 都用真 ``Orchestrator`` + 真 tape + 真 EventBus。
  - 不 mock gates（E2E-2/3）：用真 ``HumanGateHandler.request`` / ``resolve``。
  - runs_dir 隔离：subprocess fixture 用 ``--runs-dir tmp_path``（避免污染项目 ``runs/``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from orca.events.tape import Tape
from orca.iface.mcp.server import OrcaMcpServer
from orca.iface.web.run_manager import RunManager


def run_async(coro):
    """统一 asyncio.run（无 pytest-asyncio）。"""
    return asyncio.run(coro)


def make_tape(tmp_path: Path, run_id: str = "r1", name: str = "events.jsonl") -> Tape:
    """构造空 Tape（写 tmp_path，不污染 cwd）。调用方自己 append 事件。"""
    return Tape(tmp_path / name, run_id=run_id)


# ── subprocess fixture（真 stdio round-trip，SPEC §D5.1 / §6.3 硬 invariant 7）────


@contextlib.asynccontextmanager
async def orca_mcp_subprocess(runs_dir: Path, *extra: str):
    """async context：spawn ``orca mcp`` + stdio_client 连接 → yield ClientSession。

    测试侧 ``run_async`` 内 ``async with orca_mcp_subprocess(tmp_path):`` 用。返回已
    ``initialize()`` 的 ``ClientSession``；退出时 ``stdio_client`` 自动收尾子进程
    （``process`` context manager 终止子进程）。失败时 stderr 写 ``runs_dir/server.stderr``
    便于调试（subprocess stderr 不污染测试输出）。

    ``--runs-dir runs_dir`` 隔离 tape 落盘到 tmp_path（不污染项目 ``runs/``）。
    用 ``uv run orca`` 拉 console_script（不用 ``python -m orca.iface.cli.commands`` ——
    ``orca.iface.cli.__init__`` 会拉 textual import，无必要且慢）。

    **注**：asyncio subprocess transport 在 Python 3.12 有 ``__del__`` GC 时机小毛病——
    transport 对象在 ``asyncio.run`` 关 loop 后才被 GC，``__del__`` 调 ``call_soon`` raise
    ``RuntimeError: Event loop is closed``（pytest 报 ``PytestUnraisableExceptionWarning``）。
    这是 CPython + asyncio 的已知小毛病（非 Orca bug、非 mcp SDK bug），
    ``pyproject.toml`` ``filterwarnings`` 静默此特定 warning（不影响 RuntimeWarning 检查）。
    """
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    stderr_path = runs_dir / "server.stderr"
    stderr_file = open(stderr_path, "w", encoding="utf-8")
    try:
        params = StdioServerParameters(
            command="uv",
            args=["run", "orca", "mcp", "--runs-dir", str(runs_dir),
                  "--max-concurrent", "3", *extra],
            cwd=str(Path(__file__).resolve().parents[3]),
            errlog=stderr_file,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    finally:
        stderr_file.close()


# ── in-process fixture（真 RunManager + OrcaMcpServer，不 spawn 子进程）────────────


class InProcessHarness:
    """in-process 真引擎 harness（SPEC §D5.1 / E2E-2 / E2E-3 用）。

    持有：
      - ``manager``：真 ``RunManager``（runs_dir 隔离到 tmp_path），多 run 真并发托管。
      - ``server``：真 ``OrcaMcpServer``（tool_* 是 bound method，单测直调，绕开 stdio）。

    两者都用真 ``Orchestrator`` + 真 ``HumanGateHandler`` + 真 ``EventBus`` + 真 ``Tape``——
    **不 mock 引擎、不 mock gates**（SPEC §6.3 硬 invariant 1 / 2）。
    """

    def __init__(self, runs_dir: Path, max_concurrent: int = 3) -> None:
        self.manager = RunManager(
            max_concurrent=max_concurrent, runs_dir=runs_dir / "runs"
        )
        self.server = OrcaMcpServer(self.manager)
        self.runs_dir = runs_dir

    async def aclose(self) -> None:
        """teardown：``manager.shutdown`` 等所有 in-flight run + 关 gate_handler。"""
        await self.manager.shutdown(timeout=5.0)


def make_inprocess_harness(runs_dir: Path) -> InProcessHarness:
    """构造 InProcessHarness（runs_dir 隔离到 tmp_path）。"""
    return InProcessHarness(runs_dir=runs_dir)


# ── helpers（E2E 共用）─────────────────────────────────────────────────────────


def slow_workflow(tmp_path: Path) -> Path:
    """构造慢 script workflow（单节点 ``sleep 10``），保证 fire gate 时 run 还活着。

    E2E-2 / E2E-3 共用（DRY）。orchestrator 跑 ``sleep 10`` 期间 tape 不 close，测试侧
    fire + resolve 有 10s 窗口。纯 script，零 token / 零 claude。

    为何不用 ``examples/demo_linear.yaml``：demo_linear 三节点 echo 秒级完成，orchestrator
    完成后 ``_teardown_handle`` 关 tape，测试侧 fire gate 写 tape 会
    ``RuntimeError: Tape 已 close``。慢 script 让 orchestrator 在 sleep 期间保持 tape 活着。
    **不 mock 引擎**（仍用真 Orchestrator + 真 tape + 真 EventBus）—— 只是 workflow 自身慢。
    """
    p = tmp_path / "slow.yaml"
    p.write_text(
        """
name: slow_demo
description: 慢 script workflow（gate / cross-shell E2E 用，sleep 10s 保 tape 活）
entry: a
nodes:
  - name: a
    kind: script
    command: "sleep 10"
    routes:
      - to: $end
""",
        encoding="utf-8",
    )
    return p


async def fire_gate(harness: "InProcessHarness", run_id: str, gate_id: str) -> tuple[str, str]:
    """后台起 ``handler.request(gate)``，返回 ``await`` 它的 ``(answer, source)`` 元组。

    E2E-2 / E2E-3 共用（DRY）。模拟节点触发 gate（SPEC §D5.3）。**不**依赖 claude/hook——
    直接调真 ``HumanGateHandler.request``，写 tape（``human_decision_requested``）+ 暂停 await。
    """
    from orca.gates.types import HumanGate

    gate = HumanGate(
        id=gate_id,
        prompt="批准部署？",
        context={"env": "prod"},
        source="agent_ask",
        run_id=run_id,
        node="a",
        session_id=None,
        options=["yes", "no"],
    )
    handle = harness.manager.get_handle(run_id)
    assert handle is not None, f"start_run 后应能 get_handle({run_id})"
    return await handle.gate_handler.request(gate)


def parse_tool_result(result: Any) -> dict:
    """解析 ``ClientSession.call_tool`` 返回 → dict。

    FastMCP 把 dict 返回值序列化为 ``TextContent``（JSON 字符串），``structuredContent`` 为 None
    （实测 mcp SDK 1.27.2 行为）。本 helper 取 ``result.content[0].text`` 解析为 dict。

    fail loud：content 为空 / 非合法 JSON → raise（SPEC §6.3 硬 invariant 8）。
    """
    contents = getattr(result, "content", None) or []
    if not contents:
        raise AssertionError(f"tool result 无 content：{result!r}")
    text = getattr(contents[0], "text", None)
    if text is None:
        raise AssertionError(f"tool result content[0] 无 text：{contents[0]!r}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise AssertionError(f"tool result text 非合法 JSON：{text!r}") from e


async def poll_to_terminal(session, task_id: str, *, deadline_s: float, interval_s: float = 0.5) -> dict:
    """轮询 ``get_task_status`` 到终态（completed/failed/cancelled），返回终态 summary dict。

    E2E-1（script, 30s）/ E2E-4（claude, 120s）共用（DRY）。fail loud：超时 raise
    ``TimeoutError``，附 last status 便于诊断。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    last: dict = {}
    while loop.time() < deadline:
        result = await session.call_tool("get_task_status", {"task_id": task_id})
        last = parse_tool_result(result)
        if last.get("status") in ("completed", "failed", "cancelled"):
            return last
        await asyncio.sleep(interval_s)
    raise TimeoutError(
        f"task {task_id} 未在 {deadline_s}s 内到终态（last status={last.get('status')!r}）"
    )
