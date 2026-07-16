"""tests/iface/in_session/test_chart_daemon.py —— in-session chart 守护模块单测。

覆盖意图（非仅行为）：
  - ``_FlockSafeTape``：跨进程正确性（disk 刷新 + flock 互斥 + seq 单调）
  - ``_watch_terminal``：终态事件触发 / TTL 兜底 / 增量读不丢事件
  - 模块入口 ``main``：argv 解析 + 退出清理（socket unlink）
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import fcntl
import pytest

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.iface.in_session.chart_daemon import (
    _FlockSafeTape,
    _WATCH_POLL_SECONDS,
    _watch_terminal,
)


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tape_path(tmp_path: Path) -> Path:
    return tmp_path / "run.jsonl"


@pytest.fixture
def flock_path(tape_path: Path) -> Path:
    """与 cli._flock_path 同规则：tape 同目录加 ``.lock`` 后缀。"""
    return Path(str(tape_path) + ".lock")


# ── _FlockSafeTape ───────────────────────────────────────────────────────────


def _append_event(tape: Tape, etype: str, seq: int) -> None:
    """直接 append 一行到 tape 文件（绕过 Tape.append，模拟另一进程的写入）。"""
    with open(tape.path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"seq": seq, "type": etype, "timestamp": 0.0,
                            "node": None, "session_id": None, "data": {}}) + "\n")


def test_flock_safe_tape_picks_seq_from_disk_max(tape_path, flock_path):
    """_FlockSafeTape.append 前先从 disk 读 max seq → 分配的 seq = disk_max + 1。

    意图：守护持 in-memory _last_seq=0（resume 空文件）启动，但另一进程已写 5 行；
    守护第一次 append 必须分配 seq=6（而非 1），否则 seq 冲突。
    """
    # 模拟另一进程已写 5 行
    for i in range(1, 6):
        _append_event(Tape(tape_path, run_id="r"), "agent_message", i)

    safe = _FlockSafeTape(tape_path, "r", flock_path=flock_path)
    seq = asyncio.run(safe.append({"type": "custom", "data": {"kind": "chart"}}))
    assert seq == 6, f"应续 disk max(5) + 1 = 6；got {seq}"
    safe.close()


def test_flock_safe_tape_refreshes_between_appends(tape_path, flock_path):
    """两次 append 之间另一进程写入 → 第二次 append 反映 disk 新 max。

    意图：守护两次 chart emit 之间，CLI 的 orca next append 了 node_completed+rt+ns；
    守护第二次 emit 必须从新 disk max 续，不能沿用第一次后的 in-memory _last_seq。
    """
    safe = _FlockSafeTape(tape_path, "r", flock_path=flock_path)
    # 守护首 emit（空 tape → seq=1）
    s1 = asyncio.run(safe.append({"type": "custom", "data": {"i": 1}}))
    assert s1 == 1

    # 另一进程写入 3 行（模拟 orca next 的 emit_batch）
    for i in range(2, 5):
        _append_event(Tape(tape_path, run_id="r"), "node_completed", i)

    # 守护第二次 emit：disk max=4 → 应分配 seq=5
    s2 = asyncio.run(safe.append({"type": "custom", "data": {"i": 2}}))
    assert s2 == 5, f"应续 disk max(4) + 1 = 5；got {s2}"
    safe.close()


def test_flock_safe_tape_blocks_when_cli_holds_flock(tape_path, flock_path):
    """守护 append 时若 CLI 持 flock → 守护阻塞等到 CLI 释放（而非抢占）。

    意图：跨进程互斥正确性。守护用 LOCK_EX（阻塞），CLI 用 LOCK_EX|LOCK_NB。
    另一线程持锁 0.3s 期间守护尝试 append → append 完成时刻 ≥ 0.3s 后。
    """
    safe = _FlockSafeTape(tape_path, "r", flock_path=flock_path)
    tape_path.parent.mkdir(parents=True, exist_ok=True)
    flock_path.touch()

    holder_done = threading.Event()
    holder_started = threading.Event()

    def hold_lock():
        fd = open(flock_path, "w")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
            holder_started.set()
            holder_done.wait(timeout=2.0)  # 持锁等主线程释放信号
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            fd.close()

    t = threading.Thread(target=hold_lock, daemon=True)
    t.start()
    assert holder_started.wait(timeout=1.0), "持锁线程未启动"

    start = time.monotonic()
    seq = asyncio.run(safe.append({"type": "custom", "data": {}}))
    elapsed = time.monotonic() - start
    # 持锁期间守护应被阻塞（至少 0.2s；保守下界避免 flake）
    holder_done.set()
    t.join(timeout=2.0)

    assert seq == 1
    assert elapsed >= 0.15, f"守护未阻塞（elapsed={elapsed:.3f}s）；flock 互斥失效"
    safe.close()


def test_flock_safe_tape_read_max_seq_from_disk_empty(tape_path, flock_path):
    """空 tape / 不存在 → _read_max_seq_from_disk 返 0（不抛）。"""
    safe = _FlockSafeTape(tape_path, "r", flock_path=flock_path)
    assert safe._read_max_seq_from_disk() == 0
    safe.close()


def test_flock_safe_tape_read_max_seq_ignores_partial_trailing(tape_path, flock_path):
    """末尾 partial 行（持锁下不应出现，但容忍）→ 跳过取有效 max。"""
    with open(tape_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"seq": 3, "type": "agent_message", "timestamp": 0.0,
                            "node": None, "session_id": None, "data": {}}) + "\n")
        f.write("partial-line-not-json")  # partial trailing
    safe = _FlockSafeTape(tape_path, "r", flock_path=flock_path)
    assert safe._read_max_seq_from_disk() == 3
    safe.close()


# ── _watch_terminal ─────────────────────────────────────────────────────────


def _write_event(tape_path: Path, etype: str, seq: int) -> None:
    with open(tape_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"seq": seq, "type": etype, "timestamp": 0.0,
                            "node": None, "session_id": None, "data": {}}) + "\n")


def test_watch_terminal_returns_when_completed_seen(tmp_path):
    """tape 含 workflow_completed → _watch_terminal 返 'terminal'。"""
    tape = tmp_path / "run.jsonl"
    _write_event(tape, "workflow_started", 1)
    _write_event(tape, "workflow_completed", 2)

    reason = asyncio.run(_watch_terminal(tape, ttl_seconds=5, poll_interval=0.05))
    assert reason == "terminal"


def test_watch_terminal_returns_when_failed_or_cancelled(tmp_path):
    """workflow_failed / workflow_cancelled 也触发退出。"""
    for terminal in ("workflow_failed", "workflow_cancelled"):
        tape = tmp_path / f"{terminal}.jsonl"
        _write_event(tape, "workflow_started", 1)
        _write_event(tape, terminal, 2)
        reason = asyncio.run(_watch_terminal(tape, ttl_seconds=3, poll_interval=0.05))
        assert reason == "terminal", f"{terminal} 未触发 terminal 退出"


def test_watch_terminal_ttl_fallback(tmp_path):
    """无终态事件 + TTL 超时 → 返 'ttl'（兜底防泄漏）。"""
    tape = tmp_path / "run.jsonl"
    _write_event(tape, "workflow_started", 1)
    # 不写终态事件；TTL 设短到 3 * _WATCH_POLL_SECONDS 让测试快过
    reason = asyncio.run(_watch_terminal(tape, ttl_seconds=0.3, poll_interval=0.05))
    assert reason == "ttl"


def test_watch_terminal_picks_up_new_terminal_after_start(tmp_path):
    """启动时无终态；启动后追加 workflow_completed → 捕获。

    意图：增量读正确性（_watch_terminal 启动时记录 last_size，新事件触发检测）。
    """
    tape = tmp_path / "run.jsonl"
    _write_event(tape, "workflow_started", 1)

    async def go():
        watcher = asyncio.create_task(
            _watch_terminal(tape, ttl_seconds=5, poll_interval=0.05)
        )

        async def write_after_delay():
            await asyncio.sleep(0.1)
            _write_event(tape, "workflow_completed", 2)

        await write_after_delay()
        return await asyncio.wait_for(watcher, timeout=5)

    reason = asyncio.run(go())
    assert reason == "terminal"


def test_watch_terminal_handles_missing_tape(tmp_path):
    """tape 不存在 → 不崩，等 TTL（返 'ttl'）。"""
    tape = tmp_path / "nope.jsonl"
    reason = asyncio.run(_watch_terminal(tape, ttl_seconds=0.3, poll_interval=0.05))
    assert reason == "ttl"


def test_watch_terminal_handles_partial_write_race(tmp_path):
    """write(2) 中途被 poll 到 partial 行 → 下个 poll 重读完整行，终态事件不丢。

    意图：POSIX write(2) 对普通文件不保证原子性。若守护 poll 落在 CLI 写 workflow_completed
    的中途（已写 N 字节但行尾 \\n 未写），守护本次读到 partial JSON。**关键不变量**：守护的
    ``last_size`` 仅推进到最后一个 \\n 之后，partial 尾字节下个 poll 重读 → 终态事件最终必捕获。
    没有此修复 → 守护漏检终态，TTL 6h 才退（泄漏窗口）。

    本测试显式分两半写同一终态行（模拟 write 中途被 poll），验证守护最终捕获。
    """
    tape = tmp_path / "run.jsonl"
    # 先写 ws（让 _watch_terminal 启动时有内容扫）
    _write_event(tape, "workflow_started", 1)

    async def go():
        watcher = asyncio.create_task(
            _watch_terminal(tape, ttl_seconds=5, poll_interval=0.05)
        )

        # 等 watcher 进入 poll 循环（确保它已扫完 ws）
        await asyncio.sleep(0.15)

        # 分两半写 workflow_completed 行（模拟 write 中途被 poll）
        full_line = json.dumps({
            "seq": 2, "type": "workflow_completed", "timestamp": 0.0,
            "node": None, "session_id": None, "data": {},
        }) + "\n"
        half = len(full_line) // 2
        with open(tape, "a", encoding="utf-8") as f:
            f.write(full_line[:half])
            f.flush()
        # 等 watcher poll 一次（看到 partial，应跳过不推进 last_size）
        await asyncio.sleep(0.12)
        # 写剩余字节 + flush
        with open(tape, "a", encoding="utf-8") as f:
            f.write(full_line[half:])
            f.flush()

        return await asyncio.wait_for(watcher, timeout=3)

    reason = asyncio.run(go())
    assert reason == "terminal", "守护漏检 partial-write 后的终态事件（last_size 推进错）"


# ── _FlockSafeTape._read_max_seq_from_disk 增量缓存 ──────────────────────────


def test_read_max_seq_incremental_cache_picks_up_new_writes(tape_path, flock_path):
    """多次调用：首次全扫，之后只读增量；external 新写行被正确纳入 max。

    意图：增量缓存不能漏检新增的行（否则守护会沿用 stale max，导致 seq 冲突）。
    """
    safe = _FlockSafeTape(tape_path, "r", flock_path=flock_path)

    # 初始：tape 有 3 行（seq 1-3）
    for i in range(1, 4):
        _append_event(Tape(tape_path, run_id="r"), "agent_message", i)
    assert safe._read_max_seq_from_disk() == 3

    # 新写 2 行（seq 4-5）—— 仅这 2 行应被读
    for i in range(4, 6):
        _append_event(Tape(tape_path, run_id="r"), "node_completed", i)
    assert safe._read_max_seq_from_disk() == 5

    # 无新增 → cache 命中（O(1)）
    assert safe._read_max_seq_from_disk() == 5
    safe.close()


def test_read_max_seq_incremental_cache_handles_partial_trailing(tape_path, flock_path):
    """partial 尾行不推进 _scan_offset，下次重读 → 最终 max 正确。"""
    safe = _FlockSafeTape(tape_path, "r", flock_path=flock_path)
    # 完整行 seq=7
    _append_event(Tape(tape_path, run_id="r"), "agent_message", 7)
    assert safe._read_max_seq_from_disk() == 7

    # 追加 partial 行（无 \\n）
    with open(tape_path, "a", encoding="utf-8") as f:
        f.write('{"seq": 999, "type":')  # partial，未完
    # 应仍返 7（partial 未推进 offset）
    assert safe._read_max_seq_from_disk() == 7

    # 补完该行（seq=999）
    with open(tape_path, "a", encoding="utf-8") as f:
        f.write(' "agent_message", "timestamp": 0.0, "node": null, '
                '"session_id": null, "data": {}}\n')
    # 现在应捕获 seq=999
    assert safe._read_max_seq_from_disk() == 999
    safe.close()


# ── cli._chart_daemon_alive（确定性 socket 健康探，next respawn 判定基础）─────────


def test_chart_daemon_alive_no_socket_file(tmp_path):
    """socket 文件不存在 → False（守护未起 / 已 graceful 退出并 unlink）。"""
    from orca.iface.in_session.cli import _chart_daemon_alive
    assert _chart_daemon_alive(tmp_path / "nope.sock") is False


def test_chart_daemon_alive_stale_socket_returns_false(tmp_path):
    """stale socket（文件在但无监听者，守护被 SIGKILL 残留）→ False。

    意图：SIGKILL 不跑 finally unlink → socket 文件残留但无 accept 者。connect 抛
    ConnectionRefusedError → 探针判 dead → 触发 respawn。这是 next respawn 补丁的核心场景
    （pkill opencode 误杀守护后 socket 残留）。

    用 raw socket 忠实模拟：bind + listen + close fd（close 不会 unlink 路径名 —— Unix
    domain socket 的经典 gotcha，路径必须显式 unlink）→ 文件残留但无监听者。
    """
    import socket as _rawsocket
    from orca.iface.in_session.cli import _chart_daemon_alive
    stale = tmp_path / "stale.sock"

    raw = _rawsocket.socket(_rawsocket.AF_UNIX, _rawsocket.SOCK_STREAM)
    raw.bind(str(stale))
    raw.listen(8)
    raw.close()  # close fd 不 unlink 路径 → stale socket 文件残留（无监听者）
    assert stale.exists(), "前置：stale socket 文件确实残留"

    assert _chart_daemon_alive(stale) is False, "stale socket（无监听者）应判 dead"
    stale.unlink(missing_ok=True)


def test_chart_daemon_alive_real_listener_returns_true(tmp_path):
    """真有监听者 → True（connect 成功；对守护副作用 = 零：连上即 close，handler 读 EOF 静默）。"""
    from orca.iface.in_session.cli import _chart_daemon_alive
    sock = tmp_path / "live.sock"
    server_ready = threading.Event()
    stop_server = threading.Event()

    def run_server():
        # 子线程跑 asyncio server（chart_ingestor 的等价监听者）。
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def serve():
            server = await asyncio.start_unix_server(
                lambda r, w: w.close(), path=str(sock),
            )
            server_ready.set()
            try:
                while not stop_server.is_set():
                    await asyncio.sleep(0.05)
            finally:
                server.close()
                await server.wait_closed()

        try:
            loop.run_until_complete(serve())
        finally:
            loop.close()

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    try:
        assert server_ready.wait(timeout=3.0), "server 线程未在 3s 内就绪"
        assert _chart_daemon_alive(sock) is True, "活监听者应判 alive"
    finally:
        stop_server.set()
        t.join(timeout=3.0)
    sock.unlink(missing_ok=True)


# ── cli._ensure_chart_daemon（alive 早返 / spawn 失败降级）─────────────────────


def test_ensure_chart_daemon_no_spawn_when_alive(tmp_path, monkeypatch):
    """守护活（真监听者）→ ``_ensure_chart_daemon`` 早返，不调 ``_spawn_chart_daemon``。

    意图（防静默回归）：alive 早返是「每次 next 不多起一个守护」的关键。若被破坏（总 spawn），
    第二守护 ``chart_ingestor`` 入口的 ``unlink`` + ``rebind`` 会孤立第一守护，但 chart 仍落
    tape → 既有 e2e 测试全过、回归**静默**。本测试用真监听者（raw socket bind+listen+不关）
    + monkeypatch 记录 spawn，显式断言「活时不 spawn」。
    """
    import socket as _rawsocket
    from orca.iface.in_session import cli as cli_mod

    sock = tmp_path / "live.sock"
    raw = _rawsocket.socket(_rawsocket.AF_UNIX, _rawsocket.SOCK_STREAM)
    try:
        raw.bind(str(sock))
        raw.listen(8)  # 真监听者 → _chart_daemon_alive connect 成功 → True

        spawn_calls: list = []
        monkeypatch.setattr(cli_mod, "_spawn_chart_daemon",
                            lambda *a, **kw: spawn_calls.append((a, kw)))
        monkeypatch.setattr(cli_mod, "chart_sock_path", lambda run_id: sock)

        cli_mod._ensure_chart_daemon("r", tmp_path / "run.jsonl")
        assert spawn_calls == [], "守护活时 _ensure_chart_daemon 不应调 _spawn_chart_daemon"
    finally:
        raw.close()
        sock.unlink(missing_ok=True)


def test_ensure_chart_daemon_warns_on_spawn_oserror(tmp_path, monkeypatch):
    """守护死 + ``_spawn_chart_daemon`` 抛 OSError → 降级 warn、不抛（不崩 next）。

    意图：spawn 失败（Popen OSError：资源限制 / fd 耗尽）不应以裸 traceback 崩出 ``next``
    （``next`` 的 except 仅 catch ``InSessionError``）。chart 是便利层，缺了降级 warn。
    """
    from orca.iface.in_session import cli as cli_mod

    def _boom(*a, **kw):
        raise OSError("simulated resource limit (fd exhausted)")

    monkeypatch.setattr(cli_mod, "_chart_daemon_alive", lambda sock: False)  # 判死 → 走 spawn
    monkeypatch.setattr(cli_mod, "_spawn_chart_daemon", _boom)
    # _wait_for_sock 不应被触达（spawn 已 return）；patch 成 fail 兜底
    monkeypatch.setattr(cli_mod, "_wait_for_sock",
                        lambda *a, **kw: pytest.fail("_wait_for_sock 不应在 spawn 失败后被调"))

    # 不应抛
    cli_mod._ensure_chart_daemon("r", tmp_path / "run.jsonl")


# ── chart_daemon.main 端到端 smoke（spawn 真子进程）───────────────────────────


def test_main_daemon_lifecycle_spawns_binds_and_cleans_up(tmp_path):
    """``python -m orca.iface.in_session.chart_daemon`` 真起 → bind socket → SIGTERM → 清理。

    意图：生产入口（被 ``cli._spawn_chart_daemon`` detach spawn 的命令）零覆盖是规则 9 硬伤；
    本 smoke 测试驱通：argv 解析 → 起守护 → bind socket → 收信号 graceful 退出 → socket 文件清理。
    """
    import signal as _signal
    import subprocess
    import sys
    import time

    from orca.chart._paths import chart_sock_path

    # pid 后缀防 CI 并行 shard 撞 /tmp/orca-smoke-daemon-test.sock
    run_id = f"smoke-daemon-test-{os.getpid()}"
    tape = tmp_path / "run.jsonl"
    # 写 ws + 一行 custom（让守护启动时 tape 已有内容）
    tape.write_text(
        json.dumps({"seq": 1, "type": "workflow_started", "timestamp": 0.0,
                    "node": None, "session_id": None, "data": {}}) + "\n",
        encoding="utf-8",
    )
    sock = chart_sock_path(run_id)
    # 清潜在 stale socket（前次测试残留）
    try:
        sock.unlink()
    except FileNotFoundError:
        pass

    log = tmp_path / "daemon.log"
    log_fd = open(log, "a")
    proc = subprocess.Popen(
        [sys.executable, "-m", "orca.iface.in_session.chart_daemon",
         "--run-id", run_id, "--tape", str(tape), "--log-level", "INFO"],
        stdout=log_fd, stderr=log_fd, start_new_session=True,
    )
    log_fd.close()

    try:
        # 等 bind 就绪（socket 文件出现）
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if sock.exists():
                break
            time.sleep(0.05)
        assert sock.exists(), (
            f"守护未 bind socket；log=\n{log.read_text(encoding='utf-8', errors='replace')}"
        )

        # 发 SIGTERM → 守护 graceful 退出（loop.add_signal_handler → finally cleanup）
        proc.send_signal(_signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("守护未在 SIGTERM 后 5s 内退出")
        assert proc.returncode is not None

        # socket 文件应被 finally 清理
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not sock.exists():
                break
            time.sleep(0.05)
        assert not sock.exists(), (
            f"守护 SIGTERM 后 socket 未清理：{sock}；log=\n"
            f"{log.read_text(encoding='utf-8', errors='replace')}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
        try:
            sock.unlink()
        except FileNotFoundError:
            pass
