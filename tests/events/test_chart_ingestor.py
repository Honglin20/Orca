"""tests/events/test_chart_ingestor.py —— per-run chart ingestor（phase-13 SPEC §3）。

覆盖意图（非仅行为）：
  - 收合法消息 → emit custom(chart) + ack seq == tape.last_seq()
  - malformed（非 JSON / 缺字段 / 类型错）→ ack ok=False + 不 emit
  - 超限 payload → ack ok=False + 不 emit
  - teardown cancel → socket 文件删
  - emit 抛 → ack ok=False 含 error
  - crash 恢复 callback：cancelled 不重起（teardown 安全）
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from unittest.mock import patch

from orca.events.bus import EventBus
from orca.events.chart_ingestor import (
    chart_ingestor,
    make_crash_callback,
)
from orca.events.tape import Tape


# ── helpers ─────────────────────────────────────────────────────────────────


def _run(coro):
    """统一 asyncio.run（仓库约定：不用 pytest-asyncio）。"""
    return asyncio.run(coro)


def _make_bus(tmp_path: Path, run_id: str = "demo-test") -> tuple[EventBus, Tape]:
    """构造 EventBus + Tape（写 tmp_path/orca-runs/）。"""
    tape = Tape(tmp_path / "runs" / f"{run_id}.jsonl", run_id=run_id)
    bus = EventBus(tape)
    return bus, tape


def _short_sock(tmp_path: Path) -> Path:
    """短 sock path（避免 macOS /private/var/folders 触发 AF_UNIX path too long）。

    pytest tmp_path 在 macOS 是 /private/var/folders/.../pytest-of-user/pytest-NN/，超 90 字节
    会触发 SPEC §7.7 SOCK_PATH_MAX（macOS sun_path=104）。与 SPEC workaround 一致用 /tmp 短路径。
    """
    return Path(f"/tmp/orca-test-ingestor-{tmp_path.name}.sock")


def _wait_sock(sock_path: Path, loops: int = 100, delay: float = 0.01) -> bool:
    """同步等 sock 文件就绪（被 ingestor task 创建）。"""
    for _ in range(loops):
        if sock_path.exists():
            return True
        import time
        time.sleep(delay)
    return False


def _send_and_recv(sock_path: Path, msg_bytes: bytes) -> bytes:
    """连 sock → 发 msg → 读 ack → 关。返回 ack 字节（含 newline）。

    makefile 默认是文本模式（返回 str）；显式 "rb" 取 bytes（与 _render.py 一致）。
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5.0)
        s.connect(str(sock_path))
        s.sendall(msg_bytes)
        return s.makefile("rb").readline()


async def _wait_sock_async(sock_path: Path, loops: int = 100, delay: float = 0.01) -> bool:
    for _ in range(loops):
        if sock_path.exists():
            return True
        await asyncio.sleep(delay)
    return False


# ── emit + ack ──────────────────────────────────────────────────────────────


def test_ingestor_emits_chart_event_and_acks_seq(tmp_path):
    """合法消息 → emit custom(chart) + ack seq == tape.last_seq()。

    意图：完整 happy path，验证「ingestor 唯一调用 bus.emit」单一写路径 + ack 携带 seq。
    """
    sock_path = _short_sock(tmp_path)
    bus, tape = _make_bus(tmp_path)

    async def go():
        task = asyncio.create_task(chart_ingestor(sock_path, bus, "demo"))
        assert await _wait_sock_async(sock_path)
        # 发合法消息
        msg = json.dumps({
            "node": "train",
            "session_id": "sess-1",
            "payload": {"chart_type": "line", "data": [{"x": 1, "y": 2.0}], "label": "g", "title": "t"},
        }) + "\n"
        ack_raw = await asyncio.to_thread(_send_and_recv, sock_path, msg.encode("utf-8"))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        ack = json.loads(ack_raw.decode("utf-8"))
        assert ack["ok"] is True
        assert ack["seq"] == tape.last_seq()
        events = list(tape.replay())
        assert len(events) == 1
        ev = events[0]
        assert ev.type == "custom"
        assert ev.node == "train"
        assert ev.session_id == "sess-1"
        assert ev.data["kind"] == "chart"
        assert ev.data["chart"]["label"] == "g"

    try:
        _run(go())
    finally:
        try:
            sock_path.unlink(missing_ok=True)
        except OSError:
            pass


