"""tests/iface/in_session/test_daemon_liveness.py —— S9 helper 直接单元测试（SPEC §5 S9）。

``_daemon_liveness.{socket_daemon_alive, pidfile_daemon_alive}`` 是 S9 DRY 出的公共
liveness helper。本测试直接断言 helper 契约（不经守护 wrapper）：

  - ``socket_daemon_alive``：无 socket 文件 / stale socket（无监听者）/ 真监听者 三态。
  - ``pidfile_daemon_alive``：pidfile 缺 / pidfile 坏 / pid 死 / cmdline 不含模块名 /
    cmdline 不含 run_id / 完全匹配 各分支。

间接覆盖（既有）：``test_chart_daemon.py`` 守 socket probe（经 ``cli._chart_daemon_alive``
薄 wrapper）；``test_sidechain_daemon.py`` 守 pidfile probe（经 ``sidechain_daemon._sidechain_daemon_alive``
薄 wrapper）。本文件加**直接单元测试**专门覆盖 ``run_id`` 校验分支（pid 复用防御第三层），
既有测试因经 wrapper 不方便专门断言此分支。
"""
from __future__ import annotations

import socket
from pathlib import Path

import pytest

from orca.iface.in_session._daemon_liveness import (
    pidfile_daemon_alive,
    socket_daemon_alive,
)


# ── socket_daemon_alive ─────────────────────────────────────────────────────


def test_socket_probe_no_socket_file_returns_false(tmp_path: Path) -> None:
    """无 socket 文件（守护未起 / 已 graceful 退并 unlink）→ False。"""
    assert socket_daemon_alive(tmp_path / "missing.sock") is False


def test_socket_probe_stale_socket_returns_false(tmp_path: Path) -> None:
    """socket 文件存在但无监听者（SIGKILL 残留 stale）→ False。

    SPEC §5 S9：「文件存在 ≠ 有人 listen」—— connect 探才能区分。
    """
    stale = tmp_path / "stale.sock"
    stale.touch()  # 仅创建文件，不 bind / listen
    assert socket_daemon_alive(stale) is False


def test_socket_probe_real_listener_returns_true(tmp_path: Path) -> None:
    """真监听者 → connect 成功 → True。"""
    sock_path = tmp_path / "alive.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    try:
        assert socket_daemon_alive(sock_path) is True
    finally:
        server.close()
        sock_path.unlink(missing_ok=True)


def test_socket_probe_timeout_returns_false(tmp_path: Path, monkeypatch) -> None:
    """超时（OSError 子类，如 socket.timeout）→ False（保守判死）。"""
    sock_path = tmp_path / "timeout.sock"

    # Mock socket.connect 抛 timeout（OSError 子类）
    real_socket = socket.socket

    class _TimeoutSocket:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, t):
            pass

        def connect(self, addr):
            raise socket.timeout("simulated timeout")

    monkeypatch.setattr("orca.iface.in_session._daemon_liveness.socket.socket", _TimeoutSocket)
    try:
        assert socket_daemon_alive(sock_path) is False
    finally:
        # monkeypatch 自动 undo；保险
        pass


# ── pidfile_daemon_alive ────────────────────────────────────────────────────


def test_pidfile_missing_returns_false(tmp_path: Path) -> None:
    """pidfile 不存在 → False（守护未起 / 已 graceful 退并 unlink）。"""
    missing = tmp_path / "missing.pid"
    assert pidfile_daemon_alive(
        missing, module_name="orca.iface.in_session.sidechain_daemon", run_id="r1",
    ) is False


def test_pidfile_corrupt_content_returns_false(tmp_path: Path) -> None:
    """pidfile 内容非整数 → False（保守判死）。"""
    bad = tmp_path / "bad.pid"
    bad.write_text("not-a-number", encoding="utf-8")
    assert pidfile_daemon_alive(
        bad, module_name="any", run_id="r1",
    ) is False


def test_pidfile_dead_pid_returns_false(tmp_path: Path) -> None:
    """pidfile 指向不存在的 pid（无 /proc/<pid>）→ False。

    用一个几乎确定不存在的 pid（如 99999999）模拟 SIGKILL 后 pidfile 残留。
    """
    pidfile = tmp_path / "dead.pid"
    pidfile.write_text("99999999", encoding="utf-8")  # 极大 pid，几乎确定不存在
    assert pidfile_daemon_alive(
        pidfile, module_name="orca.iface.in_session.sidechain_daemon", run_id="r1",
    ) is False


