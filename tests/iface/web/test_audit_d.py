"""test_audit_d.py —— SPEC D（并发 / 守护竞态）findings 1 + 4 单测。

覆盖：
  - finding 1（auto-exit 不撕并发 run）：``_wait_ws_autoexit`` 第三条件 + debug 日志。
    - T1：manager 有非终态 in-process run → 阻挡退出 + debug 日志（ORCA_WEB_AUTOEXIT_SECONDS=2）。
    - T2（负向）：manager 无非终态 run + 无 WS + window 过 → 退出（原功能不回归）。
    - T3：``--stay`` 路径不调 ``_wait_ws_autoexit``（``_serve_and_run_inprocess(stay=True)``）。
  - finding 4（控制帧 QueueFull warn）：
    - T7：``maxsize=1`` queue 填满 → ``_on_run_changed`` 触发 ``logger.warning`` + 计数。
    - T8：grep 守门 —— 所有「帧 payload」put_nowait 路径的 QueueFull 必 warn（None-sentinel 除外）。

AC-2 / AC-3（pidfile 原子 + macOS liveness）单测见 ``tests/iface/in_session/``：
  - T4/T5 → ``test_sidechain_daemon.py``。
  - T6 + D-1 → ``test_daemon_liveness.py``。
"""
from __future__ import annotations

import asyncio
import logging
import time
import types
from pathlib import Path

import pytest

from orca.iface.cli.commands import _wait_ws_autoexit
from orca.iface.web.ws_handler import WebServer


# ── T1 / T2：auto-exit 第三条件（SPEC D finding 1 / AC-1）─────────────────────


class _FakeWebServer:
    """fake web_server：``active_ws_count`` / ``last_ws_activity_at`` 可调。"""

    def __init__(self, *, active: int, ws_age: float):
        self.active_ws_count = active
        self.last_ws_activity_at = time.monotonic() - ws_age


class _FakeManager:
    """fake RunManager：``has_nonterminal_inproc_runs`` 可控。"""

    def __init__(self, *, has_active: bool):
        self._has_active = has_active
        # debug log 内联 sum 在 has_active=True 时读 ``_runs``；空 dict 让 sum 返 0。
        self._runs: dict = {}

    def has_nonterminal_inproc_runs(self) -> bool:
        return self._has_active


