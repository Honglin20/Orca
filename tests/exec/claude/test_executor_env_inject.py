"""tests/exec/claude/test_executor_env_inject.py —— ClaudeExecutor spawn env 4 个 ORCA_* 注入（phase-13 §2）。

覆盖意图（非仅行为）：
  - mock subprocess.Popen → 断言 env overlay 含全部 4 个 ORCA_*
  - run_id / node / session_id / chart_sock 各自正确的值
  - runs_dir=None → 不注 ORCA_CHART_SOCK（向后兼容）
  - sock path 过长 → log warning + 不阻塞 run（不 raise）
  - opencode profile 同样工作（ClaudeExecutor 是 backend-agnostic，profile 切换覆盖）
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orca.exec.claude.executor import (
    ClaudeExecutor,
    _build_spawn_config,
    _resolve_chart_sock_path,
)
from orca.chart._limits import SOCK_PATH_MAX
from orca.exec.context import RunContext
from orca.profiles import get_profile
from orca.profiles.base import CliProfile
from orca.schema import AgentNode


# ── fixtures ─────────────────────────────────────────────────────────────────


def _make_profile(name: str = "claude") -> CliProfile:
    """获取 builtin profile（claude / opencode 等）。

    用 ``get_profile`` 复用 builtin 注册表（避免手搓 ProviderCapabilities 字段）。
    """
    return get_profile(name)


def _make_node() -> AgentNode:
    """最小 AgentNode（无 schema 校验严格路径，仅 spawn config 用）。"""
    return AgentNode(
        name="train",
        kind="agent",
        executor="claude",
        prompt="hello",
        routes=[],
    )


def _make_ctx(run_id: str = "demo-abc") -> RunContext:
    return RunContext(inputs={}, outputs={}, run_id=run_id)


# ── _build_spawn_config：env overlay 注入 ──────────────────────────────────


def test_spawn_config_env_overlay_has_all_four_orca_vars(monkeypatch):
    """chart_sock 非空 → env overlay 含全部 4 个 ORCA_*（run_id / node / session_id / chart_sock）。

    意图：SPEC §2.2 ClaudeExecutor spawn 时显式传 4 个 keyword → 子进程 env 必含全部 4 个
    ORCA_*（script 端 render_chart 据此路由 chart 事件）。
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")  # prefix 透传仍工作
    profile = _make_profile()
    node = _make_node()

    cfg = _build_spawn_config(
        node, profile, "hello", None,
        run_id="demo-1", session_id="sess-1",
        chart_sock="/tmp/orca-runs/demo-1.sock",
    )
    assert cfg.env_overlay["ORCA_RUN_ID"] == "demo-1"
    assert cfg.env_overlay["ORCA_NODE"] == "train"
    assert cfg.env_overlay["ORCA_SESSION_ID"] == "sess-1"
    assert cfg.env_overlay["ORCA_CHART_SOCK"] == "/tmp/orca-runs/demo-1.sock"
    # prefix 透传仍工作
    assert cfg.env_overlay["ANTHROPIC_API_KEY"] == "sk-test"


def test_spawn_config_env_overlay_no_chart_sock_when_empty(monkeypatch):
    """chart_sock="" → env overlay 不含 ORCA_CHART_SOCK（其余 3 个仍注）。

    意图：缺省空串 → 不注。这是 ClaudeExecutor.exec 内 _resolve_chart_sock_path 返回空
    时的退化路径（runs_dir 为 None 或 sock path 过长）。
    """
    profile = _make_profile()
    node = _make_node()

    cfg = _build_spawn_config(
        node, profile, "hello", None,
        run_id="demo-1", session_id="sess-1",
        chart_sock="",  # 空
    )
    assert cfg.env_overlay["ORCA_RUN_ID"] == "demo-1"
    assert cfg.env_overlay["ORCA_NODE"] == "train"
    assert cfg.env_overlay["ORCA_SESSION_ID"] == "sess-1"
    assert "ORCA_CHART_SOCK" not in cfg.env_overlay