def test_pidfile_module_name_mismatch_returns_false(tmp_path: Path, monkeypatch) -> None:
    """pidfile 指向活 pid 但 cmdline 不含 ``module_name`` → False（pid 复用为其它进程）。

    用 monkeypatch 模拟 ``/proc/<pid>/cmdline`` 读返一个不含目标模块名的 argv。
    """
    pidfile = tmp_path / "mod.pid"
    # 用本测试进程 pid（活）
    import os
    pidfile.write_text(str(os.getpid()), encoding="utf-8")

    # 拦截 Path.read_bytes 仅对 /proc/<pid>/cmdline 返伪 argv
    real_read_bytes = Path.read_bytes

    def _fake_read_bytes(self: Path) -> bytes:
        if self.name == "cmdline" and str(self).startswith("/proc/"):
            # 不含 sidechain 模块名，含别的（模拟 pid 复用为别的进程）
            return "python\x00-m\x00pytest\x00tests/some_test.py\x00".encode("utf-8")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _fake_read_bytes)
    assert pidfile_daemon_alive(
        pidfile, module_name="orca.iface.in_session.sidechain_daemon", run_id="r1",
    ) is False


def test_pidfile_run_id_mismatch_returns_false(tmp_path: Path, monkeypatch) -> None:
    """**SPEC §5 S9 / pid 复用防御第三层**：cmdline 含模块名 + ``--run-id`` 但 run_id 值不匹配 → False。

    模拟「同机另一 orca run 的守护」：cmdline 含 sidechain_daemon + ``--run-id run-X``，
    但调用方查 ``run_id="run-Y"`` → 不应判活（防 A run 的 next 错查 B run 的守护误判 alive）。
    """
    pidfile = tmp_path / "rid.pid"
    import os
    pidfile.write_text(str(os.getpid()), encoding="utf-8")

    real_read_bytes = Path.read_bytes

    def _fake_read_bytes(self: Path) -> bytes:
        if self.name == "cmdline" and str(self).startswith("/proc/"):
            # 含模块名 + --run-id run-X（不是 run-Y）
            return (
                "python\x00-m\x00orca.iface.in_session.sidechain_daemon\x00"
                "--run-id\x00run-X\x00".encode("utf-8")
            )
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _fake_read_bytes)

    # 查 run-Y → 模块名 ✓ 但 run_id ✗ → False
    assert pidfile_daemon_alive(
        pidfile,
        module_name="orca.iface.in_session.sidechain_daemon",
        run_id="run-Y",
    ) is False
    # 查 run-X → 完全匹配 → True（同 pidfile + 同 cmdline，run_id 对得上）
    assert pidfile_daemon_alive(
        pidfile,
        module_name="orca.iface.in_session.sidechain_daemon",
        run_id="run-X",
    ) is True


def test_pidfile_no_run_id_arg_skips_run_id_check(tmp_path: Path, monkeypatch) -> None:
    """``run_id=None`` → 跳过 run_id 校验（只查 module_name 在 cmdline 即可）。"""
    pidfile = tmp_path / "norid.pid"
    import os
    pidfile.write_text(str(os.getpid()), encoding="utf-8")

    real_read_bytes = Path.read_bytes

    def _fake_read_bytes(self: Path) -> bytes:
        if self.name == "cmdline" and str(self).startswith("/proc/"):
            return (
                "python\x00-m\x00orca.iface.in_session.sidechain_daemon\x00".encode("utf-8")
            )
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _fake_read_bytes)
    # run_id=None → 不校验 --run-id / run_id 值
    assert pidfile_daemon_alive(
        pidfile,
        module_name="orca.iface.in_session.sidechain_daemon",
        run_id=None,
    ) is True


# ── macOS / BSD 分支（SPEC D finding 3 / AC-3 / T6 / D-1）─────────────────────


def _force_macos_branch(monkeypatch) -> None:
    """让 ``Path("/proc").is_dir()`` 返 False，强制走 macOS ps 分支。"""
    real_is_dir = Path.is_dir

    def _fake_is_dir(self: Path) -> bool:
        if str(self) == "/proc":
            return False
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", _fake_is_dir)


def test_t6_macos_branch_alive_when_cmdline_matches(tmp_path, monkeypatch):
    """T6（AC-3）：macOS 分支，``/proc`` 不存在时走 ``os.kill(pid,0)`` + ``ps`` cmdline 校验。"""
    _force_macos_branch(monkeypatch)
    pidfile = tmp_path / "mac.pid"
    import os
    pidfile.write_text(str(os.getpid()), encoding="utf-8")  # 当前进程 pid（活）

    # mock subprocess.run 返一个含 module_name + --run-id 的 cmdline。
    import subprocess
    fake_cmdline = (
        f"python -m orca.iface.in_session.sidechain_daemon "
        f"--run-id run-X --tape /tmp/x.jsonl"
    )

    class _FakeCompleted:
        returncode = 0
        stdout = fake_cmdline
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeCompleted())

    assert pidfile_daemon_alive(
        pidfile,
        module_name="orca.iface.in_session.sidechain_daemon",
        run_id="run-X",
    ) is True