class TestAutoExitThirdConjunct:
    """SPEC D finding 1 / AC-1：``_wait_ws_autoexit`` 第三条件 + debug 日志。"""

    def test_t1_blocks_when_concurrent_run_active_and_logs_debug(
        self, caplog, monkeypatch,
    ):
        """T1：manager 有非终态 in-process run → 即便无 WS + window 过也不退 + 记 debug。

        ORCA_WEB_AUTOEXIT_SECONDS=2（R-3：wait 窗口 ≪ 假设的 B sleep 30s）。
        """
        # R-3：T1/AC-1 设此 env（即便本测试直接传 autoexit_seconds，env 也设上守门）。
        monkeypatch.setenv("ORCA_WEB_AUTOEXIT_SECONDS", "2")

        ws = _FakeWebServer(active=0, ws_age=10.0)  # 无活跃 WS + window 显然已过
        manager = _FakeManager(has_active=True)

        async def _go():
            await asyncio.wait_for(
                _wait_ws_autoexit(ws, 2.0, manager), timeout=6.0,
            )

        start = time.monotonic()
        with caplog.at_level(logging.DEBUG, logger="orca.iface.cli.commands"):
            with pytest.raises(asyncio.TimeoutError):
                asyncio.run(_go())
        # 阻挡 >= 5s（autoexit_seconds=2 ≪ 6，证明不是 window 在挡，是第三条件在挡）。
        assert time.monotonic() - start >= 5.0

        # R-3：T1 必须断言 debug 日志「auto-exit deferred: N non-terminal ...」。
        msgs = [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.DEBUG and "auto-exit deferred" in r.getMessage()
        ]
        assert msgs, f"未找到 auto-exit deferred debug 日志，caplog messages={msgs}"
        # 含 N（非终态 run 数）。
        assert "non-terminal in-process run" in msgs[0]

    def test_t1_emits_debug_only_once_per_deferred_streak(self, caplog, monkeypatch):
        """C-1：fail-path warning 加「once per deferred streak」节流。

        连续命中第三条件 N 轮，debug 日志只记一次（避免每秒 log 垃圾风暴）。退出窗口（active WS）
        打断 streak 后，再次进入 deferred 阶段再记一次。
        """
        monkeypatch.setenv("ORCA_WEB_AUTOEXIT_SECONDS", "2")
        ws = _FakeWebServer(active=0, ws_age=10.0)
        manager = _FakeManager(has_active=True)

        async def _go():
            await asyncio.wait_for(
                _wait_ws_autoexit(ws, 2.0, manager), timeout=3.0,
            )

        with caplog.at_level(logging.DEBUG, logger="orca.iface.cli.commands"):
            with pytest.raises(asyncio.TimeoutError):
                asyncio.run(_go())
        deferred_msgs = [
            r for r in caplog.records
            if r.levelno == logging.DEBUG and "auto-exit deferred" in r.getMessage()
        ]
        # 3s 内 sleep 1s 一轮 ≈ 3 轮命中，但 debug 只记 1 次（streak 节流）。
        assert len(deferred_msgs) == 1, (
            f"once-per-streak 节流失效：{len(deferred_msgs)} 条 debug"
        )

    def test_t2_returns_when_no_concurrent_run_and_window_elapsed(self):
        """T2（负向）：manager 无非终态 run + 无 WS + window 过 → 退出（原功能不回归）。"""
        ws = _FakeWebServer(active=0, ws_age=1.0)
        manager = _FakeManager(has_active=False)

        async def _go():
            await _wait_ws_autoexit(ws, 0.05, manager)

        start = time.monotonic()
        asyncio.run(_go())
        assert time.monotonic() - start < 1.0  # 立即满足三条件返回

    def test_t2_manager_none_treated_as_no_active_runs(self):
        """``manager=None``（向后兼容）→ 视无并发 run，行为退化到原两条件。"""
        ws = _FakeWebServer(active=0, ws_age=1.0)

        async def _go():
            await _wait_ws_autoexit(ws, 0.05, manager=None)

        start = time.monotonic()
        asyncio.run(_go())
        assert time.monotonic() - start < 1.0

    def test_t1_manager_probe_exception_keeps_blocking_with_warning(
        self, caplog,
    ):
        """SPEC D §7 fail-path：``has_nonterminal_inproc_runs`` 抛异常 → 视为 True（保守不退）+ warning。

        C-1 / MAJOR-1：warning 也走 once-per-streak 节流——多次迭代只 warn 一次（防日志风暴）。
        """
        ws = _FakeWebServer(active=0, ws_age=10.0)

        class _ExplodingManager:
            _runs: dict = {}  # debug log 计数防御 fallback

            def has_nonterminal_inproc_runs(self):
                raise RuntimeError("probe boom")

        async def _go():
            await asyncio.wait_for(
                _wait_ws_autoexit(ws, 0.5, _ExplodingManager()), timeout=3.0,
            )

        with caplog.at_level(logging.DEBUG, logger="orca.iface.cli.commands"):
            with pytest.raises(asyncio.TimeoutError):
                asyncio.run(_go())
        warn_msgs = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING
            and "has_nonterminal_inproc_runs 探测异常" in r.getMessage()
        ]
        assert warn_msgs, "manager 探测异常未记 warning（fail-path 未 loud）"
        # MAJOR-1：3s ≈ 3 轮迭代，warning 必只 1 次（once-per-streak 节流）。
        assert len(warn_msgs) == 1, (
            f"fail-path warning 未节流：{len(warn_msgs)} 条（应 once-per-streak）"
        )


# ── T3：--stay 不调 _wait_ws_autoexit（SPEC §3.1 I-3 / AC-1）───────────────────


