"""tests/exec/test_runner.py —— CLIRunner（假子进程，不 spawn claude，SPEC §7.5 / 计划 B.4）。

覆盖：
  - 多行 stdout readline
  - stdin pump（投递 prompt 被子进程读到）
  - 超时 → timed_out=True，SIGTERM→SIGKILL 收尾
  - on_result 回调（result 行触发；非 JSON 行跳过）
  - 非零退出码 + stderr 累积
  - elapsed > 0
  - shlex 拆分多 token cli_path

约定（同 tests/events/test_bus.py）：本仓库不用 pytest-asyncio，异步统一 ``asyncio.run``。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import pytest

from orca.exec.runner import CLIRunner, SpawnConfig

# 测试用 interpreter：直接 sys.executable -c "..."，不依赖外部 binary。
PY = sys.executable


def _run(coro):
    """统一异步入口（同 tests/events/test_bus.py 的约定）。"""
    return asyncio.run(coro)


def _cfg(argv_extra: list[str] | None = None, *, timeout=None, prompt="") -> SpawnConfig:
    return SpawnConfig(
        cli_path=PY,
        flags=["-c"],
        extra_args=argv_extra or [],
        prompt=prompt,
        prompt_channel="stdin",
        timeout=timeout,
    )


async def _collect(runner: CLIRunner) -> list[str]:
    return [ln async for ln in runner.stream()]


# ── 多行 stdout ──────────────────────────────────────────────────────────────


def test_stream_yields_each_stdout_line():
    code = "print('line1'); print('line2'); print('line3')"
    runner = CLIRunner(_cfg([code]))
    lines = _run(_collect(runner))
    assert lines == ["line1", "line2", "line3"]
    assert runner.exit_code == 0
    assert not runner.timed_out


def test_stream_skips_blank_lines():
    code = "print('a'); print(); print('b')"  # 中间空行
    runner = CLIRunner(_cfg([code]))
    lines = _run(_collect(runner))
    assert lines == ["a", "b"]


# ── stdin pump ───────────────────────────────────────────────────────────────


def test_stdin_pump_delivers_prompt():
    """投递的 prompt 被子进程从 stdin 读到并回显（SPEC §2.2 stdin pump）。"""
    code = "import sys; data=sys.stdin.read(); print('GOT:' + data)"
    runner = CLIRunner(_cfg([code], prompt="hello-stdin"))
    lines = _run(_collect(runner))
    assert lines == ["GOT:hello-stdin"]


# ── 超时 ────────────────────────────────────────────────────────────────────


def test_timeout_sets_timed_out_and_kills():
    """超时 → timed_out=True，子进程被杀（exit_code 非 0，SPEC §2.5 / §7.5）。"""
    code = "import time; print('start', flush=True); time.sleep(30); print('end')"
    runner = CLIRunner(_cfg([code], timeout=0.5))
    lines = _run(_collect(runner))
    # 流里至少有 'start'（readline 在超时前已读到），之后超时中断
    assert "start" in lines
    assert runner.timed_out is True
    assert runner.exit_code != 0  # 被 SIGTERM/SIGKILL


# ── on_result 回调 ───────────────────────────────────────────────────────────


def test_on_result_fires_for_result_line():
    """result 行（json type=result）触发 on_result(raw, usage, cost, is_error)（SPEC §4.4 关键约束）。"""
    result_obj = {
        "type": "result",
        "subtype": "success",
        "result": "DONE",
        "total_cost_usd": 0.17,
        "usage": {"input_tokens": 100, "output_tokens": 5, "cache_read_input_tokens": 50},
        "is_error": False,
    }
    # 用 Python 字面量构造对象再 json.dumps（避免双重 json.dumps 产生 JS 小写 bool 嵌进 Python 源）。
    code = (
        "import json; print(json.dumps("
        "{'type':'result','subtype':'success','result':'DONE',"
        "'total_cost_usd':0.17,'is_error':False,"
        "'usage':{'input_tokens':100,'output_tokens':5,'cache_read_input_tokens':50}}))"
    )
    captured: dict = {}
    runner = CLIRunner(
        _cfg([code]),
        on_result=lambda r, u, c, e, s: captured.update(
            raw=r, usage=u, cost=c, is_error=e, api_error_status=s,
        ),
    )
    _run(_collect(runner))
    assert captured["raw"] == "DONE"
    assert captured["usage"]["input_tokens"] == 100
    assert captured["cost"] == pytest.approx(0.17)
    assert captured["is_error"] is False
    # success result 行无 api_error_status → 透传 None（Bug1：回调始终收 5 参）。
    assert captured["api_error_status"] is None


def test_non_json_line_skipped_silently():
    """非 JSON 心跳行：debug log + 跳过，不抛、不触发 on_result（SPEC §6 json_decode 例外）。

    注意：行本身仍被 yield（translator 也看得到）；只是 CLIRunner 不把它当 result。
    """
    code = "print('not-json-heartbeat'); print('{\"type\":\"result\",\"result\":\"OK\"}')"
    captured: list = []
    runner = CLIRunner(
        _cfg([code]),
        on_result=lambda r, u, c, e, s: captured.append(r),
    )
    lines = _run(_collect(runner))
    assert "not-json-heartbeat" in lines
    assert captured == ["OK"]  # 只有 result 行触发回调


def test_non_result_json_line_does_not_fire_on_result():
    """JSON 行但 type != result 不触发 on_result（如 assistant/stream_event 行）。"""
    code = 'import json; print(json.dumps({"type":"assistant","message":{"content":[]}}))'
    captured: list = []
    runner = CLIRunner(
        _cfg([code]),
        on_result=lambda r, u, c, e, s: captured.append(r),
    )
    _run(_collect(runner))
    assert captured == []


# ── 非零退出码 + stderr ──────────────────────────────────────────────────────


def test_nonzero_exit_code_recorded():
    code = "import sys; print('before'); sys.stderr.write('boom\\n'); sys.exit(2)"
    runner = CLIRunner(_cfg([code]))
    lines = _run(_collect(runner))
    assert lines == ["before"]
    assert runner.exit_code == 2
    assert not runner.timed_out
    assert "boom" in runner.stderr


def test_stderr_accumulated_for_diagnostics():
    code = "import sys; sys.stderr.write('err line1\\n'); sys.stderr.write('err line2\\n')"
    runner = CLIRunner(_cfg([code]))
    _run(_collect(runner))
    assert "err line1" in runner.stderr
    assert "err line2" in runner.stderr


# ── elapsed ──────────────────────────────────────────────────────────────────


def test_elapsed_positive():
    runner = CLIRunner(_cfg(["print('x')"]))
    _run(_collect(runner))
    assert runner.elapsed > 0


# ── shlex 拆分多 token cli_path ───────────────────────────────────────────────


def test_cli_path_multiple_tokens_shlex_split():
    """cli_path 含空格（如 'ccr code'）经 shlex.split 拆 argv（SPEC §2.2）。

    用 sys.executable 拼一个带引号的单 token 模拟，验证 shlex 拆引号逻辑。
    """
    cfg = SpawnConfig(
        cli_path=f'"{PY}"',  # 带引号的单 token
        flags=["-c"],
        extra_args=["print('ok')"],
        prompt_channel="stdin",
    )
    runner = CLIRunner(cfg)
    argv = runner._build_argv()
    # shlex 拆掉引号 → [PY, -c, print('ok')]
    assert argv[0] == PY
    lines = _run(_collect(runner))
    assert lines == ["ok"]


# ── api_error_status 透传（Bug1：HTTP 错误码落到 on_result）──────────────────


def test_on_result_passes_api_error_status():
    """Bug1：result 行的 api_error_status（HTTP 码，如 529）透传给 on_result 第 5 参。

    claude 把 API 错误写在 stdout 的 result 行顶层（不在 stderr），runner 必须把它带出，
    否则 executor 的 node_failed 完全看不到失败原因（典型 529 早退 stderr 空）。
    """
    code = (
        "import json; print(json.dumps("
        "{'type':'result','subtype':'success','result':'API Error: 529 overloaded',"
        "'is_error':True,'api_error_status':529,'usage':{}}))"
    )
    captured: dict = {}
    runner = CLIRunner(
        _cfg([code]),
        on_result=lambda r, u, c, e, s: captured.update(
            raw=r, is_error=e, api_error_status=s,
        ),
    )
    _run(_collect(runner))
    assert captured["is_error"] is True
    assert captured["api_error_status"] == 529


# ── cancel 时 terminate proc（Bug4：防孤儿子进程）─────────────────────────────


def test_stream_terminates_orphan_proc_on_cancel(tmp_path):
    """Bug4：stream 被外部 cancel 时若 proc 仍存活，finally 必须 terminate（防孤儿 claude）。

    asyncio 子进程不随父 task cancel 自动死；若 finally 不 terminate，强退后会留孤儿进程
    继续跑（如 529 重试）烧 API quota。
    """
    pid_file = tmp_path / "child.pid"
    code = (
        "import os, time; "
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(30)"  # 模拟 claude 在跑（长任务），不自行退出
    )
    runner = CLIRunner(_cfg([code]))

    async def run_then_cancel():
        task = asyncio.create_task(_collect(runner))
        # 等子进程写 PID 文件（确保已 spawn 且存活）
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    _run(run_then_cancel())
    assert pid_file.exists(), "子进程未启动（PID 文件没写）"
    pid = int(pid_file.read_text().strip())

    # finally 应已 terminate proc；轮询确认子进程死（含 _TERMINATE_GRACE_SECONDS 余量）。
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return  # 子进程已死 ✓
        time.sleep(0.1)
    pytest.fail(f"子进程 PID={pid} 在 cancel 后仍存活（Bug4 未修：孤儿子进程）")
