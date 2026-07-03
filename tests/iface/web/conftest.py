"""tests/iface/web/conftest.py —— web 后端测试共享 fixtures + helpers。

约定（同 tests/run/conftest.py）：本仓库不用 pytest-asyncio，异步统一 ``asyncio.run``。
``run_async`` / ``make_manager`` / ``demo_yaml`` 在本文件定义，被同包测试引用。
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from orca.iface.web.run_manager import RunManager
from orca.iface.web.server import create_app


def run_async(coro):
    """统一 asyncio.run（无 pytest-asyncio）。"""
    return asyncio.run(coro)


def demo_linear_yaml(tmp_path: Path) -> Path:
    """最小线性纯 script workflow（a→b→$end，零 token，零 claude 依赖）。

    用本文件现造（不依赖 examples/），保证测试自包含。
    """
    p = tmp_path / "demo.yaml"
    p.write_text(
        """
name: demo
description: 线性纯 script demo（测试用）
entry: a
nodes:
  - name: a
    kind: script
    command: "echo step_a"
    routes:
      - to: b
  - name: b
    kind: script
    command: "echo step_b"
    routes:
      - to: $end
outputs:
  result: "{{ b.output.stdout }}"
""",
        encoding="utf-8",
    )
    return p


def make_manager(tmp_path: Path, max_concurrent: int = 3) -> RunManager:
    """构造 RunManager（runs_dir 写短路径，避免 macOS tmp_path 触发 SOCK_PATH_MAX）。

    phase-13 §7.7：sock path > 90 字节 fail loud（RunManager.start_run 启动 ingestor 前
    check）。pytest tmp_path 在 macOS 是 /private/var/folders/.../pytest-of-user/pytest-NN/，
    加上 runs/<run_id>.sock 后超 90，触发 RuntimeError。与 SPEC workaround 一致用 /tmp 短前缀
    （哈希后缀避免并发测试撞名）。
    """
    import hashlib
    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:6]
    return RunManager(max_concurrent=max_concurrent, runs_dir=Path(f"/tmp/orca-t{h}/runs"))


@pytest.fixture
def manager(tmp_path: Path) -> RunManager:
    """默认 RunManager fixture（max_concurrent=3，runs 写短路径）。"""
    return make_manager(tmp_path)


@pytest.fixture
def yaml_path(tmp_path: Path) -> Path:
    """demo workflow yaml 路径（纯 script，零 claude）。"""
    return demo_linear_yaml(tmp_path)


def free_port() -> int:
    """让 OS 分配一个空闲端口（bind 后读 sockname）。供 live_server 用。"""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path: Path):
    """启动真 uvicorn server（同进程，daemon 线程跑），yield ``(base_url, manager)``。

    供所有 playwright/integration 测试共用（DRY：原 4 个测试文件各抄一份）。
    server 在独立 daemon 线程 + 自己的 asyncio loop 里跑 ``server.serve()``；主线程**不能**
    对该 loop ``run_until_complete``（已 running）—— 改轮询端口等 accept 就绪。teardown
    ``should_exit`` + ``join`` + ``manager.shutdown``（loop 已停，可 run_until_complete）。
    """
    import uvicorn

    # phase-13 §7.7：用短路径避免 macOS tmp_path 触发 SOCK_PATH_MAX（同 make_manager）
    import hashlib
    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:6]
    manager = RunManager(runs_dir=Path(f"/tmp/orca-t{h}/runs"))
    app = create_app(manager)
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    loop = asyncio.new_event_loop()

    def _serve() -> None:
        loop.run_until_complete(server.serve())

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    base_url = f"http://127.0.0.1:{port}"
    yield base_url, manager
    server.should_exit = True
    thread.join(timeout=5.0)
    loop.run_until_complete(manager.shutdown())
    loop.close()