def test_spawn_config_env_overlay_backward_compat_no_kwargs(monkeypatch):
    """旧调用方式 ``_build_spawn_config(..., run_id=..., session_id=...)`` 不传 chart_sock → 不注 ORCA_CHART_SOCK。

    意图：chart_sock 缺省空串 = 不注，与既有调用方（如旧测试 / 部分迁移路径）兼容。
    """
    profile = _make_profile()
    node = _make_node()
    cfg = _build_spawn_config(
        node, profile, "hello", None,
        run_id="demo-1", session_id="sess-1",
    )
    assert "ORCA_CHART_SOCK" not in cfg.env_overlay


# ── _resolve_chart_sock_path ────────────────────────────────────────────────


def test_resolve_chart_sock_path_returns_resolved(tmp_path):
    """正常路径 → 返回 resolved 绝对路径（用 /tmp 短路径避免 macOS tmp_path 太长）。"""
    import hashlib
    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:6]
    short_dir = Path(f"/tmp/orca-t{h}")
    short_dir.mkdir(parents=True, exist_ok=True)
    try:
        out = _resolve_chart_sock_path(short_dir, "demo-1")
        assert out == str((short_dir / "demo-1.sock").resolve())
    finally:
        import shutil
        shutil.rmtree(short_dir, ignore_errors=True)


def test_resolve_chart_sock_path_none_returns_empty():
    """runs_dir=None → 返回空串（不注 env，向后兼容）。"""
    assert _resolve_chart_sock_path(None, "demo-1") == ""


def test_resolve_chart_sock_path_too_long_logs_warning_returns_empty(tmp_path, caplog):
    """resolved path > SOCK_PATH_MAX → log warning + 返回空串（不 raise，避免阻塞 run）。

    意图：executor 路径只生成路径，RunManager 启动 ingestor 时已 fail loud。executor 二次
    发现过长 → 退化为不注 env，让 script 端 §7.1 fail loud（而非 executor 阻塞）。
    """
    # 构造极深路径
    deep = tmp_path / ("a" * 100)
    deep.mkdir(parents=True, exist_ok=True)
    with caplog.at_level("WARNING", logger="orca.exec.claude.executor"):
        out = _resolve_chart_sock_path(deep, "demo-1")
    assert out == ""
    # log warning 含 workaround 提示
    assert any("chart sock path 过长" in r.message or "ORCA_RUNS_DIR" in r.message for r in caplog.records)


# ── ClaudeExecutor.exec：env 注入端到端（mock subprocess）─────────────────


def test_executor_exec_passes_chart_sock_to_spawn_config(monkeypatch, tmp_path):
    """ClaudeExecutor.exec 内部算 chart_sock 并传给 _build_spawn_config → 子进程 env 含。

    意图：端到端验证 exec 流程：ClaudeExecutor.__init__(runs_dir=...) → exec(node, ctx)
    → _build_spawn_config 收到 chart_sock=resolved path → env_overlay 含 ORCA_CHART_SOCK。
    用 mock CLIRunner 跳过真子进程 spawn。用 /tmp 短路径避免 macOS tmp_path 太长触发
    SOCK_PATH_MAX check（让 chart_sock 真正注入）。
    """
    import hashlib, shutil
    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:6]
    short_dir = Path(f"/tmp/orca-t{h}")
    short_dir.mkdir(parents=True, exist_ok=True)

    try:
        profile = _make_profile()
        node = _make_node()
        ctx = _make_ctx(run_id="demo-xyz")
        executor = ClaudeExecutor(profile, None, runs_dir=short_dir)

        captured_cfgs = []

        class _FakeRunner:
            def __init__(self, cfg, on_result=None):
                captured_cfgs.append(cfg)
                self.stderr = ""
                self.exit_code = 0
                self.elapsed = 0.01
                self.timed_out = False
                self.was_interrupted = False

            async def stream(self):
                # 模拟一行 result，让 accumulator 走 happy path
                yield '{"type":"result","result":"{\\"answer\\": 42}","is_error":false}'

        async def go():
            with patch("orca.exec.claude.executor.CLIRunner", _FakeRunner):
                events = []
                async for ev in executor.exec(node, ctx):
                    events.append(ev)
                return events

        import asyncio
        events = asyncio.run(go())

        assert len(captured_cfgs) == 1
        cfg = captured_cfgs[0]
        # 4 个 ORCA_* 都注入（chart_sock 是 resolved path）
        assert cfg.env_overlay["ORCA_RUN_ID"] == "demo-xyz"
        assert cfg.env_overlay["ORCA_NODE"] == "train"
        assert "ORCA_SESSION_ID" in cfg.env_overlay  # uuid，不固定值
        expected_sock = str((short_dir / "demo-xyz.sock").resolve())
        assert cfg.env_overlay["ORCA_CHART_SOCK"] == expected_sock
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


