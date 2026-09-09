"""tests/chart/test_chart_endpoint.py —— 端点解析单一真相源（spec 2026-09-08 D2）。

覆盖意图（非仅行为）：
  - ``chart_port_file_path``：``<sock>.sock`` → ``<sock>.port``（同 tmp 同 stem 派生）。
  - ``chart_endpoint`` POSIX 分支：返回 resolved socket 路径串（现状零改动）。
  - ``chart_endpoint`` win32 分支（``IS_WINDOWS`` 可 patch）：port 文件在 →
    ``tcp://127.0.0.1:<port>``；缺失 → ``""``；损坏（非数字）→ ``""``
    （不注 env → script 端既有「缺 ORCA_CHART_SOCK」fail loud）。
  - ``write_chart_port_file``：原子写内容 + tmp 名带 pid（双 daemon 写者互不踩）+
    ``os.replace`` PermissionError 重试 1 次后成功 / 仍失败 raise（fail loud）。
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import orca.chart._paths as paths_mod
from orca.chart._paths import (
    chart_endpoint,
    chart_port_file_path,
    write_chart_port_file,
)


# ── 路径派生 ─────────────────────────────────────────────────────────────────


def test_chart_port_file_path_derives_from_sock():
    """``<tmp>/orca-<hash>.sock`` → ``<tmp>/orca-<hash>.port``（同目录换后缀）。"""
    assert chart_port_file_path(Path("/tmp/orca-abc123.sock")) == Path(
        "/tmp/orca-abc123.port"
    )
    # str 入参也接受（liveness 等调用方可能传 str）。
    assert chart_port_file_path("/tmp/orca-abc123.sock") == Path("/tmp/orca-abc123.port")


def test_chart_endpoint_posix_returns_resolved_sock_path(tmp_path):
    """POSIX 分支：端点 = socket 文件 resolve 后的绝对路径（现状语义零改动）。

    patch ``IS_WINDOWS=False`` 保证两平台都走 POSIX 分支（spec 验收 4：本目录测试
    须平台无关，在 WSL 与 orca-win 都跑）。
    """
    sock = tmp_path / "orca-x.sock"
    with patch.object(paths_mod, "IS_WINDOWS", False):
        assert chart_endpoint(sock) == str(sock.resolve())


# ── win32 分支（IS_WINDOWS 运行时可 patch）──────────────────────────────────


def test_chart_endpoint_win32_reads_port_file(tmp_path):
    """win32：port 文件在且合法 → ``tcp://127.0.0.1:<port>``。"""
    sock = tmp_path / "orca-x.sock"
    chart_port_file_path(sock).write_text("51234", encoding="utf-8")
    with patch.object(paths_mod, "IS_WINDOWS", True):
        assert chart_endpoint(sock) == "tcp://127.0.0.1:51234"


def test_chart_endpoint_win32_missing_port_file_returns_empty(tmp_path):
    """win32：port 文件缺失（ingestor 未起）→ ``""``（不注 env，script 端 fail loud）。"""
    with patch.object(paths_mod, "IS_WINDOWS", True):
        assert chart_endpoint(tmp_path / "orca-x.sock") == ""


def test_chart_endpoint_win32_corrupt_port_file_returns_empty(tmp_path):
    """win32：port 文件损坏（半写 / 非 digits）→ ``""``（宽容解析失败，非 crash）。"""
    sock = tmp_path / "orca-x.sock"
    port_file = chart_port_file_path(sock)
    port_file.write_text("not-a-port", encoding="utf-8")
    with patch.object(paths_mod, "IS_WINDOWS", True):
        assert chart_endpoint(sock) == ""
    port_file.write_text("", encoding="utf-8")  # 空文件（半写残留）
    with patch.object(paths_mod, "IS_WINDOWS", True):
        assert chart_endpoint(sock) == ""


def test_chart_endpoint_win32_port_file_with_whitespace_strips(tmp_path):
    """win32：port 值带首尾空白（文本写法的常见形态）→ strip 后解析。"""
    sock = tmp_path / "orca-x.sock"
    chart_port_file_path(sock).write_text(" 4123 \n", encoding="utf-8")
    with patch.object(paths_mod, "IS_WINDOWS", True):
        assert chart_endpoint(sock) == "tcp://127.0.0.1:4123"


# ── write_chart_port_file：原子写 + pid tmp + replace 重试（D1 修订）─────────


def test_write_chart_port_file_writes_port_and_pid_suffixed_tmp(tmp_path):
    """写入内容 = 端口字符串；tmp 文件名带 pid（D1：防双 daemon 写者互踩固定 tmp 名）。"""
    sock = tmp_path / "orca-x.sock"
    port_file = chart_port_file_path(sock)
    write_chart_port_file(port_file, 51234)
    assert port_file.read_text(encoding="utf-8") == "51234"
    # 落盘过程无 tmp 残留（replace 完成）。
    assert not list(tmp_path.glob("*.tmp"))


def test_write_chart_port_file_retry_once_on_permission_error(tmp_path, monkeypatch):
    """Windows reader 持句柄 → replace 抛 PermissionError → 重试 1 次成功。

    意图：D1 修订的「重试 1 次」契约——短暂持锁的 reader（chart_endpoint 的
    read_text 微秒级）不应让端口发布失败；模拟第一次 raise、第二次成功。
    """
    sock = tmp_path / "orca-x.sock"
    port_file = chart_port_file_path(sock)
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "reader holds handle (simulated)")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    write_chart_port_file(port_file, 51234)
    assert calls["n"] == 2, "PermissionError 后必须恰好重试 1 次"
    assert port_file.read_text(encoding="utf-8") == "51234"


def test_write_chart_port_file_raises_after_retry_exhausted(tmp_path, monkeypatch):
    """重试 1 次仍 PermissionError → raise（fail loud，由 D3 熔断兜底，不无限循环）。"""
    sock = tmp_path / "orca-x.sock"
    port_file = chart_port_file_path(sock)

    def always_denied(src, dst):
        raise PermissionError(13, "permanently locked (simulated)")

    monkeypatch.setattr(os, "replace", always_denied)
    with pytest.raises(PermissionError):
        write_chart_port_file(port_file, 51234)
