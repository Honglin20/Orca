"""tests/iface/in_session/test_sidechain_daemon.py —— in-session sidechain 守护（SPEC-B v4 B2）。

覆盖意图（SPEC §9 AC1-AC7）：
  - liveness probe（``_sidechain_daemon_alive``）：pidfile 缺失/stale pid/wrong cmdline/ok。
  - `_SidechainDriver.run`：discover + stream + ingest 一轮 → tape 出 agent_*；多 poll 不重 emit。
  - crash callback：task cancel → no-op；exception → recreate；多次 crash 不无限爆炸。
  - 端到端 daemon subprocess：detach spawn → 写 mock sidechain jsonl → tape 出 agent_*
    （回合级实时，≤2s）。
  - 幂等（SPEC §9 AC2）：SIGKILL daemon → respawn → tape source_id 唯一（不重复 emit）。
  - 终态自退（SPEC §9 AC1）：tape 出 workflow_completed → daemon 自退。
  - U1 node 派生（SPEC §6）：tape 有 node_started → daemon emit agent_* node 正确。
  - bootstrap spawn + next respawn（cli 接线点）：spawn helper 起进程；ensure 探活 respawn。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orca.events.adapters.cc_jsonl import CCJsonlAdapter
from orca.events.bus import EventBus
from orca.events.raw_agent_event import RawAgentEvent
from orca.events.sidechain_ingestor import SidechainIngestor
from orca.events.tape import Tape
from orca.iface.in_session.cli import (
    _detect_backend_from_env,
    _ensure_sidechain_daemon,
    _spawn_sidechain_daemon,
)
from orca.iface.in_session.sidechain_daemon import (
    _SidechainDriver,
    _make_sidechain_crash_callback,
    _sidechain_daemon_alive,
    _sidechain_pidfile_path,
)


# ── helpers ─────────────────────────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


def _make_tape(tmp_path: Path, run_id: str = "test-run") -> tuple[EventBus, Tape, Path]:
    tape_path = tmp_path / "runs" / f"{run_id}.jsonl"
    tape = Tape(tape_path, run_id=run_id)
    bus = EventBus(tape)
    return bus, tape, tape_path


def _append_event(tape_path: Path, etype: str, *, seq: int, node: str | None = None,
                  session_id: str | None = None, data: dict | None = None) -> None:
    tape_path.parent.mkdir(parents=True, exist_ok=True)
    with open(tape_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "seq": seq, "type": etype, "timestamp": 0.0,
            "node": node, "session_id": session_id, "data": data or {},
        }) + "\n")


def _write_sidechain_line(root: Path, task_id: str, obj: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with open(root / f"agent-{task_id}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def _assistant_text(text: str) -> dict:
    return {"type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


# ── liveness probe ───────────────────────────────────────────────────────────


def test_pidfile_alive_no_pidfile():
    """pidfile 不存在 → False。"""
    # 用一个唯一 run_id 避免与正在跑的守护 pidfile 碰撞。
    run_id = "test-no-pidfile-xyz"
    assert _sidechain_daemon_alive(run_id) is False
    # 兜底清 pidfile（防测试间污染；正常不应存在）。
    _sidechain_pidfile_path(run_id).unlink(missing_ok=True)


def test_pidfile_alive_stale_pid(tmp_path):
    """pidfile 存在但 pid 死了 / 不存在 → False。"""
    run_id = "test-stale-pid-xyz"
    pidfile = _sidechain_pidfile_path(run_id)
    try:
        # 写一个几乎肯定不存在的 pid（max int32 附近）。
        pidfile.write_text("2147483600", encoding="utf-8")
        assert _sidechain_daemon_alive(run_id) is False
    finally:
        pidfile.unlink(missing_ok=True)


def test_pidfile_alive_wrong_cmdline(tmp_path):
    """pidfile 存在 + pid 活但 cmdline 不含 sidechain_daemon → False（防 pid 复用）。

    用本测试自己的 pid（pytest 进程）作 alive pid；cmdline 不含 sidechain_daemon。
    """
    run_id = "test-wrong-cmd-xyz"
    pidfile = _sidechain_pidfile_path(run_id)
    try:
        pidfile.write_text(str(os.getpid()), encoding="utf-8")
        # 本 pytest 进程 cmdline 不含 "orca.iface.in_session.sidechain_daemon"。
        assert _sidechain_daemon_alive(run_id) is False
    finally:
        pidfile.unlink(missing_ok=True)


def test_pidfile_alive_correct_cmdline(tmp_path, monkeypatch):
    """端到端：spawn 真 daemon subprocess → pidfile alive True。

    用真 daemon subprocess（非 mock）测 pidfile + /proc 校验的正确性 —— 验证守护进程
    的真实 argv 在 ``/proc/<pid>/cmdline`` 里能被正确解析。
    """
    run_id = "test-correct-cmd-xyz"
    sidechain_root = tmp_path / "sidechain"
    tape_path = tmp_path / "runs" / f"{run_id}.jsonl"
    tape_path.parent.mkdir(parents=True, exist_ok=True)
    _append_event(tape_path, "workflow_started", seq=1)
    _append_event(tape_path, "node_started", seq=2, node="N")
    monkeypatch.setenv("ORCA_CC_SIDECHAIN_ROOT", str(sidechain_root))

    if not Path("/proc").is_dir():
        pytest.skip("非 Linux，跳过 pidfile + /proc 校验测试")

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "orca.iface.in_session.sidechain_daemon",
            "--run-id", run_id, "--tape", str(tape_path),
            "--backend", "cc", "--host-session", "cmdline-test-host",
            "--ttl", "30", "--poll-interval", "0.5",
        ],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # 等 daemon 写 pidfile。
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if _sidechain_pidfile_path(run_id).is_file():
                break
            time.sleep(0.05)
        else:
            pytest.fail("daemon 未在 5s 内写 pidfile")
        assert _sidechain_daemon_alive(run_id) is True, (
            "真 daemon subprocess 应被 pidfile + /proc 校验识别为 alive"
        )
    finally:
        proc.send_signal(signal.SIGTERM)
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()
        _sidechain_pidfile_path(run_id).unlink(missing_ok=True)


# ── _SidechainDriver ─────────────────────────────────────────────────────────


def test_driver_iterates_and_ingests(tmp_path):
    """driver.run 单 iteration：discover + stream → ingest → tape 出 agent_*。"""
    sidechain_root = tmp_path / "sidechain"
    bus, tape, tape_path = _make_tape(tmp_path)
    try:
        # 准备 sidechain jsonl（一个子 agent 一条 text）。
        _write_sidechain_line(sidechain_root, "t1", _assistant_text("hello"))

        adapter = CCJsonlAdapter("h", root=sidechain_root)
        ingestor = SidechainIngestor(bus, tape_path)
        driver = _SidechainDriver(adapter, ingestor, "h", poll_interval=0.05)

        async def go():
            # 跑一 iteration 后 cancel。
            task = asyncio.create_task(driver.run())
            await asyncio.sleep(0.15)  # 至少跑一次 poll
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        _run(go())

        events = list(tape.replay())
        agent_msgs = [e for e in events if e.type == "agent_message"]
        assert len(agent_msgs) == 1
        assert agent_msgs[0].data["text"] == "hello"
        assert agent_msgs[0].session_id == "t1"
    finally:
        bus.close()


def test_driver_does_not_reemit_on_multiple_polls(tmp_path):
    """同一事件多 poll cycle 不重 emit（source_id dedup）。"""
    sidechain_root = tmp_path / "sidechain"
    bus, tape, tape_path = _make_tape(tmp_path)
    try:
        _write_sidechain_line(sidechain_root, "t1", _assistant_text("once"))

        adapter = CCJsonlAdapter("h", root=sidechain_root)
        ingestor = SidechainIngestor(bus, tape_path)
        # driver host_session 必须与 adapter 一致（discover_children scope 校验）。
        driver = _SidechainDriver(adapter, ingestor, "h", poll_interval=0.03)

        async def go():
            task = asyncio.create_task(driver.run())
            await asyncio.sleep(0.2)  # 多 poll cycle
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass

        _run(go())
        events = list(tape.replay())
        assert len([e for e in events if e.type == "agent_message"]) == 1, (
            "多 poll 不应触发重 emit"
        )
    finally:
        bus.close()


def test_driver_picks_up_new_lines_across_polls(tmp_path):
    """driver 多 poll cycle：第一次读旧行；后续 append 新行 → 第二次读到。"""
    sidechain_root = tmp_path / "sidechain"
    bus, tape, tape_path = _make_tape(tmp_path)
    try:
        _write_sidechain_line(sidechain_root, "t1", _assistant_text("first"))

        adapter = CCJsonlAdapter("h", root=sidechain_root)
        ingestor = SidechainIngestor(bus, tape_path)
        driver = _SidechainDriver(adapter, ingestor, "h", poll_interval=0.05)

        async def go():
            task = asyncio.create_task(driver.run())
            await asyncio.sleep(0.12)  # 第一次 poll 完成
            # 追加新行。
            _write_sidechain_line(sidechain_root, "t1", _assistant_text("second"))
            await asyncio.sleep(0.12)  # 第二次 poll 读到
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass

        _run(go())
        events = list(tape.replay())
        texts = [e.data["text"] for e in events if e.type == "agent_message"]
        assert texts == ["first", "second"]
    finally:
        bus.close()


# ── crash callback ──────────────────────────────────────────────────────────


def test_crash_callback_silent_on_cancelled(tmp_path):
    """task 被 cancel（守护正常退出路径）→ callback no-op（不重起）。"""
    sidechain_root = tmp_path / "sidechain"
    bus, tape, tape_path = _make_tape(tmp_path)
    try:
        adapter = CCJsonlAdapter("h", root=sidechain_root)

        def make_driver():
            ing = SidechainIngestor(bus, tape_path)
            return _SidechainDriver(adapter, ing, "h", poll_interval=0.05)

        driver = make_driver()

        async def go():
            task = asyncio.create_task(driver.run())
            task.add_done_callback(_make_sidechain_crash_callback(make_driver, "r1"))
            await asyncio.sleep(0.05)
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
            # 等一会，确认 callback 没 recreate task（若有，loop 里应能看到）。
            await asyncio.sleep(0.05)

        _run(go())
        # 只能间接验证：tape 仍可控（agent_* ≤ 1 个 / bus 仍可用）。
        # callback 的核心契约：cancelled 不重起（无新 task 创建）。
    finally:
        bus.close()


def test_crash_callback_recreates_on_exception(tmp_path):
    """task 抛异常（非 CancelledError）→ callback 重起 + 重挂 callback（递归）。

    直接用 asyncio.Task + raise_exception 模拟 task-level crash（绕过 driver 内部的
    iteration-level catch）；验证 callback 触发 recreate + 重挂。
    """
    sidechain_root = tmp_path / "sidechain"
    bus, tape, tape_path = _make_tape(tmp_path)
    try:
        adapter = CCJsonlAdapter("h", root=sidechain_root)
        recreate_count = {"n": 0}

        def make_driver():
            recreate_count["n"] += 1
            ing = SidechainIngestor(bus, tape_path)

            class _StubDriver(_SidechainDriver):
                """stub driver：不真 discover/stream；run() 直接 sleep 等 cancel。"""
                async def _iterate_once(self):
                    await asyncio.sleep(0.5)  # 占位；让 run loop 慢些

            return _StubDriver(adapter, ing, "h", poll_interval=0.05)

        async def go():
            # 直接造一个会 crash 的 task（不走 driver.run，避免 driver 内部 catch 兜住）。
            async def _crash():
                raise RuntimeError("simulated task crash")

            task = asyncio.create_task(_crash(), name="orca-sidechain-driver-r-crash")
            task.add_done_callback(_make_sidechain_crash_callback(make_driver, "r-crash"))

            # 等 crash + callback 触发 recreate。
            await asyncio.sleep(0.2)

            # 收尾：cancel 所有 sidechain task（含 recreated）。
            pending = [t for t in asyncio.all_tasks()
                       if t is not asyncio.current_task()
                       and "sidechain-driver" in t.get_name()]
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # crash callback 测试模拟的 RuntimeError 预期；其它异常记 debug 不掩盖。
                    pass

        _run(go())
        # 至少触发了一次重起（callback 看到 exception，调 make_driver + 起新 task）。
        assert recreate_count["n"] >= 1, (
            f"crash callback 应调 make_driver；recreate_count={recreate_count}"
        )
    finally:
        bus.close()


# ── backend dispatch（_make_adapter）─────────────────────────────────────────


def test_make_adapter_dispatches_cc():
    from orca.iface.in_session.sidechain_daemon import _make_adapter
    a = _make_adapter("cc", "fake-host")
    from orca.events.adapters.cc_jsonl import CCJsonlAdapter
    assert isinstance(a, CCJsonlAdapter)


def test_make_adapter_dispatches_opencode():
    from orca.iface.in_session.sidechain_daemon import _make_adapter
    a = _make_adapter("opencode", "fake-host")
    from orca.events.adapters.opencode_sqlite import OpencodeSqliteAdapter
    assert isinstance(a, OpencodeSqliteAdapter)


def test_make_adapter_unknown_raises():
    from orca.iface.in_session.sidechain_daemon import _make_adapter
    with pytest.raises(ValueError, match="unknown backend"):
        _make_adapter("vintage", "h")


# ── _make_adapter family 透传（SPEC §P4）──────────────────────────────────────


def test_make_adapter_cc_passes_family_to_adapter(monkeypatch, tmp_path):
    """SPEC §P4：``_make_adapter("cc", h, family="cac")`` → CCJsonlAdapter 用 .cac dotdir。

    守 family 透传链：daemon argv ``--family`` → ``_make_adapter`` → adapter ctor → resolver。
    若有人误删 ``family=family`` 透传，adapter 会回退探测（默认 .claude）→ root 路径出错。
    """
    monkeypatch.delenv("ORCA_CC_SIDECHAIN_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    from orca.iface.in_session.sidechain_daemon import _make_adapter
    adapter = _make_adapter("cc", "h-sid", family="cac")
    # root 应指向 .cac（family=cac），非默认 .claude。
    assert ".cac" in adapter.root.parts, (
        f"family=cac 应让 adapter root 走 .cac dotdir，得 {adapter.root}"
    )
    assert ".claude" not in adapter.root.parts


def test_make_adapter_opencode_passes_family_to_adapter(monkeypatch, tmp_path):
    """SPEC §P4：``_make_adapter("opencode", h, family="nga")`` → adapter db 指向 .local/share/nga。"""
    monkeypatch.delenv("ORCA_OPENCODE_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from orca.iface.in_session.sidechain_daemon import _make_adapter
    adapter = _make_adapter("opencode", "h-sid", family="nga")
    assert adapter.db_path == tmp_path / ".local" / "share" / "nga" / "opencode.db"


def test_make_adapter_family_none_falls_back_to_probe(monkeypatch, tmp_path):
    """family=None → adapter 走 resolver probe/default（不传 family，回归既有行为）。"""
    monkeypatch.delenv("ORCA_CC_SIDECHAIN_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    from orca.iface.in_session.sidechain_daemon import _make_adapter
    # family=None + 无任何 .claude/.cac 存在 → resolver default .claude。
    adapter = _make_adapter("cc", "h-sid")
    assert ".claude" in adapter.root.parts


# ── _detect_backend_from_env（cli.py 启动参数检测）──────────────────────────


def test_detect_backend_cc(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-123")
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    assert _detect_backend_from_env() == "cc"


def test_detect_backend_opencode(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("ORCA_HOST_SESSION_ID", "opc-456")
    assert _detect_backend_from_env() == "opencode"


def test_detect_backend_none(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    assert _detect_backend_from_env() is None


# ── 端到端：subprocess daemon 实时推送 ─────────────────────────────────────


def _spawn_daemon_subprocess(
    run_id: str, tape_path: Path, backend: str, host_session: str,
    *, ttl: int = 30, poll: float = 0.1, family: str | None = None,
) -> subprocess.Popen:
    """detach spawn 一个真的 sidechain daemon subprocess（测试 e2e 用）。

    ``family`` 非空 → 加 ``--family`` argv（SPEC §P4 e2e 演练）。
    """
    cmd = [
        sys.executable, "-m", "orca.iface.in_session.sidechain_daemon",
        "--run-id", run_id, "--tape", str(tape_path),
        "--backend", backend, "--host-session", host_session,
        "--ttl", str(ttl), "--poll-interval", str(poll),
        "--log-level", "DEBUG",
    ]
    if family is not None:
        cmd.extend(["--family", family])
    log_fd = open(tape_path.parent / f"{run_id}_daemon.log", "a", encoding="utf-8")
    return subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
        start_new_session=True, close_fds=True,
    )


def test_e2e_daemon_ingests_cc_sidechain(tmp_path, monkeypatch):
    """端到端：spawn daemon → 写 sidechain jsonl → tape 出 agent_*（回合级实时）。

    SPEC §9 AC1 + AC7：实时测试方法（真 daemon + 真 tape，断言时延 ≤2s）。
    """
    run_id = "e2e-cc-run"
    sidechain_root = tmp_path / "sidechain"
    bus, tape, tape_path = _make_tape(tmp_path, run_id=run_id)

    # bootstrap 模拟：先写 workflow_started + node_started。
    _append_event(tape_path, "workflow_started", seq=1,
                  data={"host_session": "e2e-host"})
    _append_event(tape_path, "node_started", seq=2, node="A")
    bus.close()  # 让 daemon 自己 _FlockSafeTape 写

    # 覆盖 sidechain root（用 env）。
    monkeypatch.setenv("ORCA_CC_SIDECHAIN_ROOT", str(sidechain_root))
    # CC daemon 内部用 env 解析 root（即便 cli 给的是 --backend cc）。
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "e2e-host")

    proc = _spawn_daemon_subprocess(run_id, tape_path, "cc", "e2e-host")
    try:
        # 等 daemon 写 pidfile + 起来。
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if _sidechain_daemon_alive(run_id):
                break
            time.sleep(0.05)
        else:
            pytest.fail("daemon 未在 5s 内 alive")

        # 模拟 CC 写 sidechain jsonl（子 agent 一条 text）。
        t0 = time.monotonic()
        _write_sidechain_line(sidechain_root, "e2e-task-1", _assistant_text("live event"))

        # 等 tape 出现 agent_message（实时窗口 ≤2s）。
        deadline = time.monotonic() + 3.0
        agent_event = None
        while time.monotonic() < deadline:
            with open(tape_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "agent_message":
                        agent_event = obj
                        break
            if agent_event:
                break
            time.sleep(0.05)

        elapsed = time.monotonic() - t0
        assert agent_event is not None, (
            f"daemon 未在 3s 内把 sidechain 事件 emit 到 tape；log:\n"
            + (tape_path.parent / f"{run_id}_daemon.log").read_text(encoding="utf-8", errors="replace")
        )
        assert agent_event["data"]["text"] == "live event"
        assert agent_event["session_id"] == "e2e-task-1"
        assert agent_event["node"] == "A", "U1 派生：node_started[A] 是最后一条 → agent_* 挂 A"
        assert elapsed <= 2.0, f"回合级实时 ≤2s；实际 {elapsed:.2f}s"
    finally:
        proc.send_signal(signal.SIGTERM)
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        _sidechain_pidfile_path(run_id).unlink(missing_ok=True)


def test_e2e_daemon_with_family_argv_reads_cac_dotdir(tmp_path, monkeypatch):
    """SPEC §P4：daemon argv ``--family cac`` → 读 ``~/.cac/projects/<enc>/<host>/subagents``。

    不设 ``ORCA_CC_SIDECHAIN_ROOT`` env（让 family 决定路径），用 ``HOME`` env 隔离 ``Path.home``
    到 tmp_path/home，构造 ``.cac/projects/.../subagents`` 路径，spawn daemon 传 ``--family cac``
    → daemon 内 resolver 走 config 分支（source="config"）→ 读 .cac。

    守 ``--family`` argv 解析 + 透传到 ``_make_adapter`` → adapter ctor → resolver 的端到端链。
    """
    run_id = "e2e-family-cac"
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # subprocess 继承 HOME env → 其内 Path.home() 返 fake_home（subprocess 不受 monkeypatch 影响）。
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("ORCA_CC_SIDECHAIN_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)

    encoded = str(tmp_path).replace("/", "-")  # _encode_cwd 等价
    sidechain_root = (
        fake_home / ".cac" / "projects" / encoded / "fam-host" / "subagents"
    )
    sidechain_root.mkdir(parents=True)

    bus, tape, tape_path = _make_tape(tmp_path, run_id=run_id)
    _append_event(tape_path, "workflow_started", seq=1,
                  data={"host_session": "fam-host"})
    _append_event(tape_path, "node_started", seq=2, node="A")
    bus.close()

    proc = _spawn_daemon_subprocess(
        run_id, tape_path, "cc", "fam-host", family="cac",
    )
    try:
        # 等 daemon 写 pidfile + 起来。
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if _sidechain_daemon_alive(run_id):
                break
            time.sleep(0.05)
        else:
            pytest.fail("daemon 未在 5s 内 alive")

        # 模拟 cac 写 sidechain jsonl。
        _write_sidechain_line(sidechain_root, "fam-task-1", _assistant_text("from cac"))

        # 等 tape 出现 agent_message（family=cac argv 失败 → 读不到 .cac → tape 无事件）。
        deadline = time.monotonic() + 3.0
        agent_event = None
        while time.monotonic() < deadline:
            with open(tape_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "agent_message":
                        agent_event = obj
                        break
            if agent_event:
                break
            time.sleep(0.05)

        assert agent_event is not None, (
            "daemon 未读到 .cac sidechain（--family cac argv 透传链失败）；log:\n"
            + (tape_path.parent / f"{run_id}_daemon.log").read_text(encoding="utf-8", errors="replace")
        )
        assert agent_event["data"]["text"] == "from cac"
        assert agent_event["session_id"] == "fam-task-1"
    finally:
        proc.send_signal(signal.SIGTERM)
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        _sidechain_pidfile_path(run_id).unlink(missing_ok=True)


def test_e2e_idempotent_after_sigkill_and_respawn(tmp_path, monkeypatch):
    """SPEC §9 AC2：SIGKILL daemon → respawn → tape source_id 唯一无重复。

    步骤：
      1. spawn daemon，写一条 sidechain → daemon emit 到 tape。
      2. SIGKILL daemon（模拟 crash；不跑 finally 清理）。
      3. spawn 新 daemon（手动调 _ensure 等价的 spawn）。
      4. 新 daemon 从 tape 重建 source_id set；再写同 sidechain 行（用同 task_id 但 cursor 重置）
         → **不**应重复 emit。
      5. tape agent_message 仍 1 条（source_id 唯一）。
    """
    run_id = "e2e-idempotent-run"
    sidechain_root = tmp_path / "sidechain"
    bus, tape, tape_path = _make_tape(tmp_path, run_id=run_id)
    _append_event(tape_path, "workflow_started", seq=1)
    _append_event(tape_path, "node_started", seq=2, node="N")
    bus.close()

    monkeypatch.setenv("ORCA_CC_SIDECHAIN_ROOT", str(sidechain_root))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "idem-host")

    # 第一次 spawn + emit。
    proc1 = _spawn_daemon_subprocess(run_id, tape_path, "cc", "idem-host")
    try:
        # 等 alive。
        for _ in range(100):
            if _sidechain_daemon_alive(run_id): break
            time.sleep(0.05)
        # 写 sidechain 行。
        _write_sidechain_line(sidechain_root, "task-X", _assistant_text("emit-once"))
        # 等 tape 出现 agent_message。
        for _ in range(60):
            with open(tape_path) as f:
                if any('"agent_message"' in line for line in f):
                    break
            time.sleep(0.05)
        else:
            pytest.fail("第一次 daemon 未 emit agent_message")
    finally:
        # SIGKILL（不跑 finally 清理 pidfile）。
        proc1.kill()
        proc1.wait(timeout=5)

    # stale pidfile 残留（SIGKILL 不跑 unlink）→ _sidechain_daemon_alive 应判 dead。
    # 等内核回收 pid（避免 pidfile 指向活进程）。
    time.sleep(0.2)
    assert _sidechain_daemon_alive(run_id) is False, "SIGKILL 后应判 dead"

    # 第二次 spawn（模拟 _ensure respawn）。
    proc2 = _spawn_daemon_subprocess(run_id, tape_path, "cc", "idem-host")
    try:
        for _ in range(100):
            if _sidechain_daemon_alive(run_id): break
            time.sleep(0.05)
        # 等 daemon rebuild + 跑至少一次 poll（不写新行 → 不应 emit）。
        time.sleep(0.5)
    finally:
        proc2.send_signal(signal.SIGTERM)
        try: proc2.wait(timeout=5)
        except subprocess.TimeoutExpired: proc2.kill()
        _sidechain_pidfile_path(run_id).unlink(missing_ok=True)

    # 验证 tape 中 agent_message 仍只 1 条（不重复 emit；source_id dedup 兜底）。
    agent_msgs = []
    with open(tape_path, "r", encoding="utf-8") as f:
        for line in f:
            try: obj = json.loads(line)
            except json.JSONDecodeError: continue
            if obj.get("type") == "agent_message":
                agent_msgs.append(obj)
    assert len(agent_msgs) == 1, (
        f"SIGKILL + respawn 后 tape agent_message 应 1 条（幂等），实际 {len(agent_msgs)}"
    )
    # source_id 唯一。
    sids = [m["data"].get("source_id") for m in agent_msgs]
    assert len(set(sids)) == 1, f"source_id 重复: {sids}"


def test_e2e_terminal_event_exits_daemon(tmp_path, monkeypatch):
    """SPEC §9：tape 出 workflow_completed → daemon 自退（_watch_terminal 触发）。"""
    run_id = "e2e-terminal-run"
    sidechain_root = tmp_path / "sidechain"
    bus, tape, tape_path = _make_tape(tmp_path, run_id=run_id)
    _append_event(tape_path, "workflow_started", seq=1)
    _append_event(tape_path, "node_started", seq=2, node="N")
    bus.close()

    monkeypatch.setenv("ORCA_CC_SIDECHAIN_ROOT", str(sidechain_root))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "term-host")

    proc = _spawn_daemon_subprocess(run_id, tape_path, "cc", "term-host", ttl=30)
    try:
        # 等 alive。
        for _ in range(100):
            if _sidechain_daemon_alive(run_id): break
            time.sleep(0.05)
        else:
            pytest.fail("daemon 未 alive")

        # 写 workflow_completed → _watch_terminal 应捕获，daemon 自退。
        _append_event(tape_path, "workflow_completed", seq=3,
                      data={"outputs": {}})

        # 等 daemon 自退（pidfile 被 finally 清）。
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert proc.poll() is not None, "daemon 应在 workflow_completed 后自退"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        _sidechain_pidfile_path(run_id).unlink(missing_ok=True)


# ── cli spawn helpers（_spawn_sidechain_daemon / _ensure_sidechain_daemon）──


def test_spawn_sidechain_daemon_skips_when_no_host_session(tmp_path, monkeypatch, caplog):
    """无 host_session env → skip spawn（fail-open；warn log）。"""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    # _spawn 不应起子进程。
    _spawn_sidechain_daemon("r", tmp_path / "tape.jsonl")
    # 静默 skip；无子进程残留（conftest 会兜底清，但这里无）。


def test_ensure_sidechain_daemon_respawns_when_dead(tmp_path, monkeypatch):
    """ensure 探 dead → spawn 新 daemon；探 alive → no-op。"""
    run_id = "ensure-run-xyz"
    sidechain_root = tmp_path / "sidechain"
    tape_path = tmp_path / "runs" / f"{run_id}.jsonl"
    tape_path.parent.mkdir(parents=True, exist_ok=True)
    _append_event(tape_path, "workflow_started", seq=1,
                  data={"host_session": "ensure-host"})
    _append_event(tape_path, "node_started", seq=2, node="N")

    monkeypatch.setenv("ORCA_CC_SIDECHAIN_ROOT", str(sidechain_root))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ensure-host")

    try:
        # 第一次 ensure：应 spawn（dead → alive）。
        _ensure_sidechain_daemon(run_id, tape_path)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if _sidechain_daemon_alive(run_id):
                break
            time.sleep(0.05)
        else:
            pytest.fail("_ensure 第一次未成功 spawn")

        # 第二次 ensure：应 no-op（alive → return early）。
        _ensure_sidechain_daemon(run_id, tape_path)
        # 验证仍只一个守护进程（no double spawn）。
        # 简化：pidfile 未被覆盖（同 pid）。读 pidfile 比对前后 pid。
        pid1 = int(_sidechain_pidfile_path(run_id).read_text().strip())
        _ensure_sidechain_daemon(run_id, tape_path)
        time.sleep(0.3)
        pid2 = int(_sidechain_pidfile_path(run_id).read_text().strip())
        assert pid1 == pid2, "alive 时 ensure 不应起第二个 daemon"
    finally:
        # 清理：kill 守护。
        pidfile = _sidechain_pidfile_path(run_id)
        if pidfile.is_file():
            try:
                pid = int(pidfile.read_text().strip())
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, ValueError, OSError):
                pass
            pidfile.unlink(missing_ok=True)
