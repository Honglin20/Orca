"""tests/chart/test_sock_path_length.py —— socket 路径长度检查（phase-13 SPEC §7.7）。

SPEC §7.7：sock path 长度 > SOCK_PATH_MAX（90 字节）→ fail loud（macOS sun_path=104 /
Linux 108，留余量取 90）。

覆盖意图（非仅行为）：
  - ``render_chart`` 端：sock_path > 90 → RuntimeError（建议用户改 ORCA_RUNS_DIR）
  - ``RunManager.start_run`` 端：resolved sock path > 90 → RuntimeError（避免 ingestor
    crash + callback 无限重起循环）
  - 90 字节刚好边界：≤ 90 通过；91+ raise
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orca.chart import render_chart
from orca.chart._limits import SOCK_PATH_MAX
from orca.iface.web.run_manager import RunManager


# ── client lib 端（_render.py）───────────────────────────────────────────────


def test_render_chart_rejects_long_sock_path(monkeypatch):
    """render_chart 端：ORCA_CHART_SOCK > SOCK_PATH_MAX（90）→ RuntimeError。

    意图：SPEC §7.7 client lib 在连 socket 前先查长度，fail loud 给出 workaround 建议。
    """
    monkeypatch.setenv("ORCA_RUN_ID", "demo-1")
    monkeypatch.setenv("ORCA_NODE", "train")
    monkeypatch.setenv("ORCA_SESSION_ID", "sess-1")
    long_sock = "/" + "x" * (SOCK_PATH_MAX + 5)  # 95 字节
    monkeypatch.setenv("ORCA_CHART_SOCK", long_sock)

    with pytest.raises(RuntimeError, match="socket path 过长"):
        render_chart(chart_type="line", data=[], label="g", title="t")


def test_render_chart_accepts_max_boundary_sock_path(monkeypatch):
    """render_chart 端：ORCA_CHART_SOCK 长度恰好 = SOCK_PATH_MAX（90）→ 不 raise（边界通过）。

    意图：边界条件——90 字节允许，91 字节 raise。防误把 ≤ 当 <。
    """
    monkeypatch.setenv("ORCA_RUN_ID", "demo-1")
    monkeypatch.setenv("ORCA_NODE", "train")
    monkeypatch.setenv("ORCA_SESSION_ID", "sess-1")
    # 构造恰好 90 字节的 path：/tmp/orca/...（前缀 9 + 80 字符 = 90）
    boundary_sock = "/tmp/orca/" + "x" * (SOCK_PATH_MAX - len("/tmp/orca/"))
    assert len(boundary_sock) == SOCK_PATH_MAX
    monkeypatch.setenv("ORCA_CHART_SOCK", boundary_sock)

    # mock socket（不会真连），验证长度检查通过、后续步骤正常
    sock_mock = MagicMock()
    sock_mock.__enter__.return_value = sock_mock
    sock_mock.__exit__.return_value = False
    makefile_mock = MagicMock()
    # context manager 支持（_render.py 用 ``with s.makefile("rb") as f:``）
    makefile_mock.__enter__.return_value = makefile_mock
    makefile_mock.__exit__.return_value = False
    makefile_mock.readline.return_value = b'{"ok": true, "seq": 1}\n'
    sock_mock.makefile.return_value = makefile_mock
    with patch("orca.chart._render.socket.socket", return_value=sock_mock):
        seq = render_chart(chart_type="line", data=[], label="g", title="t")
    assert seq == 1


# ── RunManager 端（ingestor 启动前）─────────────────────────────────────────


def test_run_manager_accepts_deep_runs_dir(tmp_path):
    """RunManager.start_run：深 runs_dir **不再** RuntimeError（2026-07-08 短路径化）。

    意图回归：socket 现走 ``<tmp>/orca-<hash>.sock``（``chart_sock_path``），与 runs 目录
    解耦——曾经"深服务器 runs 目录 → sun_path 超限 → fail loud"的痛点（用户报的 NGA 98 字节
    案例）已根治。tape/jsonl 仍在深 runs 目录，只有 socket 移到系统 temp 短路径。
    故即便 runs_dir 极深，start_run 也不再因 socket 路径 raise。
    """
    deep_dir = tmp_path / ("a" * 80) / ("b" * 30) / "runs"
    manager = RunManager(runs_dir=deep_dir)

    from tests.iface.web.conftest import demo_linear_yaml
    yaml = demo_linear_yaml(tmp_path)

    async def go():
        # 不再 raise；run 正常起（socket 在 /tmp，长度恒短）。
        run_id = await manager.start_run(str(yaml), {}, None, None)
        assert run_id is not None
        await manager.shutdown()

    import asyncio
    asyncio.run(go())


def test_chart_sock_path_short_and_deterministic():
    """``chart_sock_path(run_id)``：短（< SOCK_PATH_MAX）+ 确定性（同 run_id 同路径）+
    在系统 temp 目录下（与 runs 解耦）。两端（RunManager bind / executor env）同源。"""
    from orca.chart._paths import chart_sock_path

    p1 = chart_sock_path("demo_task-20260708-205425-6a9ea6")
    p2 = chart_sock_path("demo_task-20260708-205425-6a9ea6")
    p3 = chart_sock_path("another-run-id-20260708")
    assert p1 == p2, "同 run_id 必须确定性同路径（两端寻址一致）"
    assert p1 != p3, "不同 run_id 不同路径"
    assert p1.parent == p1.parent, "在 temp 目录"
    import tempfile
    from pathlib import Path
    assert p1.parent == Path(tempfile.gettempdir()), "socket 必在系统 temp 目录"
    assert p1.name.startswith("orca-") and p1.suffix == ".sock"
    assert len(str(p1)) < SOCK_PATH_MAX, "路径恒短（temp + 10 hex）"


def test_sock_path_max_constant_is_90():
    """SPEC §7.7 SOCK_PATH_MAX = 90（常量回归）。"""
    assert SOCK_PATH_MAX == 90