class TestStayDoesNotAutoExit:
    """``--stay`` 显式覆盖：进程不自动退，``_wait_ws_autoexit`` 不应被调（I-3）。"""

    def test_stay_branch_does_not_invoke_wait_ws_autoexit(
        self, tmp_path, monkeypatch,
    ):
        """``_serve_and_run_inprocess(stay=True)`` 走 ``await serve_task`` 而非 auto-exit。

        用 mock uvicorn + manager：若 ``_wait_ws_autoexit`` 被调，标记函数会 raise，测试失败。
        """
        # ``wf_path`` fixture 在 tests/iface/cli/conftest，跨目录不可见——本文件内联一个最小 yaml。
        wf_path = tmp_path / "stay.yaml"
        wf_path.write_text(
            'name: stay_wf\ndescription: stay test\nentry: a\nnodes:\n'
            '  - name: a\n    kind: script\n    command: "echo a"\n'
            '    routes:\n      - to: $end\n',
            encoding="utf-8",
        )

        import sys

        from orca.iface.cli.commands import (
            RunConfig,
            _serve_and_run_inprocess,
        )

        class _FakeServer:
            def __init__(self, config):
                self.started = True
                self.should_exit = False

            async def serve(self):
                # stay 路径会 await serve_task，立即 should_exit 让它快速返回。
                self.should_exit = True
                await asyncio.sleep(0)

        class _FakeManager:
            async def start_run(self, *a, **kw):
                return "rid"

            def get_handle(self, run_id):
                return None

            async def shutdown(self):
                pass

        fake_uvicorn_mod = types.ModuleType("uvicorn")
        fake_uvicorn_mod.Config = lambda *a, **kw: None
        fake_uvicorn_mod.Server = _FakeServer
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn_mod)

        fake_web_mod = types.ModuleType("orca.iface.web")
        fake_web_mod.RunManager = lambda **kw: _FakeManager()
        monkeypatch.setitem(sys.modules, "orca.iface.web", fake_web_mod)

        fake_server_mod = types.ModuleType("orca.iface.web.server")
        fake_server_mod.create_app = lambda manager: types.SimpleNamespace(
            state=types.SimpleNamespace(web_server=types.SimpleNamespace(
                last_ws_activity_at=time.monotonic(),
            ))
        )
        monkeypatch.setitem(sys.modules, "orca.iface.web.server", fake_server_mod)
        monkeypatch.setattr(
            "orca.iface.cli.commands._wait_server_started",
            lambda server, timeout: asyncio.sleep(0),
        )

        # 守门：stay 路径不应调 _wait_ws_autoexit。被调 → raise。
        async def _forbidden_autoexit(*a, **kw):
            raise AssertionError("--stay 路径不应调 _wait_ws_autoexit")

        monkeypatch.setattr(
            "orca.iface.cli.commands._wait_ws_autoexit", _forbidden_autoexit,
        )

        async def _run():
            return await _serve_and_run_inprocess(
                RunConfig(yaml_path=wf_path),
                wf=None,
                bind_host="127.0.0.1",
                display_host="127.0.0.1",
                port=12345,
                stay=True,
            )

        rc = asyncio.run(_run())
        # 无 handle → 默认 EXIT_RUN_FAILED，但关键是没抛 AssertionError。
        assert rc in (0, 1)


# ── T7 / T8：finding 4（控制帧 QueueFull warn）───────────────────────────────


class TestHasNonterminalInprocRuns:
    """SPEC D finding 1：``RunManager.has_nonterminal_inproc_runs`` 只读 helper 直接单测。

    主审 🟡：T1 integration 降级为 fake-based unit；为补强，对真 RunManager 加
    ``has_nonterminal_inproc_runs`` 的直接单测——覆盖空 / 有终态 run / 有非终态 run 三态。
    """

    def test_empty_manager_returns_false(self, tmp_path):
        from orca.iface.web.run_manager import RunManager
        m = RunManager(runs_dir=tmp_path / "runs")
        assert m.has_nonterminal_inproc_runs() is False

    def test_returns_true_when_a_non_done_task_exists(self, tmp_path):
        from orca.iface.web.run_manager import RunManager, InProcessRunHandle

        m = RunManager(runs_dir=tmp_path / "runs")

        async def _go():
            task = asyncio.create_task(asyncio.sleep(100))
            try:
                # RunView dataclass 必填 tape，但本测试只关心 isinstance + _task.done()，
                # 故用 ``object.__new__`` 跳过 init 仅设必要属性（避免 Tape 构造开销）。
                handle = object.__new__(InProcessRunHandle)
                handle._task = task
                m._runs["r1"] = handle
                # 在同一 loop 内断言（task 仍 pending）。
                assert m.has_nonterminal_inproc_runs() is True
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(_go())

    def test_returns_false_when_all_tasks_done(self, tmp_path):
        from orca.iface.web.run_manager import RunManager, InProcessRunHandle

        m = RunManager(runs_dir=tmp_path / "runs")

        async def _go():
            task = asyncio.create_task(asyncio.sleep(0))
            await asyncio.sleep(0.05)  # 让 task 完成
            assert task.done() is True
            handle = object.__new__(InProcessRunHandle)
            handle._task = task
            m._runs["r2"] = handle
            assert m.has_nonterminal_inproc_runs() is False

        asyncio.run(_go())