def test_t6_macos_branch_pid_reuse_returns_false(tmp_path, monkeypatch):
    """T6（AC-3 pid 复用）：macOS 分支 + ps 返不含 module_name 的 cmdline → False。"""
    _force_macos_branch(monkeypatch)
    pidfile = tmp_path / "mac-reuse.pid"
    import os
    pidfile.write_text(str(os.getpid()), encoding="utf-8")

    import subprocess

    class _FakeCompleted:
        returncode = 0
        stdout = "python -m pytest tests/some_test.py"  # 不含 module_name
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeCompleted())

    assert pidfile_daemon_alive(
        pidfile,
        module_name="orca.iface.in_session.sidechain_daemon",
        run_id="run-X",
    ) is False


def test_t6_macos_branch_pid_not_found_returns_false(tmp_path, monkeypatch):
    """T6（AC-3 pid 不存在）：macOS 分支 + ``os.kill(pid,0)`` 抛 ProcessLookupError → False。"""
    _force_macos_branch(monkeypatch)
    pidfile = tmp_path / "mac-dead.pid"
    pidfile.write_text("99999999", encoding="utf-8")  # 几乎确定不存在的 pid

    import os
    real_kill = os.kill

    def _fake_kill(pid, sig):
        if pid == 99999999:
            raise ProcessLookupError()
        return real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", _fake_kill)

    assert pidfile_daemon_alive(
        pidfile,
        module_name="orca.iface.in_session.sidechain_daemon",
        run_id="run-X",
    ) is False


def test_t6_macos_branch_ps_subprocess_failure_returns_false(tmp_path, monkeypatch, caplog):
    """T6 fail-path：macOS 分支 + ``ps`` 命令不可用（OSError）→ 保守 False + warning。

    SPEC D §7：ps subprocess 异常必须 warning 提示（fail loud，便于诊断为何守护被判 dead）。
    """
    import logging as _logging
    _force_macos_branch(monkeypatch)
    pidfile = tmp_path / "mac-psfail.pid"
    import os
    pidfile.write_text(str(os.getpid()), encoding="utf-8")

    import subprocess

    def _boom(*a, **kw):
        raise OSError("ps not found")

    monkeypatch.setattr(subprocess, "run", _boom)

    with caplog.at_level(_logging.WARNING, logger="orca.iface.in_session._daemon_liveness"):
        assert pidfile_daemon_alive(
            pidfile,
            module_name="orca.iface.in_session.sidechain_daemon",
            run_id="run-X",
        ) is False
    # SPEC D §7：ps 异常必 warning 提示检查 ps 命令。
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= _logging.WARNING]
    assert any("ps subprocess 异常" in m and "检查 ps" in m for m in msgs), (
        f"ps 异常未记 warning（fail-path 未 loud）：{msgs}"
    )


def test_d1_macos_branch_run_id_as_substring_in_unrelated_arg_returns_false(
    tmp_path, monkeypatch,
):
    """D-1（round-2 主审）：cmdline 含一个**与 run_id 无关但包含 run_id 作子串**的 arg → False。

    场景：run_id=``a1b2``；另一 arg ``--foo=a1b2deadbeef`` 把 run_id 作前缀子串包含。
    简单子串匹配 ``run_id in cmdline`` 会误判 True，但本实现要求 ``--run-id`` 同时在 cmdline
    —— ``--run-id`` 不在 → False。
    """
    _force_macos_branch(monkeypatch)
    pidfile = tmp_path / "mac-d1.pid"
    import os
    pidfile.write_text(str(os.getpid()), encoding="utf-8")

    import subprocess

    class _FakeCompleted:
        returncode = 0
        # cmdline 含 module_name + 含 run_id 子串的无关 arg，但**无** ``--run-id``。
        stdout = (
            "python -m orca.iface.in_session.sidechain_daemon "
            "--foo=a1b2deadbeef"
        )
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeCompleted())

    assert pidfile_daemon_alive(
        pidfile,
        module_name="orca.iface.in_session.sidechain_daemon",
        run_id="a1b2",  # 期望 False：cmdline 无 ``--run-id``
    ) is False


def test_macos_branch_kill0_permission_still_runs_ps(tmp_path, monkeypatch):
    """SPEC D §4.3 m3：``PermissionError`` → 视进程存在，进 cmdline 校验（不 short-circuit True）。"""
    _force_macos_branch(monkeypatch)
    pidfile = tmp_path / "mac-perm.pid"
    import os
    pidfile.write_text(str(os.getpid()), encoding="utf-8")

    import os
    import subprocess

    def _fake_kill(pid, sig):
        raise PermissionError("no permission")

    class _FakeCompleted:
        returncode = 0
        stdout = "python -m orca.iface.in_session.sidechain_daemon --run-id run-Z"
        stderr = ""

    monkeypatch.setattr(os, "kill", _fake_kill)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeCompleted())

    assert pidfile_daemon_alive(
        pidfile,
        module_name="orca.iface.in_session.sidechain_daemon",
        run_id="run-Z",
    ) is True
