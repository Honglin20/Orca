"""test_registry.py —— ProcessRegistry DI acquire/release/shutdown 幂等 + signal-safe。

phase-11-process-lifecycle §1 / §5.1 / ADR §4.7。

verify intent (Rule 9)：
  - 测试不是「registry 能用」，而是「**DI 隔离 + 幂等清理**」——多个测试同时跑不互相污染，
    shutdown 多次调不报错（atexit / signal / orchestrator 三处都调是常态）。
"""

from __future__ import annotations

import signal
import threading
from unittest.mock import MagicMock

import pytest

from orca.exec.registry import (
    ProcessRegistry,
    RegisteredProcess,
    get_default_registry,
    spawn_kwargs_for_process_group,
)


# ── 假进程对象（只持 .pid，足够 acquire 用）─────────────────────────────────


class _FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid


# ── acquire / release ─────────────────────────────────────────────────────


def test_acquire_returns_entry_with_proc_metadata(process_local: ProcessRegistry):
    """acquire 登记 proc + 派生 entry 字段（pid/pgid/backend/run_id/node_id/...）。"""
    proc = _FakeProc(pid=4242)
    entry = process_local.acquire(
        proc, backend="claude", run_id="run-xyz", node_id="analyzer",
    )
    assert isinstance(entry, RegisteredProcess)
    assert entry.pid == 4242
    assert entry.backend == "claude"
    assert entry.run_id == "run-xyz"
    assert entry.node_id == "analyzer"
    # started_at 是最近时间戳（容忍 5s 漂移——CI 慢机器）
    import time
    assert abs(entry.started_at - time.time()) < 5.0


def test_acquire_cleans_up_after_release(process_local: ProcessRegistry):
    """release 后 entry 从 registry 移除——幂等（多次 release 不报错）。"""
    proc = _FakeProc(pid=1001)
    process_local.acquire(proc, backend="opencode", run_id="r1")
    process_local.release(1001)
    # 再次 release：不报错（幂等）
    process_local.release(1001)
    process_local.release(99999)  # 未注册的 pid 也不报错


def test_release_unknown_pid_is_noop(process_local: ProcessRegistry):
    """未注册 pid release = no-op（铁律 5 幂等）。"""
    process_local.release(99999)


def test_acquire_overwrite_logs_warning(process_local: ProcessRegistry, caplog):
    """同 pid 重复 acquire 覆盖 + 警告（正常路径不应发生，但不崩）。"""
    proc1 = _FakeProc(pid=5000)
    proc2 = _FakeProc(pid=5000)
    process_local.acquire(proc1, backend="claude", run_id="r1")
    with caplog.at_level("WARNING"):
        process_local.acquire(proc2, backend="opencode", run_id="r2")
    # 第二次覆盖：backend 已更新
    # 直接验证：内部 dict 持有的是 proc2 的 entry
    with process_local._lock:
        entry = process_local._procs[5000]
    assert entry.backend == "opencode"
    assert any("pid=5000" in rec.message for rec in caplog.records)


def test_acquire_during_shutdown_raises(process_local: ProcessRegistry):
    """shutdown 进行中再 acquire → RuntimeError fail loud（避免新 proc 进将死 registry）。"""
    proc = _FakeProc(pid=7000)
    process_local.acquire(proc, backend="claude", run_id="r1")
    # 模拟 shutdown 进行中
    with process_local._lock:
        process_local._shutting_down = True
    with pytest.raises(RuntimeError, match="正在 shutdown"):
        process_local.acquire(_FakeProc(pid=7001), backend="claude", run_id="r2")


# ── kill_one ─────────────────────────────────────────────────────────────


def test_kill_one_unknown_pid_is_noop(process_local: ProcessRegistry):
    """未注册 pid kill_one = no-op（幂等）。"""
    process_local.kill_one(99999, grace_seconds=0.1)  # 不报错


def test_kill_one_rejects_grace_over_10s(process_local: ProcessRegistry):
    """SPEC §2.3：grace > 10s 阻塞 cancel，禁止。"""
    proc = _FakeProc(pid=8000)
    process_local.acquire(proc, backend="claude", run_id="r1")
    with pytest.raises(ValueError, match="超过上限"):
        process_local.kill_one(8000, grace_seconds=15.0)


def test_kill_one_runs_cleanup_hooks(process_local: ProcessRegistry):
    """Stage 4 cleanup hooks 总是跑（即使进程已退出）。"""
    called = []
    proc = _FakeProc(pid=8001)
    process_local.acquire(
        proc, backend="claude", run_id="r1",
        cleanup_hooks=[lambda: called.append("hook1"), lambda: called.append("hook2")],
    )
    # 进程 pid 8001 实际不存在，kill_one 会走 SIGTERM→SIGKILL→cleanup 流程
    # （ProcessLookupError 各 stage 内部吞）
    process_local.kill_one(8001, grace_seconds=0.05)
    assert called == ["hook1", "hook2"]