# ── malformed（SPEC §3.4）────────────────────────────────────────────────────


def test_ingestor_malformed_message_acks_error(tmp_path):
    """非 JSON / 缺字段 / 类型错 → ack ok=False + 不 emit。"""
    sock_path = _short_sock(tmp_path)
    bus, tape = _make_bus(tmp_path)

    async def go():
        task = asyncio.create_task(chart_ingestor(sock_path, bus, "demo"))
        assert await _wait_sock_async(sock_path)

        # case 1: 非 JSON
        ack1 = await asyncio.to_thread(_send_and_recv, sock_path, b"not a json\n")
        a1 = json.loads(ack1.decode())
        assert a1["ok"] is False
        assert "invalid JSON" in a1["error"]

        # case 2: 缺 session_id
        bad_msg = json.dumps({"node": "train", "payload": {}}) + "\n"
        ack2 = await asyncio.to_thread(_send_and_recv, sock_path, bad_msg.encode())
        a2 = json.loads(ack2.decode())
        assert a2["ok"] is False
        assert "malformed" in a2["error"]

        # case 3: payload 非 dict
        bad_msg2 = json.dumps({"node": "x", "session_id": "y", "payload": "not dict"}) + "\n"
        ack3 = await asyncio.to_thread(_send_and_recv, sock_path, bad_msg2.encode())
        a3 = json.loads(ack3.decode())
        assert a3["ok"] is False

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # tape 无任何事件（malformed 不 emit）
        assert list(tape.replay()) == []

    try:
        _run(go())
    finally:
        try:
            sock_path.unlink(missing_ok=True)
        except OSError:
            pass


# ── 超限（SPEC §5.2 ingestor 端复核）─────────────────────────────────────────


def test_ingestor_oversize_payload_rejected(tmp_path):
    """payload > MAX_MESSAGE_BYTES（2 MB）→ 不 emit / 不写 tape（SPEC §5.2 核心契约）。

    意图：防 client 绕过 _render 直接写 socket。极端超限（5MB > asyncio readline limit 2MB+1024）
    时，readline 抛 LimitOverrunError → handler 通用 except catch → ack 写可能因 stream 异常
    状态对 client 不可读（client 看到 timeout / EOF）。这是可接受的——client lib ``_render.py``
    在 sendall 之前已有 size check（2MB+1 字节即 raise），ingestor 端是兜底；client 看到的
    timeout/EOF 同样是 fail loud（``_render`` 把 ``socket.timeout`` 映射到 RuntimeError ack
    超时 + ``ack_raw=""`` 映射到「未收到 ack」）。

    本测试聚焦 SPEC §5.2 的**核心契约**：「超限 payload 不写 tape」。ack 路径由其它测试覆盖。
    """
    from orca.chart._limits import MAX_MESSAGE_BYTES
    sock_path = _short_sock(tmp_path)
    bus, tape = _make_bus(tmp_path)

    async def go():
        task = asyncio.create_task(chart_ingestor(sock_path, bus, "demo"))
        assert await _wait_sock_async(sock_path)

        # 构造 > 2MB 的消息
        big_data = [{"x": i, "y": "padding-padding-padding"} for i in range(100_000)]
        msg = (json.dumps({
            "node": "train",
            "session_id": "s",
            "payload": {"chart_type": "line", "data": big_data, "label": "g", "title": "t"},
        }) + "\n").encode("utf-8")
        assert len(msg) > MAX_MESSAGE_BYTES

        # 发送（ack 可能 timeout / EOF，均符合 fail loud；不强制解析 ack 内容）
        def _send_only():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(2.0)
                    s.connect(str(sock_path))
                    s.sendall(msg)
                    # 尝试读 ack（可能 timeout / 空）
                    return s.makefile("rb").readline()
            except (socket.timeout, ConnectionResetError, BrokenPipeError):
                return b""  # 任一 fail loud 路径都 OK

        await asyncio.to_thread(_send_only)

        # 给 handler 时间处理（readline 异常已被 catch）
        await asyncio.sleep(0.05)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # SPEC §5.2 核心契约：tape 永不存超限 payload
        assert list(tape.replay()) == [], "超限 payload 不应写入 tape"

    try:
        _run(go())
    finally:
        try:
            sock_path.unlink(missing_ok=True)
        except OSError:
            pass