def test_executor_exec_no_runs_dir_skips_chart_sock(monkeypatch):
    """runs_dir=None → exec 不注 ORCA_CHART_SOCK（向后兼容）。

    意图：旧 ClaudeExecutor(profile) 不传 runs_dir → 子进程 env 不含 chart 路由；
    script 端 render_chart 会因 ORCA_* 缺失 fail loud（SPEC §7.1）。
    """
    profile = _make_profile()
    node = _make_node()
    ctx = _make_ctx()
    executor = ClaudeExecutor(profile, None, runs_dir=None)

    captured_cfgs = []

    class _FakeRunner:
        def __init__(self, cfg, on_result=None):
            captured_cfgs.append(cfg)
            self.stderr = ""
            self.exit_code = 0
            self.elapsed = 0.01
            self.timed_out = False
            self.was_interrupted = False

        async def stream(self):
            yield '{"type":"result","result":"{\\"answer\\": 42}","is_error":false}'

    async def go():
        with patch("orca.exec.claude.executor.CLIRunner", _FakeRunner):
            async for _ in executor.exec(node, ctx):
                pass

    import asyncio
    asyncio.run(go())

    cfg = captured_cfgs[0]
    # run_id / node / session_id 仍注（exec 始终传），但 chart_sock 不注（runs_dir=None）
    assert "ORCA_RUN_ID" in cfg.env_overlay
    assert "ORCA_NODE" in cfg.env_overlay
    assert "ORCA_SESSION_ID" in cfg.env_overlay
    assert "ORCA_CHART_SOCK" not in cfg.env_overlay


# ── profile-agnostic（opencode profile 同样工作）────────────────────────────


def test_executor_env_inject_works_with_any_profile_name(monkeypatch, tmp_path):
    """opencode profile（或任何 profile）同样注入 ORCA_* env。

    意图：SPEC §2.1 executor-agnostic。ClaudeExecutor 是统一 executor，profile 切换
    backend（claude/ccr/opencode）不影响 env overlay 注入逻辑。用 /tmp 短路径避免
    macOS tmp_path 太长触发 SOCK_PATH_MAX check。
    """
    import hashlib, shutil
    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:6]
    short_dir = Path(f"/tmp/orca-t{h}")
    short_dir.mkdir(parents=True, exist_ok=True)

    try:
        profile = _make_profile(name="opencode")
        node = _make_node()
        ctx = _make_ctx(run_id="opencode-run")
        executor = ClaudeExecutor(profile, None, runs_dir=short_dir)

        captured_cfgs = []

        class _FakeRunner:
            def __init__(self, cfg, on_result=None):
                captured_cfgs.append(cfg)
                self.stderr = ""
                self.exit_code = 0
                self.elapsed = 0.01
                self.timed_out = False
                self.was_interrupted = False

            async def stream(self):
                yield '{"type":"result","result":"{\\"answer\\": 42}","is_error":false}'

        async def go():
            with patch("orca.exec.claude.executor.CLIRunner", _FakeRunner):
                async for _ in executor.exec(node, ctx):
                    pass

        import asyncio
        asyncio.run(go())

        cfg = captured_cfgs[0]
        assert cfg.env_overlay["ORCA_RUN_ID"] == "opencode-run"
        assert "ORCA_CHART_SOCK" in cfg.env_overlay
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)
