"""tests/iface/web/test_run_manager_chart.py —— RunHandle chart ingestor 集成（phase-13 SPEC §3.1）。

覆盖意图（非仅行为）：
  - start_run（非 resume）→ runs/<run_id>.sock 文件存在
  - run 终态后 teardown → socket 文件删除（cancel task + unlink）
  - resume=True 模式 → 不起 ingestor（无 socket 文件，SPEC §3.1 YAGNI 边界）
"""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path
from unittest.mock import patch

from orca.iface.web.run_manager import RunManager
from orca.run.orchestrator import Orchestrator

from tests.iface.web.conftest import demo_linear_yaml, run_async


def _short_runs_dir(tmp_path: Path) -> Path:
    """短路径 runs_dir（避免 macOS /private/var/folders 触发 SOCK_PATH_MAX）。

    SPEC §7.7：sock path > 90 字节 fail loud。pytest tmp_path 在 macOS 是
    /private/var/folders/.../pytest-of-user/pytest-NN/，runs/<run_id>.sock 加上后会超 90。
    用 ``/tmp/orca-tN`` 短前缀（N = tmp_path 的 inode 哈希），既短又不撞名。
    """
    import hashlib
    # tmp_path.path().stat().st_ino 在 macOS APFS 不稳定，用 path 字符串 hash 更可移植
    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:6]
    return Path(f"/tmp/orca-t{h}/runs")


def _wait_sock(sock_path: Path, timeout: float = 5.0) -> bool:
    """同步等 sock 文件就绪（轮询）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sock_path.exists():
            return True
        time.sleep(0.01)
    return False


# ── 非 resume：起 ingestor + 创建 sock 文件 ─────────────────────────────────


def test_start_run_creates_chart_socket_file(tmp_path):
    """非 resume 模式 start_run → runs/<run_id>.sock 存在（ingestor task 起）。

    意图：SPEC §3.1 「RunHandle 启动时 create_task(chart_ingestor)」+ sock 文件由
    asyncio.start_unix_server 创建。验证 socket 在 run 进行中存在。
    """
    runs_dir = _short_runs_dir(tmp_path)
    yaml = demo_linear_yaml(tmp_path)
    manager = RunManager(runs_dir=runs_dir)

    async def go():
        # patch Orchestrator.run 注入 hold，让 run 在 running 态时被断言
        hold = asyncio.Event()

        async def hold_run(self):
            await hold.wait()

        with patch.object(Orchestrator, "run", hold_run):
            run_id = await manager.start_run(str(yaml), {}, None, None)
            await asyncio.sleep(0.05)  # 让 ingestor 起来
            sock = runs_dir / f"{run_id}.sock"
            assert sock.exists(), f"sock 文件未创建: {sock}"
            # socket 应可连（server 在 listen）
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                s.connect(str(sock))  # 不抛即 listen 正常

        hold.set()  # 放行
        await manager.shutdown()

    run_async(go())


# ── run 完成 → teardown → sock 文件删 ────────────────────────────────────────


def test_run_teardown_deletes_chart_socket(tmp_path):
    """run 完成（teardown 触发）→ runs/<run_id>.sock 文件被删。

    意图：SPEC §3.1 「_teardown_handle cancel + unlink」+ SPEC §0.1 #4「socket 是传输通道，
    run 结束删除」。验证无残留（防 stale socket 影响下次同 run_id 启动）。
    """
    runs_dir = _short_runs_dir(tmp_path)
    yaml = demo_linear_yaml(tmp_path)
    manager = RunManager(runs_dir=runs_dir)

    async def go():
        run_id = await manager.start_run(str(yaml), {}, None, None)
        sock = runs_dir / f"{run_id}.sock"
        # 等 run 完成（demo workflow 是 a→b→$end 纯 script，秒级完成）
        await manager.wait_done(run_id, timeout=10.0)
        # shutdown 完成 teardown
        await manager.shutdown()
        assert not sock.exists(), f"sock 文件未删: {sock}"

    run_async(go())


# ── resume 边界：不起 ingestor（SPEC §3.1 YAGNI）────────────────────────────


def test_resume_mode_skips_chart_ingestor(tmp_path):
    """resume=True 模式 → 不创建 sock 文件 + RunHandle._chart_ingestor is None。

    意图：SPEC §3.1 「resume 模式重开 tape 时，RunHandle 不构造 chart ingestor」。
    这是 YAGNI 边界——script 调 render_chart 时 socket 不存在 → fail loud（设计意图）。
    """
    runs_dir = _short_runs_dir(tmp_path)
    yaml = demo_linear_yaml(tmp_path)
    manager = RunManager(runs_dir=runs_dir)

    async def go():
        # patch Orchestrator.run 让 run 不实际执行（无需真 resume tape 内容）
        async def noop(self):
            return

        with patch.object(Orchestrator, "run", noop):
            run_id = await manager.start_run(str(yaml), {}, None, None, resume=True)
            await asyncio.sleep(0.05)  # 等可能的 ingestor 启动（应不启动）
            handle = manager.get_handle(run_id)
            assert handle is not None
            assert handle._chart_ingestor is None, "resume 模式不应起 ingestor"
            sock = runs_dir / f"{run_id}.sock"
            assert not sock.exists(), "resume 模式不应创建 sock 文件"
        await manager.shutdown()

    run_async(go())


# ── cancel_run 也清理 sock（teardown 路径覆盖）──────────────────────────────


def test_cancel_run_deletes_chart_socket(tmp_path):
    """cancel_run 触发 teardown → sock 文件删。

    意图：teardown 路径覆盖正常完成 / cancel 双路径，socket 清理必须幂等。
    """
    runs_dir = _short_runs_dir(tmp_path)
    yaml = demo_linear_yaml(tmp_path)
    manager = RunManager(runs_dir=runs_dir)

    async def go():
        hold = asyncio.Event()

        async def hold_run(self):
            await hold.wait()

        with patch.object(Orchestrator, "run", hold_run):
            run_id = await manager.start_run(str(yaml), {}, None, None)
            await asyncio.sleep(0.05)
            sock = runs_dir / f"{run_id}.sock"
            assert sock.exists()

            ok = await manager.cancel_run(run_id, reason="test")
            assert ok
            assert not sock.exists(), "cancel_run 后 sock 文件应删"

        hold.set()
        await manager.shutdown()

    run_async(go())