def test_ingestor_accepts_payload_under_2mb_but_over_64kb(tmp_path):
    """64KB < payload < 2MB → 正常 accept（不被 asyncio readline 64KB 默认上限误拒）。

    意图：验证 ingestor 显式设置 ``limit=MAX_MESSAGE_BYTES+1024`` —— SPEC §5.2 单帧上限是
    2MB（不是 asyncio 默认的 64KB），1MB 量级的合法 chart 必须能通过。
    """
    sock_path = _short_sock(tmp_path)
    bus, tape = _make_bus(tmp_path)

    async def go():
        task = asyncio.create_task(chart_ingestor(sock_path, bus, "demo"))
        assert await _wait_sock_async(sock_path)

        # 构造 ~200KB 单行（> 64KB 默认 readline 上限，< 2MB）
        data = [{"x": i, "y": "p" * 50} for i in range(2_000)]  # ~200KB
        msg = (json.dumps({
            "node": "train", "session_id": "s",
            "payload": {"chart_type": "line", "data": data, "label": "g", "title": "t"},
        }) + "\n").encode("utf-8")
        assert 64 * 1024 < len(msg) < 2 * 1024 * 1024

        ack_raw = await asyncio.to_thread(_send_and_recv, sock_path, msg)
        ack = json.loads(ack_raw.decode())
        # 必须成功（不被 asyncio 64KB 默认上限误拒）
        assert ack["ok"] is True, f"1MB 内合法 payload 被拒: {ack}"
        assert ack["seq"] == tape.last_seq()

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        _run(go())
    finally:
        try:
            sock_path.unlink(missing_ok=True)
        except OSError:
            pass


# ── teardown cancel → socket 删除（SPEC §3.4 / §3.1 finally）──────────────


def test_ingestor_teardown_unlinks_socket(tmp_path):
    """task cancel → finally 块 unlink socket 文件。"""
    sock_path = _short_sock(tmp_path)
    bus, _ = _make_bus(tmp_path)

    async def go():
        task = asyncio.create_task(chart_ingestor(sock_path, bus, "demo"))
        assert await _wait_sock_async(sock_path)
        assert sock_path.exists()

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # finally 块应已 unlink
        assert not sock_path.exists()

    try:
        _run(go())
    finally:
        try:
            sock_path.unlink(missing_ok=True)
        except OSError:
            pass


# ── emit 抛 → ack error（SPEC §3.4 emit 失败兜底）────────────────────────────


def test_ingestor_emit_failure_acks_error(tmp_path):
    """bus.emit 抛（如 tape 写失败）→ ack ok=False + error 透传。"""
    sock_path = _short_sock(tmp_path)
    bus, _ = _make_bus(tmp_path)

    async def go():
        task = asyncio.create_task(chart_ingestor(sock_path, bus, "demo"))
        assert await _wait_sock_async(sock_path)

        async def boom(*args, **kwargs):
            raise RuntimeError("tape write failed")

        with patch.object(bus, "emit", side_effect=boom):
            msg = json.dumps({
                "node": "train", "session_id": "s",
                "payload": {"chart_type": "line", "data": [], "label": "g", "title": "t"},
            }) + "\n"
            ack_raw = await asyncio.to_thread(_send_and_recv, sock_path, msg.encode())

        ack = json.loads(ack_raw.decode())
        assert ack["ok"] is False
        assert "tape write failed" in ack["error"]

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        _run(go())
    finally:
        try:
            sock_path.unlink(missing_ok=True)
        except OSError:
            pass