# ── T7 / T8：finding 4（控制帧 QueueFull warn）───────────────────────────────


class _ManagerStub:
    """最小 manager stub：``add_run_changed_listener`` 收 callback（WebServer 构造用）。"""

    def __init__(self):
        self.listeners: list = []

    def add_run_changed_listener(self, cb):
        self.listeners.append(cb)


class TestOnRunChangedQueueFullWarn:
    """SPEC D finding 4 / AC-4：``_on_run_changed`` QueueFull 加 warn + 计数。"""

    def test_t7_queue_full_emits_warning_with_fields(self, caplog):
        """``maxsize=1`` queue 填满后 ``_on_run_changed`` 触发 warning（含 run/action/conn）。"""
        manager = _ManagerStub()
        web_server = WebServer(manager)

        # 构造一条已满 queue 的 fake connection。
        full_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        full_queue.put_nowait({"existing": "frame"})  # 填满

        # 用可哈希的 _Conn 实例（``_connections`` 的 key 需可哈希）。
        class _Conn:
            ws = None
            queue = full_queue
            writer = None
            subscription = None

        conn = _Conn()
        # ``_connections`` 内部 dict（键通常为 WebSocket；测试用任意可哈希对象）。
        web_server._connections[conn] = conn

        with caplog.at_level(logging.WARNING, logger="orca.iface.web.ws_handler"):
            # 触发广播：queue 已满 → QueueFull → warning。
            web_server._on_run_changed("run-r1", "deleted")

        msgs = [r.getMessage() for r in caplog.records]
        assert any("run_changed" in m and "queue full" in m for m in msgs), (
            f"未触发 QueueFull warning：{msgs}"
        )
        # 含字段（run / action / conn）。
        first = next(m for m in msgs if "run_changed" in m)
        assert "run=run-r1" in first
        assert "action=deleted" in first
        assert "conn=" in first  # SPEC §6 AC-4「含 conn id」契约
        # 计数器累加。
        assert web_server.dropped_control_frames >= 1

    def test_t8_grep_all_payload_put_nowait_queuefull_paths_warn(self):
        """T8 grep 守门：所有「帧 payload」put_nowait 的 QueueFull 路径必 warn（None-sentinel 除外）。

        静态扫 ``ws_handler.py``：``put_nowait`` 后紧跟的 ``except asyncio.QueueFull`` 块
        必含 ``logger.warning``（除非紧邻是 ``None`` sentinel + ``writer.cancel`` 兜底）。
        """
        ws_path = (
            Path(__file__).resolve().parents[3]
            / "orca" / "iface" / "web" / "ws_handler.py"
        )
        text = ws_path.read_text(encoding="utf-8")
        lines = text.splitlines()

        # 扫所有 put_nowait 行；对每个找其所属 try 的 except QueueFull 块。
        # 简化：对每个 put_nowait，检查它前后 30 行窗口内是否有 QueueFull + warning。
        violations: list[str] = []
        for i, ln in enumerate(lines):
            if "put_nowait" not in ln:
                continue
            # 是否 None sentinel（紧邻 writer.cancel）？
            window = "\n".join(lines[max(0, i - 2):i + 15])
            is_none_sentinel = "put_nowait(None)" in ln
            if is_none_sentinel:
                # None-sentinel 必须紧邻 writer.cancel（兜底）。
                assert "writer.cancel" in window or "cancel()" in window, (
                    f"None-sentinel put_nowait 缺 writer.cancel 兜底：line {i+1}"
                )
                continue
            # payload put_nowait：窗口内必须有 QueueFull + logger.warning。
            if "QueueFull" not in window:
                violations.append(f"line {i+1}: put_nowait 无 QueueFull 处理")
                continue
            if "logger.warning" not in window:
                violations.append(f"line {i+1}: QueueFull 块缺 logger.warning")
        assert not violations, (
            f"ws_handler.py 帧 payload put_nowait QueueFull 路径有静默丢：\n{violations}"
        )
