"""tests/iface/in_session/conftest.py —— in-session 测试公共 fixture。

**chart 守护清理**（phase-13 §3 in-session 衔接引入）：``cli.bootstrap`` 现会 detach 起
``chart_daemon`` 守护进程，**脱离 bootstrap CLI 存活**到 run 终态或 6h TTL。本目录既存测试
（``test_in_session_cli.py`` 等）大量调 bootstrap 但不推进 workflow 到终态 → 守护会跑到 TTL，
单测套件跑完前积累大量泄漏进程（撑 /tmp、占 subprocess slot、CI 内存超）。

本 autouse fixture 在每个测试后**仅清理** cwd 落在该测试 ``tmp_path** 下的守护进程（/proc
遍历，读 ``/proc/<pid>/cmdline`` 过滤 ``chart_daemon`` + ``/proc/<pid>/cwd`` readlink 比对）。
按 cwd 而非命令行参数匹配：``--tape`` 是相对路径（daemon 继承测试 cwd），cmdline 不含
tmp_path；但 cwd symlink 解析后即测试 tmp_path，唯一标识本测试的守护。不影响并发其它测试
（pytest-xdist 各 worker 独立 tmp_path）。
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _cleanup_chart_daemons_after_test(tmp_path: Path):
    """每个测试后：kill cwd 落在本测试 tmp_path 下的 chart_daemon 守护进程。

    best-effort：``/proc`` 不存在（非 Linux）或权限不足时静默跳过 —— 守护的强契约（终态自退
    + 6h TTL 兜底）由生产代码 + 集成测试守，此 fixture 仅卫生（防单测套件积累泄漏）。
    """
    yield
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return  # 非 Linux：跳过（项目 POSIX-only，CI 必 Linux）
    my_tmp = str(tmp_path)
    my_pid = os.getpid()
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        if pid == my_pid:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode(
                "utf-8", errors="replace"
            )
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if "orca.iface.in_session.chart_daemon" not in cmdline:
            continue
        try:
            cwd_target = (entry / "cwd").resolve(strict=False)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if not str(cwd_target).startswith(my_tmp):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
