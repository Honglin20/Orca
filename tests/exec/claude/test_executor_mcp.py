"""tests/exec/claude/test_executor_mcp.py —— ClaudeExecutor 的 --mcp-config 注入（phase 11 §5.4）。

INTENT：
  - 注入 agent_tools_server → spawn argv 含 ``--mcp-config <path>`` + ``--allowed-tools`` 含
    ``mcp__orca-agent-tools__ask_user``（spike 验证：claude -p 默认不给 MCP 工具授权）。
  - 不注入（None）→ argv 不含 ``--mcp-config``，tools 保持既有行为（向后兼容）。
  - register_session 在 spawn 前被调（phase 6 register debt，review B2）。
  - prompt 末尾拼 ask_user 路由 instruction（含 orca_run_id / orca_node 实际值）。

策略：mock CLIRunner 捕获 SpawnConfig（不 spawn claude）。AgentToolsMcpServer 用真实实例
（start SSE server，write_config 写 tmp_path）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from orca.exec.claude.executor import ClaudeExecutor, _ASK_USER_TOOL_NAME
from orca.exec.context import RunContext
from orca.exec.mcp_tools.server import AgentToolsMcpServer
from orca.gates.context_registry import SessionContextRegistry
from orca.gates.handler import HumanGateHandler
from orca.profiles import get_profile
from orca.schema import AgentNode, Event

# ── 共享 helper（与 test_executor.py 同构，tests 非包故就地复制）─────────────────


# 完整 result 行（agent 产出 "ok"）。executor 据此走 node_completed 路径。
_RESULT_LINE = json.dumps({"type": "result", "result": "ok", "usage": {}, "total_cost_usd": 0.0})


def run_async(coro):
    return asyncio.run(coro)


class _CaptureRunner:
    """CLIRunner 替身：捕获 SpawnConfig + 按预设行 yield（不 spawn 子进程）。"""

    def __init__(self, lines=None, *, exit_code: int = 0, elapsed: float = 0.1) -> None:
        self._lines = list(lines) if lines is not None else []
        self._on_result = None
        self.captured_cfg = None
        self.exit_code = exit_code
        self.timed_out = False
        self.elapsed = elapsed
        self.stderr = ""
        self.was_interrupted = False

    async def stream(self) -> AsyncIterator[str]:
        for line in self._lines:
            self._maybe_fire_on_result(line)
            yield line

    def _maybe_fire_on_result(self, line: str) -> None:
        if self._on_result is None:
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        if isinstance(obj, dict) and obj.get("type") == "result":
            self._on_result(
                obj.get("result", ""), obj.get("usage") or {},
                obj.get("total_cost_usd") or 0.0, bool(obj.get("is_error", False)),
                obj.get("api_error_status"),
            )


def _patch_capture(monkeypatch, lines=None):
    """把 ClaudeExecutor 的 CLIRunner 替换成 _CaptureRunner，返回实例（.captured_cfg 取 argv）。

    默认喂一行成功 result（``"ok"``），让 executor 走 node_completed 路径。
    """
    fake = _CaptureRunner(lines=lines if lines is not None else [_RESULT_LINE])

    def factory(cfg=None, on_result=None):
        fake.captured_cfg = cfg
        fake._on_result = on_result
        return fake

    monkeypatch.setattr("orca.exec.claude.executor.CLIRunner", factory)
    return fake


def _make_server(tmp_path) -> tuple[HumanGateHandler, AgentToolsMcpServer]:
    """构造 (handler, server)——handler 不需真 bus（这些测试不验 gate 事件）。"""
    from orca.events.bus import EventBus
    from orca.events.tape import Tape

    tape = Tape(tmp_path / "ev.jsonl", run_id="r1")
    bus = EventBus(tape)
    handler = HumanGateHandler(bus)
    server = AgentToolsMcpServer(handler, SessionContextRegistry(), runs_dir=tmp_path)
    return handler, server


async def _collect(node, executor, ctx) -> list[Event]:
    return [ev async for ev in executor.exec(node, ctx)]


# ── mcp-config flag 注入 ─────────────────────────────────────────────────────


def test_claude_executor_passes_mcp_config_flag_when_server_present(tmp_path, monkeypatch):
    """注入 agent_tools_server → argv 含 ``--mcp-config <path>`` + allowed-tools 含 ask_user。"""
    capture = _patch_capture(monkeypatch)
    handler, server = _make_server(tmp_path)

    async def scenario():
        await server.start()
        try:
            profile = get_profile("claude")
            executor = ClaudeExecutor(profile, agent_tools_server=server)
            node = AgentNode(name="agent_x", prompt="do it")
            ctx = RunContext(inputs={}, outputs={}, run_id="run-mcp-1")
            events = await _collect(node, executor, ctx)
            # 成功产出 node_completed
            assert any(e.type == "node_completed" for e in events)
            # argv 含 --mcp-config <path>
            cfg = capture.captured_cfg
            assert "--mcp-config" in cfg.mcp_flag_args
            idx = cfg.mcp_flag_args.index("--mcp-config")
            config_path = Path(cfg.mcp_flag_args[idx + 1])
            assert config_path.exists()
            # config 文件含 SSE url 指向 server port
            config = json.loads(config_path.read_text())
            assert config["mcpServers"]["orca-agent-tools"]["type"] == "sse"
            # allowed-tools 含 ask_user（spike 验证：claude -p 默认不给 MCP 工具授权）
            assert "--allowed-tools" in cfg.extra_args
            tools_idx = cfg.extra_args.index("--allowed-tools")
            assert _ASK_USER_TOOL_NAME in cfg.extra_args[tools_idx + 1]
        finally:
            await server.stop()

    run_async(scenario())


def test_claude_executor_no_mcp_config_when_server_absent(tmp_path, monkeypatch):
    """agent_tools_server=None → argv 不含 --mcp-config（向后兼容，既有行为）。"""
    capture = _patch_capture(monkeypatch)
    profile = get_profile("claude")
    executor = ClaudeExecutor(profile, agent_tools_server=None)
    node = AgentNode(name="agent_y", prompt="do it", tools=["Bash", "Read"])
    ctx = RunContext(inputs={}, outputs={}, run_id="r2")

    events = run_async(_collect(node, executor, ctx))
    assert any(e.type == "node_completed" for e in events)
    cfg = capture.captured_cfg
    assert cfg.mcp_flag_args == []
    # tools 保持既有行为（声明白名单，无 ask_user 注入）
    assert "--allowed-tools" in cfg.extra_args
    tools_idx = cfg.extra_args.index("--allowed-tools")
    assert cfg.extra_args[tools_idx + 1] == "Bash Read"
    assert _ASK_USER_TOOL_NAME not in cfg.extra_args[tools_idx + 1]
    # 负向断言：server=None 时 prompt 不含 ask_user instruction（向后兼容的 prompt 不被污染）
    rendered = next(e for e in events if e.type == "prompt_rendered")
    assert "[Orca ask_user tool]" not in rendered.data["preview"]
    assert "orca_run_id" not in rendered.data["preview"]


def test_claude_executor_appends_ask_user_to_declared_tools(tmp_path, monkeypatch):
    """node.tools 声明白名单 + 注入 server → ask_user 被 append 进 allowed-tools（SPEC §11.3）。

    INTENT：用户声明 tools=["Bash"] 时若不 append ask_user，白名单会屏蔽 ask_user（claude
    拒调未授权工具）。验证 argv 含 "Bash mcp__orca-agent-tools__ask_user"。
    """
    capture = _patch_capture(monkeypatch)
    handler, server = _make_server(tmp_path)

    async def scenario():
        await server.start()
        try:
            profile = get_profile("claude")
            executor = ClaudeExecutor(profile, agent_tools_server=server)
            node = AgentNode(name="whitelist", prompt="do it", tools=["Bash"])
            ctx = RunContext(inputs={}, outputs={}, run_id="run-wl-1")
            await _collect(node, executor, ctx)
            cfg = capture.captured_cfg
            assert "--allowed-tools" in cfg.extra_args
            tools_idx = cfg.extra_args.index("--allowed-tools")
            tools_str = cfg.extra_args[tools_idx + 1]
            assert "Bash" in tools_str
            assert _ASK_USER_TOOL_NAME in tools_str
            # 顺序：用户声明在前，ask_user append 在后
            assert tools_str.index("Bash") < tools_str.index(_ASK_USER_TOOL_NAME)
        finally:
            await server.stop()

    run_async(scenario())


def test_build_spawn_config_raises_when_run_id_or_session_id_empty(tmp_path, monkeypatch):
    """注入 server 但 run_id/session_id 空 → RuntimeError（fail loud，防写不出 mcp-config）。

    INTENT：编程错误守卫（Rule 12）。ClaudeExecutor.exec 总是带 run_id/session_id，此分支
    纯防调用约定被破坏。直接单测 helper。
    """
    from orca.exec.claude.executor import _build_spawn_config

    handler, server = _make_server(tmp_path)
    profile = get_profile("claude")
    node = AgentNode(name="guard", prompt="x")

    async def scenario():
        await server.start()
        try:
            # run_id 空
            with pytest.raises(RuntimeError, match="run_id/session_id 为空"):
                _build_spawn_config(node, profile, "prompt", server, run_id="", session_id="s1")
            # session_id 空
            with pytest.raises(RuntimeError, match="run_id/session_id 为空"):
                _build_spawn_config(node, profile, "prompt", server, run_id="r1", session_id="")
        finally:
            await server.stop()

    run_async(scenario())


def test_claude_executor_registers_session_when_server_present(tmp_path, monkeypatch):
    """注入 server → spawn 前 register_session 被调（phase 6 register debt，review B2）。

    INTENT：HumanGateHandler 的 gate 答案回流依赖 session_id → (run_id, node) 映射。
    ClaudeExecutor spawn 成功拿到 claude session_id 后必须 register，否则 ask_user 闭环断。
    """
    _patch_capture(monkeypatch)
    handler, server = _make_server(tmp_path)
    # 用 server 自带的 registry（与 _make_server 一致）

    async def scenario():
        await server.start()
        try:
            profile = get_profile("claude")
            executor = ClaudeExecutor(profile, agent_tools_server=server)
            node = AgentNode(name="agent_z", prompt="do it")
            ctx = RunContext(inputs={}, outputs={}, run_id="run-reg-1")
            await _collect(node, executor, ctx)
            # spawn 后 registry 含一条登记（session_id 由 executor 内部 uuid 生成，
            # run_id/node 是确定性的）。
            entries = [
                (sid, loc) for sid, loc in server.registry._map.items()
                if loc.run_id == "run-reg-1" and loc.node == "agent_z"
            ]
            assert len(entries) == 1, f"register_session 未登记或登记多次：{entries}"
        finally:
            await server.stop()

    run_async(scenario())


def test_claude_executor_appends_ask_user_instruction_when_server_present(tmp_path, monkeypatch):
    """注入 server → prompt_rendered.preview 含 ask_user 路由 instruction（决策 D4）。

    INTENT：确定性路由靠 claude 主动填 orca_run_id/orca_node。instruction 把具体 run_id /
    node 值填进去，降低 claude 省略路由参的概率。
    """
    _patch_capture(monkeypatch)
    handler, server = _make_server(tmp_path)

    async def scenario():
        await server.start()
        try:
            profile = get_profile("claude")
            executor = ClaudeExecutor(profile, agent_tools_server=server)
            node = AgentNode(name="asker", prompt="base task")
            ctx = RunContext(inputs={}, outputs={}, run_id="run-instr-1")
            events = await _collect(node, executor, ctx)
            rendered = next(e for e in events if e.type == "prompt_rendered")
            preview = rendered.data["preview"]
            # preview 是 prompt[-200:]，可能截掉 [Orca ask_user tool] 头，但 routing 部分
            # （含具体 run_id / node 值 + 参数名）必在末尾。断言 load-bearing 部分：
            # claude 看到这些就能正确填路由参。
            assert "orca_run_id" in preview
            assert "orca_node" in preview
            assert "run-instr-1" in preview
            assert "asker" in preview
            # call signature 段含参数名 + 实际值（确定性路由的落地点）
            assert "ask_user(prompt=" in preview
        finally:
            await server.stop()

    run_async(scenario())


# ── §11.3 allowed-tools 边界：空 tools list + 去重 ────────────────────────────


def test_build_spawn_config_appends_ask_user_when_tools_is_empty_list(tmp_path):
    """node.tools=[]（空白名单，非 None）+ 注入 server → allowed-tools 含 ask_user。

    INTENT（§11.3 边界）：``node.tools`` 有三种状态——None（全开）/ []（声明但空，
    实际等于「无工具」）/ [...](白名单)。注入 server 时三种都须让 ask_user 可用：
      - None → 仅声明 ask_user（既有测试覆盖）
      - [...] → append ask_user（既有测试覆盖）
      - [] → append ask_user（**本测试**）——空 list 是「用户显式声明了白名单结构但没列
        任何工具」，与 None（全开）语义不同；注入 server 时应让 ask_user 可用，否则
        agent 在「全空 tools + ask_user 挂载」配置下无法问用户。
    """
    from orca.exec.claude.executor import _build_spawn_config

    handler, server = _make_server(tmp_path)
    profile = get_profile("claude")
    node = AgentNode(name="empty_tools", prompt="x", tools=[])

    async def scenario():
        await server.start()
        try:
            cfg = _build_spawn_config(
                node, profile, "prompt", server, run_id="r-et", session_id="s-et",
            )
            assert "--allowed-tools" in cfg.extra_args
            tools_idx = cfg.extra_args.index("--allowed-tools")
            tools_str = cfg.extra_args[tools_idx + 1]
            assert _ASK_USER_TOOL_NAME in tools_str, (
                f"tools=[] + server 注入应让 ask_user 可用，实际 allowed-tools={tools_str!r}"
            )
        finally:
            await server.stop()

    run_async(scenario())


def test_build_spawn_config_does_not_duplicate_ask_user_if_already_in_tools(tmp_path):
    """node.tools 已含 ask_user + 注入 server → allowed-tools 不重复（去重不变量）。

    INTENT（§11.3 去重）：用户在 yaml 里手动声明了 ``mcp__orca-agent-tools__ask_user``
    （如想显式控制顺序），注入 server 时实现走 ``if X not in tools_list`` 分支不重复
    append。重复会让 claude ``--allowed-tools`` 解析出错或行为未定义。回归保护。
    """
    from orca.exec.claude.executor import _build_spawn_config

    handler, server = _make_server(tmp_path)
    profile = get_profile("claude")
    node = AgentNode(
        name="dedup", prompt="x", tools=["Bash", _ASK_USER_TOOL_NAME],
    )

    async def scenario():
        await server.start()
        try:
            cfg = _build_spawn_config(
                node, profile, "prompt", server, run_id="r-dd", session_id="s-dd",
            )
            tools_idx = cfg.extra_args.index("--allowed-tools")
            tools_str = cfg.extra_args[tools_idx + 1]
            # ask_user 恰好出现一次（去重生效）
            assert tools_str.count(_ASK_USER_TOOL_NAME) == 1, (
                f"ask_user 应去重（恰好 1 次），实际 allowed-tools={tools_str!r}"
            )
            # Bash 保留
            assert "Bash" in tools_str
        finally:
            await server.stop()

    run_async(scenario())
