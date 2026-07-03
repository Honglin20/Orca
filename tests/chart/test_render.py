"""tests/chart/test_render.py —— render_chart 主流程（phase-13 SPEC §4.2 / §7 fail loud）。

覆盖意图（非仅行为）：
  - env 全 → mock socket → 验证发送消息正确 + ack seq 返回
  - env 缺（任一 ORCA_*）→ raise RuntimeError（SPEC §7.1）
  - sock path 过长 → raise（SPEC §7.7）
  - payload 校验失败 → raise ValueError（SPEC §7.2）
  - 大小超限 → raise ValueError（SPEC §5.2）
  - socket 不存在 → raise（SPEC §7.3）
  - ack timeout → raise（SPEC §7.6）
  - socket 关闭无 ack → raise
  - ack ok=False → raise（SPEC §7.4）
  - ack 缺 seq → raise
  - 降采样触发：data > max_points 时调用 downsample（stderr 写 warning）
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from orca.chart import render_chart
from orca.chart._limits import (
    ACK_TIMEOUT_SECONDS,
    DEFAULT_MAX_POINTS,
    MAX_MESSAGE_BYTES,
    SOCK_PATH_MAX,
)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def orca_env(monkeypatch, tmp_path):
    """注入完整 ORCA_* env，sock 用短路径（macOS pytest tmp_path 在 /private/var 太长）。

    monkeypatch 后 teardown 自动清理（不污染真实 /tmp）；sock 路径仅用于 mock 接管，不会真连。
    """
    # 用 tmp_path 内的 basename + 短 prefix，避免 macOS /private/var/folders 触发 SOCK_PATH_MAX。
    # 选 /tmp/ 是因为 macOS sun_path=104，留给 run_id + .sock 后还 < 90。
    sock = f"/tmp/orca-test-{tmp_path.name}.sock"
    monkeypatch.setenv("ORCA_RUN_ID", "demo-abc")
    monkeypatch.setenv("ORCA_NODE", "train")
    monkeypatch.setenv("ORCA_SESSION_ID", "sess-xyz")
    monkeypatch.setenv("ORCA_CHART_SOCK", sock)
    # teardown 时清理（即使 mock 没真创建，残留也不应污染下次跑）
    yield {
        "run_id": "demo-abc",
        "node": "train",
        "session_id": "sess-xyz",
        "sock": sock,
    }
    import os as _os
    try:
        _os.unlink(sock)
    except FileNotFoundError:
        pass


def _make_socket_mock(ack_response: bytes | None = b'{"ok": true, "seq": 42}\n'):
    """构造 mock socket：返回固定 ack 字节。``ack_response=None`` 模拟 socket EOF。

    makefile mock 支持 ``with s.makefile("rb") as f`` context manager（``__enter__`` 返回
    自身，``__exit__`` 返回 False），与真 socket.makefile 行为一致。
    """
    fam, type_ = __import__("socket").AF_UNIX, __import__("socket").SOCK_STREAM
    mock = MagicMock()
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    makefile_mock = MagicMock()
    # context manager 支持（_render.py 用 ``with s.makefile("rb") as f:``）
    makefile_mock.__enter__.return_value = makefile_mock
    makefile_mock.__exit__.return_value = False
    if ack_response is None:
        makefile_mock.readline.return_value = b""
    else:
        makefile_mock.readline.return_value = ack_response
    mock.makefile.return_value = makefile_mock
    return mock, (fam, type_)


# ── 成功路径 ─────────────────────────────────────────────────────────────────


def test_render_chart_success_returns_seq(orca_env):
    """env 全 + mock socket + ack ok=True seq=42 → 返回 42。

    意图：完整 happy path 验证（env 读 + payload + encoded + socket + ack 解析）。
    """
    sock_mock, _ = _make_socket_mock(b'{"ok": true, "seq": 42}\n')
    with patch("orca.chart._render.socket.socket", return_value=sock_mock):
        seq = render_chart(
            chart_type="line",
            data=[{"x": 1, "y": 1.0}],
            label="g1",
            title="t1",
        )
    assert seq == 42

    # 验证 sendall 收到的内容含身份 + payload
    sent_bytes = sock_mock.sendall.call_args[0][0]
    sent = json.loads(sent_bytes.decode("utf-8"))
    assert sent["node"] == "train"
    assert sent["session_id"] == "sess-xyz"
    assert sent["payload"]["chart_type"] == "line"
    assert sent["payload"]["label"] == "g1"
    assert sent["payload"]["data"] == [{"x": 1, "y": 1.0}]


def test_render_chart_settimeout_called(orca_env):
    """socket.settimeout(ACK_TIMEOUT_SECONDS) 必被调（防 client 无 timeout 挂死）。"""
    sock_mock, _ = _make_socket_mock()
    with patch("orca.chart._render.socket.socket", return_value=sock_mock):
        render_chart(chart_type="line", data=[], label="g", title="t")
    sock_mock.settimeout.assert_called_once_with(ACK_TIMEOUT_SECONDS)


# ── env 缺失（SPEC §7.1）─────────────────────────────────────────────────────


def test_render_env_missing_run_id_raises(orca_env, monkeypatch):
    """ORCA_RUN_ID 缺 → raise RuntimeError，错误信息明示缺失变量。"""
    monkeypatch.delenv("ORCA_RUN_ID", raising=False)
    with pytest.raises(RuntimeError, match="ORCA_RUN_ID"):
        render_chart(chart_type="line", data=[], label="g", title="t")


def test_render_env_missing_node_raises(orca_env, monkeypatch):
    """ORCA_NODE 缺 → raise。"""
    monkeypatch.delenv("ORCA_NODE", raising=False)
    with pytest.raises(RuntimeError, match="ORCA_NODE"):
        render_chart(chart_type="line", data=[], label="g", title="t")


def test_render_env_missing_session_id_raises(orca_env, monkeypatch):
    """ORCA_SESSION_ID 缺 → raise。"""
    monkeypatch.delenv("ORCA_SESSION_ID", raising=False)
    with pytest.raises(RuntimeError, match="ORCA_SESSION_ID"):
        render_chart(chart_type="line", data=[], label="g", title="t")


def test_render_env_missing_sock_raises(orca_env, monkeypatch):
    """ORCA_CHART_SOCK 缺 → raise。"""
    monkeypatch.delenv("ORCA_CHART_SOCK", raising=False)
    with pytest.raises(RuntimeError, match="ORCA_CHART_SOCK"):
        render_chart(chart_type="line", data=[], label="g", title="t")


def test_render_env_all_missing_raises(orca_env, monkeypatch):
    """全部 ORCA_* 缺（用户直接 python foo.py 跑）→ raise，错误信息列全部缺失。"""
    for k in ("ORCA_RUN_ID", "ORCA_NODE", "ORCA_SESSION_ID", "ORCA_CHART_SOCK"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError) as exc_info:
        render_chart(chart_type="line", data=[], label="g", title="t")
    msg = str(exc_info.value)
    for k in ("ORCA_RUN_ID", "ORCA_NODE", "ORCA_SESSION_ID", "ORCA_CHART_SOCK"):
        assert k in msg


# ── sock path 过长（SPEC §7.7）───────────────────────────────────────────────


def test_render_sock_path_too_long_raises(orca_env, monkeypatch):
    """sock_path 长度 > SOCK_PATH_MAX（90）→ raise RuntimeError + workaround 提示。"""
    long_path = "/" + "x" * SOCK_PATH_MAX  # 必 > 90 字节
    monkeypatch.setenv("ORCA_CHART_SOCK", long_path)
    with pytest.raises(RuntimeError, match="socket path 过长"):
        render_chart(chart_type="line", data=[], label="g", title="t")


# ── payload 校验失败（SPEC §7.2）─────────────────────────────────────────────


def test_render_unknown_chart_type_raises_validation_error(orca_env):
    """未知 chart_type → ValueError（validate_payload fail loud）。"""
    with pytest.raises(ValueError, match="未知 chart_type"):
        render_chart(chart_type="heatmap", data=[], label="g", title="t")


def test_render_empty_label_raises_validation_error(orca_env):
    """空 label → ValueError。"""
    with pytest.raises(ValueError, match="label 必须非空"):
        render_chart(chart_type="line", data=[], label="", title="t")


# ── 大小超限（SPEC §5.2）─────────────────────────────────────────────────────


def test_render_oversize_payload_raises(orca_env):
    """post-downsample 仍 > MAX_MESSAGE_BYTES（2 MB）→ raise ValueError。

    意图：fixture 行数极大且 max_points 也极大 → 降采样不触发，但 encoded 字节超 2MB → 必 raise。
    验证「tape 永不存超限 payload」铁律。
    """
    # 每行 ~100 字节；50k 行 → ~5 MB encoded > 2 MB
    big_data = [{"x": i, "y": float(i), "label": f"row-{i}-padding-padding-padding"} for i in range(50_000)]
    with pytest.raises(ValueError, match="chart payload 过大"):
        render_chart(
            chart_type="line",
            data=big_data,
            label="g",
            title="t",
            max_points=100_000,  # 故意大，让降采样不触发
        )


# ── socket 不可达（SPEC §7.3）────────────────────────────────────────────────


def test_render_socket_not_found_raises(orca_env):
    """sock 文件不存在（Orca 进程未启 / run 已结束）→ FileNotFoundError → RuntimeError。"""
    import socket as _socket
    sock_mock = MagicMock()
    sock_mock.__enter__.return_value = sock_mock
    sock_mock.__exit__.return_value = False
    sock_mock.connect.side_effect = FileNotFoundError()
    with patch("orca.chart._render.socket.socket", return_value=sock_mock):
        with pytest.raises(RuntimeError, match="无法连接 Orca chart socket"):
            render_chart(chart_type="line", data=[], label="g", title="t")


def test_render_socket_connection_refused_raises(orca_env):
    """sock 文件存在但 server 未 listen → ConnectionRefusedError → RuntimeError。"""
    sock_mock = MagicMock()
    sock_mock.__enter__.return_value = sock_mock
    sock_mock.__exit__.return_value = False
    sock_mock.connect.side_effect = ConnectionRefusedError()
    with patch("orca.chart._render.socket.socket", return_value=sock_mock):
        with pytest.raises(RuntimeError, match="无法连接 Orca chart socket"):
            render_chart(chart_type="line", data=[], label="g", title="t")


# ── ack timeout（SPEC §7.6）──────────────────────────────────────────────────


def test_render_ack_timeout_raises(orca_env):
    """socket.timeout 异常 → RuntimeError（10s 后 fail loud，防 script 挂死）。"""
    import socket as _socket
    sock_mock = MagicMock()
    sock_mock.__enter__.return_value = sock_mock
    sock_mock.__exit__.return_value = False
    sock_mock.connect.return_value = None
    sock_mock.sendall.return_value = None
    makefile_mock = MagicMock()
    # context manager 支持（_render.py 用 ``with s.makefile("rb") as f:``）
    makefile_mock.__enter__.return_value = makefile_mock
    makefile_mock.__exit__.return_value = False
    makefile_mock.readline.side_effect = _socket.timeout()
    sock_mock.makefile.return_value = makefile_mock

    with patch("orca.chart._render.socket.socket", return_value=sock_mock):
        with pytest.raises(RuntimeError, match="ack 超时"):
            render_chart(chart_type="line", data=[], label="g", title="t")


# ── socket 关闭无 ack（ingestor crash 重起窗口期）────────────────────────────


def test_render_socket_closed_no_ack_raises(orca_env):
    """readline 返回 b""（EOF）→ socket 关闭无 ack → RuntimeError。

    意图：ingestor crash 后 done_callback 重起中的 ~ms 窗口，script 收 EOF 而非 timeout。
    """
    sock_mock, _ = _make_socket_mock(ack_response=None)
    with patch("orca.chart._render.socket.socket", return_value=sock_mock):
        with pytest.raises(RuntimeError, match="未收到 ack"):
            render_chart(chart_type="line", data=[], label="g", title="t")


# ── ack ok=False（SPEC §7.4）─────────────────────────────────────────────────


def test_render_ack_not_ok_raises(orca_env):
    """ack ok=False → RuntimeError 含 error 字段。"""
    sock_mock, _ = _make_socket_mock(b'{"ok": false, "error": "malformed message"}\n')
    with patch("orca.chart._render.socket.socket", return_value=sock_mock):
        with pytest.raises(RuntimeError, match="Orca 拒收 chart"):
            render_chart(chart_type="line", data=[], label="g", title="t")


def test_render_ack_invalid_json_raises(orca_env):
    """ack 非 JSON → raise（防 server 端协议错被静默）。"""
    sock_mock, _ = _make_socket_mock(b"not a json\n")
    with patch("orca.chart._render.socket.socket", return_value=sock_mock):
        with pytest.raises(RuntimeError, match="ack 非 JSON"):
            render_chart(chart_type="line", data=[], label="g", title="t")


def test_render_ack_missing_seq_raises(orca_env):
    """ack ok=True 但无 seq 字段 → raise（防协议错）。"""
    sock_mock, _ = _make_socket_mock(b'{"ok": true}\n')  # 缺 seq
    with patch("orca.chart._render.socket.socket", return_value=sock_mock):
        with pytest.raises(RuntimeError, match="缺 seq"):
            render_chart(chart_type="line", data=[], label="g", title="t")


# ── 降采样触发 ───────────────────────────────────────────────────────────────


def test_render_triggers_downsample_when_data_exceeds_max_points(orca_env, capsys):
    """data 行数 > max_points → 调 downsample + 写 stderr warning（透明降采样）。"""
    data = [{"x": i, "y": float(i)} for i in range(100)]
    sock_mock, _ = _make_socket_mock(b'{"ok": true, "seq": 1}\n')
    with patch("orca.chart._render.socket.socket", return_value=sock_mock):
        render_chart(chart_type="line", data=data, label="g", title="t", max_points=10)
    # stderr 含 warning（透明降采样可见）
    err = capsys.readouterr().err
    assert "降采样" in err or "downsample" in err.lower()
    # sendall 收到的 data 行数 ≤ 10（line 无 hue 分桶 → ≤ 10 行）
    sent_bytes = sock_mock.sendall.call_args[0][0]
    sent = json.loads(sent_bytes.decode("utf-8"))
    assert len(sent["payload"]["data"]) <= 10


def test_render_default_max_points_is_2000():
    """SPEC §5.1 默认 max_points=2000。"""
    assert DEFAULT_MAX_POINTS == 2000


def test_render_max_message_bytes_is_2mb():
    """SPEC §5.2 MAX_MESSAGE_BYTES = 2 MB。"""
    assert MAX_MESSAGE_BYTES == 2 * 1024 * 1024


def test_render_sock_path_max_is_90():
    """SPEC §7.7 SOCK_PATH_MAX = 90。"""
    assert SOCK_PATH_MAX == 90