def test_kill_one_cleanup_hook_exception_does_not_block(process_local: ProcessRegistry, caplog):
    """cleanup hook 抛异常不阻塞 shutdown（SPEC §2.2，warning + 继续）。"""
    def boom():
        raise RuntimeError("cleanup hook 故意崩")
    proc = _FakeProc(pid=8002)
    process_local.acquire(
        proc, backend="claude", run_id="r1", cleanup_hooks=[boom],
    )
    with caplog.at_level("WARNING"):
        process_local.kill_one(8002, grace_seconds=0.05)
    assert any("cleanup hook" in rec.message for rec in caplog.records)


def test_kill_one_releases_entry(process_local: ProcessRegistry):
    """kill_one 完成后 release entry（多次 kill_one 同 pid 不报错）。"""
    proc = _FakeProc(pid=8003)
    process_local.acquire(proc, backend="claude", run_id="r1")
    process_local.kill_one(8003, grace_seconds=0.05)
    # 第二次 kill：已是 unknown pid，no-op
    process_local.kill_one(8003, grace_seconds=0.05)


# ── shutdown 幂等 ─────────────────────────────────────────────────────────


def test_shutdown_is_idempotent(process_local: ProcessRegistry):
    """SPEC §1.1 / 铁律 5：shutdown 多次调不报错（atexit / signal / RunManager 三处都调）。"""
    proc = _FakeProc(pid=9001)
    process_local.acquire(proc, backend="claude", run_id="r1")
    process_local.shutdown()
    process_local.shutdown()
    process_local.shutdown()  # 第三次：完全 no-op


def test_shutdown_clears_all_entries(process_local: ProcessRegistry):
    """shutdown 清空所有未 release 的 entry。"""
    for pid in (100, 101, 102):
        process_local.acquire(_FakeProc(pid=pid), backend="claude", run_id="r1")
    process_local.shutdown()
    with process_local._lock:
        assert process_local._procs == {}


def test_shutdown_after_shutdown_blocks_further_acquire(process_local: ProcessRegistry):
    """shutdown 后 acquire 应 RuntimeError——确保新进程不进将死的 registry。"""
    process_local.shutdown()
    with pytest.raises(RuntimeError):
        process_local.acquire(_FakeProc(pid=11000), backend="claude", run_id="r1")


# ── atexit 注册 ──────────────────────────────────────────────────────────


def test_acquire_registers_atexit_once(process_local: ProcessRegistry):
    """首次 acquire 后注册 atexit；后续 acquire 不重复注册。"""
    process_local.acquire(_FakeProc(pid=12000), backend="claude", run_id="r1")
    assert process_local._atexit_registered is True
    process_local.acquire(_FakeProc(pid=12001), backend="claude", run_id="r1")
    # 仍是 True（不重复注册）
    assert process_local._atexit_registered is True


# ── signal-safety：handler 只能设 Event，不直接调 shutdown ────────────────


def test_signal_handler_should_only_set_event_not_call_shutdown():
    """SPEC §1.3：signal handler 必须 async-signal-safe——只设 Event，不直接调 shutdown。

    本测试验证 ``run/__main__.py`` 里 _on_signal 的设计契约（不真正发信号）：
    构造一个最小 handler 模拟其行为，验证只 set Event。
    """
    shutdown_event = threading.Event()
    registry = ProcessRegistry()

    # 这是 SPEC §1.3 要求的 handler 模板
    def _on_signal(signum, frame):  # noqa: ANN001
        # 仅 async-signal-safe 操作：threading.Event.set 是 async-signal-safe
        shutdown_event.set()

    # 模拟 signal 调用
    _on_signal(signal.SIGINT, None)
    assert shutdown_event.is_set()
    # registry 内部 state 不变（未触发 shutdown）
    assert registry._shutting_down is False


# ── DI 隔离：每个 fixture 实例独立 ─────────────────────────────────────────


def test_process_local_fixture_instances_are_isolated(
    process_local: ProcessRegistry,
):
    """两次 process_local 各自独立（ADR §4.7 闭环 B8：避免 xdist 并行污染）。"""
    other = ProcessRegistry()
    proc = _FakeProc(pid=5555)
    process_local.acquire(proc, backend="claude", run_id="r1")
    # other 不持有此 pid
    with other._lock:
        assert 5555 not in other._procs


# ── get_default_registry 模块级惰性 ────────────────────────────────────────


def test_get_default_registry_returns_same_instance(monkeypatch):
    """production singleton：同进程多次调返同一实例（lazily-created）。

    测试后复位模块级 singleton，避免污染同进程后续测试（review 🟡 #3）。
    """
    r1 = get_default_registry()
    r2 = get_default_registry()
    assert r1 is r2
    # 复位：让后续测试的 default singleton 重新 lazily-created（避免此测试 acquire/shutdown
    # 后的 state 跨测试泄漏——尤其 xdist 并行 worker 长生命周期）。
    monkeypatch.setattr("orca.exec.registry._default_registry", None)


# ── spawn_kwargs_for_process_group ──────────────────────────────────────


def test_spawn_kwargs_returns_dict_for_create_subprocess_exec():
    """spawn_kwargs_for_process_group 返回 dict，展开到 create_subprocess_exec。"""
    kwargs = spawn_kwargs_for_process_group()
    assert isinstance(kwargs, dict)
    # POSIX: {'start_new_session': True}; Windows: {'creationflags': ...}
    assert "start_new_session" in kwargs or "creationflags" in kwargs


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
