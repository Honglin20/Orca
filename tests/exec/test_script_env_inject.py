"""tests/exec/test_script_env_inject.py —— ScriptExecutor spawn env 4 个 ORCA_* 注入（phase-13 §11 #9）。

覆盖意图（非仅行为）—— 与 ``tests/exec/claude/test_executor_env_inject.py`` 对称：
  - 真 subprocess → script 子进程 env 含 4 个 ORCA_*（用 ``env`` 命令打印验证）
  - run_id / node / session_id / chart_sock 各自正确的值
  - runs_dir=None → 不注 ORCA_CHART_SOCK（其余 3 个仍注；向后兼容）
  - shell 路径（``echo`` / ``env`` builtin）与 python 子进程路径（``python -c "..."``）都验证
    （用户要求：script_kind=shell / python 两类都覆盖）
  - sock path 过长 → log warning + 不阻塞 run（不 raise，与 ClaudeExecutor 同语义）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from orca.chart._limits import SOCK_PATH_MAX
from orca.exec.context import RunContext
from orca.exec.script import (
    ScriptExecutor,
    _build_spawn_env,
    _resolve_chart_sock_path,
)
from orca.schema import Event, ScriptNode


# ── fixtures ─────────────────────────────────────────────────────────────────


def _ctx(run_id: str = "demo-abc") -> RunContext:
    return RunContext(inputs={}, outputs={}, run_id=run_id)


def _short_dir(tmp_path: Path) -> Path:
    """macOS tmp_path 通常 > SOCK_PATH_MAX；用一个 /tmp 短路径避免污染主断言。"""
    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:6]
    short = Path(f"/tmp/orca-script-{h}")
    short.mkdir(parents=True, exist_ok=True)
    return short


# ── _resolve_chart_sock_path（与 ClaudeExecutor 同语义）────────────────────


def test_resolve_chart_sock_path_returns_resolved(tmp_path):
    short = _short_dir(tmp_path)
    try:
        out = _resolve_chart_sock_path(short, "demo-1")
        assert out == str((short / "demo-1.sock").resolve())
    finally:
        shutil.rmtree(short, ignore_errors=True)


def test_resolve_chart_sock_path_none_returns_empty():
    """runs_dir=None → 返回空串（不注 env，向后兼容）。"""
    assert _resolve_chart_sock_path(None, "demo-1") == ""


def test_resolve_chart_sock_path_too_long_logs_warning_returns_empty(tmp_path, caplog):
    """resolved path > SOCK_PATH_MAX → log warning + 返回空串（不 raise，避免阻塞 run）。"""
    deep = tmp_path / ("a" * 100)
    deep.mkdir(parents=True, exist_ok=True)
    with caplog.at_level("WARNING", logger="orca.exec.script"):
        out = _resolve_chart_sock_path(deep, "demo-1")
    assert out == ""
    assert any(
        "chart sock path 过长" in r.message or "ORCA_RUNS_DIR" in r.message
        for r in caplog.records
    )


# ── _build_spawn_env（单测 overlay 构造）────────────────────────────────────


def test_build_spawn_env_has_all_four_orca_vars(monkeypatch):
    """4 件套全传 → env overlay 含全部 4 个 ORCA_*（合并自 os.environ）。"""
    env = _build_spawn_env("train", "demo-1", "sess-1", "/tmp/orca-runs/demo-1.sock")
    assert env["ORCA_RUN_ID"] == "demo-1"
    assert env["ORCA_NODE"] == "train"
    assert env["ORCA_SESSION_ID"] == "sess-1"
    assert env["ORCA_CHART_SOCK"] == "/tmp/orca-runs/demo-1.sock"
    # os.environ 仍存在（PATH 等）
    assert "PATH" in env


def test_build_spawn_env_no_chart_sock_when_empty():
    """chart_sock="" → 仍注其余 3 个（不注 ORCA_CHART_SOCK）。"""
    env = _build_spawn_env("train", "demo-1", "sess-1", "")
    assert env["ORCA_RUN_ID"] == "demo-1"
    assert env["ORCA_NODE"] == "train"
    assert env["ORCA_SESSION_ID"] == "sess-1"
    assert "ORCA_CHART_SOCK" not in env


# ── ScriptExecutor.exec 真子进程 env 注入端到端 ──────────────────────────────


def _run(coro):
    return asyncio.run(coro)


async def _collect(node, ctx, *, runs_dir=None) -> list[Event]:
    exe = ScriptExecutor(runs_dir=runs_dir)
    return [ev async for ev in exe.exec(node, ctx)]


def test_script_executor_passes_chart_env_to_subprocess_shell(tmp_path):
    """shell 路径：ScriptExecutor spawn 的子进程 env 含 4 个 ORCA_*。

    意图：用真 ``env`` 命令把子进程 env 打到 stdout，断言 4 件套注入。
    这是 phase-13 §11 #9 executor-agnostic 契约的核心断言（与 ClaudeExecutor 对称）。
    """
    short = _short_dir(tmp_path)
    try:
        node = ScriptNode(name="s", command="env")
        ctx = _ctx(run_id="demo-xyz")
        events = _run(_collect(node, ctx, runs_dir=short))

        completed = [e for e in events if e.type == "node_completed"][0]
        stdout = completed.data["output"]["stdout"]
        expected_sock = str((short / "demo-xyz.sock").resolve())

        # 4 个 ORCA_* 都在子进程 env 里
        assert f"ORCA_RUN_ID=demo-xyz" in stdout
        assert f"ORCA_NODE=s" in stdout
        assert f"ORCA_SESSION_ID=" in stdout  # uuid，存在即可
        assert f"ORCA_CHART_SOCK={expected_sock}" in stdout
    finally:
        shutil.rmtree(short, ignore_errors=True)


def test_script_executor_passes_chart_env_to_python_subprocess(tmp_path):
    """python 路径：python 子进程经 os.environ 看到 4 个 ORCA_*。

    用户重点：script_kind=python 路径覆盖（``python -c "..."`` 调 ``orca.chart.render_chart``
    时从 env 读身份）。这里仅验证 env 注入，真实 render_chart 在 E2E 覆盖。
    """
    short = _short_dir(tmp_path)
    try:
        # python -c 打印 4 个 env 变量
        py_cmd = (
            "import os, sys; "
            "sys.stdout.write('RUN_ID=' + os.environ.get('ORCA_RUN_ID','') + '\\n'); "
            "sys.stdout.write('NODE=' + os.environ.get('ORCA_NODE','') + '\\n'); "
            "sys.stdout.write('SID=' + os.environ.get('ORCA_SESSION_ID','') + '\\n'); "
            "sys.stdout.write('SOCK=' + os.environ.get('ORCA_CHART_SOCK','') + '\\n')"
        )
        node = ScriptNode(name="pyworker", command=f"python3 -c \"{py_cmd}\"")
        ctx = _ctx(run_id="py-run-1")
        events = _run(_collect(node, ctx, runs_dir=short))

        completed = [e for e in events if e.type == "node_completed"][0]
        stdout = completed.data["output"]["stdout"]
        expected_sock = str((short / "py-run-1.sock").resolve())

        assert "RUN_ID=py-run-1" in stdout
        assert "NODE=pyworker" in stdout
        assert "SID=" in stdout and len(completed.data["output"]["stdout"].split("SID=")[1].split()[0]) == 32
        assert f"SOCK={expected_sock}" in stdout
    finally:
        shutil.rmtree(short, ignore_errors=True)


def test_script_executor_no_runs_dir_skips_chart_sock():
    """runs_dir=None → 子进程 env 不含 ORCA_CHART_SOCK（其余 3 个仍注，向后兼容）。

    意图：旧 ``ScriptExecutor()`` 不传 runs_dir → 子进程 env 不含 chart 路由；
    script 端 render_chart 会因 ORCA_CHART_SOCK 缺失 fail loud（SPEC §7.1）。
    """
    node = ScriptNode(name="s", command="env")
    ctx = _ctx(run_id="demo-no-runs")
    events = _run(_collect(node, ctx, runs_dir=None))

    completed = [e for e in events if e.type == "node_completed"][0]
    stdout = completed.data["output"]["stdout"]
    # run_id / node / session_id 仍注（exec 始终传）
    assert "ORCA_RUN_ID=demo-no-runs" in stdout
    assert "ORCA_NODE=s" in stdout
    assert "ORCA_SESSION_ID=" in stdout
    # chart_sock 不注
    assert "ORCA_CHART_SOCK=" not in stdout


def test_script_executor_long_sock_path_logs_warning_and_skips(tmp_path, caplog):
    """runs_dir 深 → resolved path 过长 → log warning + 不注 chart_sock，run 仍成功（不阻塞）。

    意图：与 ClaudeExecutor 同语义（SPEC §7.7），executor 路径不 raise。
    """
    deep = tmp_path / ("a" * 100)
    deep.mkdir(parents=True, exist_ok=True)
    # 用 env 命令验证 ORCA_CHART_SOCK 不在子进程 env 里
    node = ScriptNode(name="s", command="env")
    ctx = _ctx(run_id="deep-run")
    with caplog.at_level("WARNING", logger="orca.exec.script"):
        events = _run(_collect(node, ctx, runs_dir=deep))

    completed = [e for e in events if e.type == "node_completed"][0]
    stdout = completed.data["output"]["stdout"]
    # run_id / node 仍注；chart_sock 不注（路径过长退化）
    assert "ORCA_RUN_ID=deep-run" in stdout
    assert "ORCA_NODE=s" in stdout
    assert "ORCA_CHART_SOCK=" not in stdout
    assert any(
        "chart sock path 过长" in r.message or "ORCA_RUNS_DIR" in r.message
        for r in caplog.records
    )


# ── 既有 ScriptExecutor 行为零回归（构造方式 backward compat）────────────────


def test_script_executor_zero_args_backward_compat():
    """ScriptExecutor() 不传任何参 → 既有行为（runs_dir=None，不注 chart_sock）。

    既有测试 ``tests/exec/test_script.py::_collect`` 用 ``ScriptExecutor()`` 调用，
    patch 后必须保持兼容（run_id/node/session_id 注但不阻塞既有断言）。
    """
    exe = ScriptExecutor()
    assert exe._runs_dir is None
    # 跑一个无害命令验证仍工作
    node = ScriptNode(name="s", command="echo ok")
    events = _run(_collect(node, _ctx()))
    assert events[-1].type == "node_completed"
    assert events[-1].data["output"]["stdout"].strip() == "ok"