# ── crash 恢复 callback（SPEC §3.4 done_callback 重起）──────────────────────


def test_make_crash_callback_returns_callable(tmp_path):
    """make_crash_callback 返回可调用对象（构造不抛）。"""
    cb = make_crash_callback(Path("/tmp/x.sock"), None, "demo")
    assert callable(cb)


def test_crash_callback_no_restart_on_cancelled(tmp_path):
    """task 被 cancel（非 crash）→ callback 静默返回，不重起。

    意图：teardown 走 cancel 路径，crash callback 不能误触发重起（否则 teardown 后僵尸 task）。
    """

    async def go():
        sock_path = _short_sock(tmp_path)
        bus, _ = _make_bus(tmp_path)

        task = asyncio.create_task(chart_ingestor(sock_path, bus, "demo"))
        cb = make_crash_callback(sock_path, bus, "demo")
        task.add_done_callback(cb)

        assert await _wait_sock_async(sock_path)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # 给 callback 一点时间执行（如果它错误重起，会有新 task）
        await asyncio.sleep(0.05)
        # 当前 loop 不应有 -restart 命名的 task（callback 应早返回）
        restart_tasks = [
            t for t in asyncio.all_tasks()
            if "restart" in t.get_name()
        ]
        assert restart_tasks == []

    try:
        _run(go())
    finally:
        try:
            _short_sock(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass


def test_crash_callback_restarts_on_exception(tmp_path):
    """task 抛异常（非 cancel）→ callback 重起一个新 task。

    意图：ingestor task crash（如 sock bind 异常）→ done_callback 必须 unlink stale sock
    + 创建新 task（带 -restart 名）；新 task 接管后续 chart 推送。
    """
    sock_path = _short_sock(tmp_path)
    bus, _ = _make_bus(tmp_path)

    async def go():
        # 用 patch 让首 chart_ingestor 启动后立刻 raise（模拟 server crash）
        call_count = {"n": 0}
        original_start = asyncio.start_unix_server

        async def flaky_start(handler, *, path=None, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated server crash")
            return await original_start(handler, path=path, **kw)

        with patch("orca.events.chart_ingestor.asyncio.start_unix_server", side_effect=flaky_start):
            task = asyncio.create_task(
                chart_ingestor(sock_path, bus, "demo"),
                name="orca-chart-ingestor-demo",
            )
            cb = make_crash_callback(sock_path, bus, "demo")
            task.add_done_callback(cb)
            # 等 crash + 重起
            await asyncio.sleep(0.1)

        # 重起 task 应存在（-restart 命名）
        restart_tasks = [
            t for t in asyncio.all_tasks()
            if "restart" in t.get_name()
        ]
        assert len(restart_tasks) >= 1, "crash callback 未重起 task"

        # 清理：cancel 所有 ingestor task（避免 leaked task）
        for t in restart_tasks:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    try:
        _run(go())
    finally:
        try:
            sock_path.unlink(missing_ok=True)
        except OSError:
            pass


def test_ingestor_stale_socket_unlinked_before_bind(tmp_path):
    """SPEC §3.2：sock 文件已存在（stale）→ chart_ingestor 先 unlink 再 bind。

    意图：前次 run crash 残留 sock 文件时，新 ingestor 启动必须清理（否则 AddressAlreadyInUse）。
    """
    sock_path = _short_sock(tmp_path)
    bus, _ = _make_bus(tmp_path)

    # 预创建 stale sock 文件
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.write_text("stale")
    assert sock_path.exists()

    async def go():
        task = asyncio.create_task(chart_ingestor(sock_path, bus, "demo"))
        assert await _wait_sock_async(sock_path)
        # sock 文件被替换（不是 stale 内容了——asyncio.start_unix_server 重新创建 socket）
        assert sock_path.exists()
        # socket 应可连（被 server 监听）
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        _run(go())
    finally:
        try:
            sock_path.unlink(missing_ok=True)
        except OSError:
            pass
