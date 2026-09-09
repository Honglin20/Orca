"""tests/iface/in_session/test_flock.py —— flock shim 语义（spec 2026-09-08 D4）。

覆盖意图（非仅行为）：
  - ``LOCK_EX | LOCK_NB`` 二次抢锁（另一 fd 持有）→ raise ``BlockingIOError``
    （归一 POSIX 语义；cli/daemon 的 busy-exit / gc skip 分支零改动的契约前提）。
    **Windows 回归重点**：旧实现漏 ``import errno`` → NB 冲突抛 NameError 逃逸
    ``except BlockingIOError``（已知 bug #1）。
  - 持锁释放后可再抢（锁真被释放，非永久占用）。
  - ``LOCK_UN`` 未持锁 → no-op 成功（对齐 POSIX ``flock(LOCK_UN)``；msvcrt 会 raise，
    shim 不透传——已知 bug #6 / D4 修订）。
  - 锁随 fd 关闭释放（chart_daemon ``finally unlock + close`` 两平台等价的前提）。
  - 常量三件套存在且可按位组合（调用方 ``LOCK_EX | LOCK_NB`` 写法零改动）。

平台无关：经 shim 本身跑（POSIX 走 fcntl 直传、win32 走 msvcrt），两平台语义一致
是 shim 的存在意义；orca-win 真跑是主验证面（spec 验收 4）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from orca.iface.in_session import _flock

IS_WINDOWS = sys.platform == "win32"


def _open_writable(path: Path):
    """锁文件以可写模式打开（D4 契约：msvcrt 锁要求可写 fd；调用方现状 ``open(w)``）。"""
    return open(path, "w")


def test_nb_conflict_raises_blockingioerror(tmp_path):
    """另一 fd 持锁时 ``LOCK_EX | LOCK_NB`` → BlockingIOError（非 NameError/OSError）。

    Windows 回归：旧 shim 用 ``errno.EACCES`` 未 import → NameError 逃逸调用方
    ``except BlockingIOError``（busy 分支失效 = flock 互斥整个失效）。
    """
    lock_path = tmp_path / "tape.jsonl.lock"
    fd1 = _open_writable(lock_path)
    try:
        _flock.flock(fd1.fileno(), _flock.LOCK_EX | _flock.LOCK_NB)
        fd2 = _open_writable(lock_path)
        try:
            with pytest.raises(BlockingIOError):
                _flock.flock(fd2.fileno(), _flock.LOCK_EX | _flock.LOCK_NB)
        finally:
            fd2.close()
    finally:
        fd1.close()


def test_lock_release_allows_reacquire(tmp_path):
    """unlock 后同/另一 fd 可再抢（锁释放真实生效，非永久占用）。"""
    lock_path = tmp_path / "tape.jsonl.lock"
    fd1 = _open_writable(lock_path)
    try:
        _flock.flock(fd1.fileno(), _flock.LOCK_EX | _flock.LOCK_NB)
        _flock.flock(fd1.fileno(), _flock.LOCK_UN)
        # 释放后另一 fd 立即可抢（NB 不再冲突）。
        fd2 = _open_writable(lock_path)
        try:
            _flock.flock(fd2.fileno(), _flock.LOCK_EX | _flock.LOCK_NB)
            _flock.flock(fd2.fileno(), _flock.LOCK_UN)
        finally:
            fd2.close()
    finally:
        fd1.close()


def test_unlock_without_lock_is_noop(tmp_path):
    """未持锁 fd 上 ``LOCK_UN`` → no-op 成功（D4 修订：不透传 msvcrt 的 raise）。

    Windows 回归：msvcrt 对未锁区域 unlock 抛 OSError；POSIX ``flock(LOCK_UN)``
    对未持锁 fd 是 no-op 成功。shim 必须对齐 POSIX（调用方 ``finally unlock``
    不能因重复释放崩）。
    """
    lock_path = tmp_path / "tape.jsonl.lock"
    fd = _open_writable(lock_path)
    try:
        _flock.flock(fd.fileno(), _flock.LOCK_UN)  # 不应 raise
    finally:
        fd.close()


def test_double_unlock_is_noop(tmp_path):
    """持锁 → unlock → 再 unlock（重复释放）→ 第二次 no-op（两平台行为对称）。"""
    lock_path = tmp_path / "tape.jsonl.lock"
    fd = _open_writable(lock_path)
    try:
        _flock.flock(fd.fileno(), _flock.LOCK_EX | _flock.LOCK_NB)
        _flock.flock(fd.fileno(), _flock.LOCK_UN)
        _flock.flock(fd.fileno(), _flock.LOCK_UN)  # 重复 unlock 不 raise
    finally:
        fd.close()


def test_lock_released_on_fd_close(tmp_path):
    """锁随 fd 关闭自动释放（msvcrt 与 flock 共有语义；无孤儿锁）。"""
    lock_path = tmp_path / "tape.jsonl.lock"
    fd1 = _open_writable(lock_path)
    _flock.flock(fd1.fileno(), _flock.LOCK_EX | _flock.LOCK_NB)
    fd1.close()  # 不显式 unlock，直接 close
    fd2 = _open_writable(lock_path)
    try:
        # fd1 已关 → 锁释放 → fd2 NB 抢锁必须成功（否则孤儿锁会让后续 CLI 永久 busy）。
        _flock.flock(fd2.fileno(), _flock.LOCK_EX | _flock.LOCK_NB)
        _flock.flock(fd2.fileno(), _flock.LOCK_UN)
    finally:
        fd2.close()


def test_constants_exist_and_bitwise_or(tmp_path):
    """LOCK_EX / LOCK_NB / LOCK_UN 三常量存在且可按位组合（调用方写法零改动）。"""
    for name in ("LOCK_EX", "LOCK_NB", "LOCK_UN"):
        assert isinstance(getattr(_flock, name), int), name
    # 组合可传给 flock 不因常量位冲突误入异常分支（win32 本地常量位值自洽）。
    lock_path = tmp_path / "tape.jsonl.lock"
    fd = _open_writable(lock_path)
    try:
        _flock.flock(fd.fileno(), _flock.LOCK_EX | _flock.LOCK_NB)
    finally:
        fd.close()
