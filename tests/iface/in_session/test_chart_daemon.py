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
