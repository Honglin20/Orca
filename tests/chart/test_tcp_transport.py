"""tests/chart/test_tcp_transport.py —— chart 传输层 TCP 分支（spec 2026-09-08 D1/D2）。

覆盖意图（非仅行为）：
  - TCP loopback 闭环：ingestor win32 分支（``_IS_WINDOWS`` 可 patch）bind 临时端口 +
    写 ``.port`` sidecar → client（``_render._connect_endpoint`` 的 tcp 分支）连上、
    发消息、收到 ack ok=True seq —— 协议字节级不变（单行 JSON + ack，短连接）。
  - teardown cancel → port 文件删（finally 清理）。
  - ``chart_endpoint`` 在 win32 分支下从 port 文件解析出 ``tcp://127.0.0.1:<port>``。
  - ``_connect_endpoint`` fail loud：非法 tcp 串 → RuntimeError（不静默猜）。

平台无关设计：在 Linux/WSL 上运行时把 ``orca.chart._paths.IS_WINDOWS`` 与
``orca.events.chart_ingestor._IS_WINDOWS`` 同时 patch 成 True（模拟 win32 路径分支），
client 用 ``_render._connect_endpoint`` 的 tcp 分支（AF_INET loopback 全平台可用）。
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

import orca.chart._paths as paths_mod
import orca.events.chart_ingestor as chart_ingestor_mod
from orca.chart._paths import chart_endpoint, chart_port_file_path
from orca.chart._render import _connect_endpoint
from orca.events.bus import EventBus
from orca.events.tape import Tape


def _run(coro):
    """统一 asyncio.run（仓库约定：不用 pytest-asyncio）。"""
    return asyncio.run(coro)


def _make_bus(tmp_path: Path, run_id: str = "tcp-demo") -> tuple[EventBus, Tape]:
    tape = Tape(tmp_path / "runs" / f"{run_id}.jsonl", run_id=run_id)
    bus = EventBus(tape)
    return bus, tape


async def _wait_port_file(port_file: Path, loops: int = 200, delay: float = 0.01) -> bool:
    """等 ingestor bind 完成写出 port sidecar。"""
    for _ in range(loops):
        if port_file.exists():
            return True
        await asyncio.sleep(delay)
    return False


def test_tcp_loopback_closed_loop_send_and_ack(tmp_path):
    """win32 TCP 分支端到端：bind → port sidecar → client tcp:// 连上 → 发 → ack seq。

    意图：D1 的核心契约——传输换成 TCP 后**协议字节级不变**（单行 UTF-8 JSON + 单行
    ack + 短连接），且端点发现（port 文件）与 client 分支（``tcp://`` 前缀）闭环。
    这是 Windows 上 chart 推送的唯一通道，等价于 POSIX 侧
    ``test_ingestor_emits_chart_event_and_acks_seq``。
    """
    sock_path = tmp_path / "orca-test.sock"
    port_file = chart_port_file_path(sock_path)
    bus, tape = _make_bus(tmp_path)

    async def go():
        with (
            patch.object(paths_mod, "IS_WINDOWS", True),
            patch.object(chart_ingestor_mod, "_IS_WINDOWS", True),
        ):
            task = asyncio.create_task(
                chart_ingestor_mod.chart_ingestor(sock_path, bus, "tcp-demo")
            )
            assert await _wait_port_file(port_file), "ingestor 未写出 port sidecar"

            endpoint = chart_endpoint(sock_path)
            assert endpoint.startswith("tcp://127.0.0.1:"), endpoint
            port = int(endpoint.rsplit(":", 1)[1])
            assert 0 < port < 65536

            msg = (json.dumps({
                "node": "train",
                "session_id": "sess-1",
                "payload": {
                    "chart_type": "line",
                    "data": [{"x": 1, "y": 2.0}],
                    "label": "g",
                    "title": "t",
                },
            }) + "\n").encode("utf-8")

            def send():
                with _connect_endpoint(endpoint) as s:
                    s.sendall(msg)
                    return s.makefile("rb").readline()

            ack_raw = await asyncio.to_thread(send)

            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        ack = json.loads(ack_raw.decode("utf-8"))
        assert ack["ok"] is True, ack
        assert ack["seq"] == tape.last_seq()
        events = list(tape.replay())
        assert len(events) == 1
        assert events[0].type == "custom"
        assert events[0].data["kind"] == "chart"

    _run(go())


def test_tcp_branch_teardown_unlinks_port_file(tmp_path):
    """win32 分支 teardown cancel → finally 删 ``.port`` sidecar（无端点残留）。"""
    sock_path = tmp_path / "orca-test-teardown.sock"
    port_file = chart_port_file_path(sock_path)
    bus, _ = _make_bus(tmp_path)

    async def go():
        with (
            patch.object(paths_mod, "IS_WINDOWS", True),
            patch.object(chart_ingestor_mod, "_IS_WINDOWS", True),
        ):
            task = asyncio.create_task(
                chart_ingestor_mod.chart_ingestor(sock_path, bus, "tcp-demo")
            )
            assert await _wait_port_file(port_file)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert not port_file.exists(), "cancel 后 port sidecar 应被 finally 删除"

    _run(go())


# ── _connect_endpoint fail loud（D2：平台守卫 + 端点形态校验）─────────────────


def test_connect_endpoint_rejects_malformed_tcp_addr():
    """tcp:// 端点缺 host / 端口非数字 → RuntimeError（不静默猜默认值）。"""
    with pytest.raises(RuntimeError, match="tcp 端点非法"):
        _connect_endpoint("tcp://:9999")
    with pytest.raises(RuntimeError, match="tcp 端点非法"):
        _connect_endpoint("tcp://127.0.0.1:notaport")


def test_connect_endpoint_refused_raises_connection_error():
    """tcp:// 指向无监听者端口 → ConnectionRefusedError（由调用方映射 §7.3 fail loud）。

    意图：TCP 分支的连接失败语义与 Unix 分支一致（OSError 子类，``render_chart``
    既有 except 分支零改动）。
    """
    # 找一个确定无监听的端口：bind 一个 socket 记下端口再关掉（TIME_WAIT 后内核
    # 不会立刻重派发给新 bind 的 listener；短窗口内 connect 必 refused）。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises((ConnectionRefusedError, ConnectionResetError, OSError)):
        _connect_endpoint(f"tcp://127.0.0.1:{port}")
