"""web_registry.py —— web server 端口登记（SPEC §13 D6 / B-6 / M-7）。

**两代登记**：
  - **新（用户级）**：``~/.orca/.orca-web.json`` —— 一用户（ORCA_HOME）一份登记，
    ``{port, runs_dir_fp}``。``runs_dir_fp`` 实为 ``orca_home_fingerprint()``（兼容期保留字段名）。
  - **旧（per-project）**：``<runs_dir>/.orca-web.json`` —— 读时 fallback 静默迁移一次（M-7）。

**并发**（SPEC §13.2 B-6）：「决策+spawn+bind+socket-ready+写回」临界区由调用方
（``_open_run``）持有 ``_registry_lock()`` 实现；本模块提供 ``try_acquire_port_decision``
的 atomic「读+比较+写」helper（在 lock 内读写，保证两并发 ``orca open`` 只起一个 server）。

**探测权威**（spec-review）：登记只是 hint；``_lookup_my_registered_port`` 读 port 后**仍**
probe + 指纹校验（在 ``commands.py``）；陈旧/损坏 → ``None`` 自愈。

依赖单向：纯 stdlib（json/os/pathlib/tempfile），不 import commands（避免环）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from orca.runtime import orca_home

REGISTRY_NAME = ".orca-web.json"
_LOCK_NAME = ".orca-web.lock"

# 旧 per-project 登记的迁移标记（写入新登记的 ``migrated_from`` 字段；迁移完成的旧文件改名加后缀，
# 避免下次再迁。M-7）。
_LEGACY_MIGRATED_SUFFIX = ".migrated"


def orca_home_registry_path() -> Path:
    """用户级登记文件路径：``~/.orca/.orca-web.json``。"""
    return orca_home() / REGISTRY_NAME


@contextmanager
def _registry_lock() -> Iterator[None]:
    """exclusive flock ``~/.orca/.orca-web.lock``（SPEC §13.2 B-6）。

    单一临界区 helper；**公开 API 禁嵌套调用**（Windows msvcrt 死锁）。
    """
    home = orca_home()
    home.mkdir(parents=True, exist_ok=True)
    lock_path = home / _LOCK_NAME
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
        if sys.platform == "win32":
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ── 旧 per-project 接口（保留给既有测试 / fallback 读） ────────────────────────


def registry_path(runs_dir: Path | str) -> Path:
    """旧 per-project 登记文件路径：``<runs_dir>/.orca-web.json``。"""
    return Path(runs_dir) / REGISTRY_NAME


def read_registry(runs_dir: Path | str) -> dict | None:
    """读旧 per-project 登记。缺失/损坏/非 dict → ``None``。"""
    try:
        data = json.loads(registry_path(runs_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_registry(
    runs_dir: Path | str, *, port: int, runs_dir_fp: str
) -> None:
    """原子写旧 per-project 登记（保留向后兼容；新代码应调 ``write_orca_home_registry``）。"""
    path = registry_path(runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"port": port, "runs_dir_fp": runs_dir_fp}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


# ── 新用户级登记（SPEC §13 D6） ───────────────────────────────────────────────


def lookup_orca_home_port() -> int | None:
    """读 ``~/.orca/.orca-web.json`` 的 port（无/坏 → ``None``）。外部 API：自带锁。"""
    with _registry_lock():
        return _lookup_orca_home_port_unlocked()


def _lookup_orca_home_port_unlocked() -> int | None:
    """读 port（**不加锁**——供 ``exclusive_port_decision`` 临界区内调用，P1）。"""
    try:
        data = json.loads(
            orca_home_registry_path().read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    port = data.get("port")
    return int(port) if isinstance(port, int) else None


def write_orca_home_registry(*, port: int, runs_dir_fp: str) -> None:
    """原子写用户级登记（SPEC §13 D6）。外部 API：自带 ``_registry_lock``。"""
    with _registry_lock():
        _write_orca_home_registry_unlocked(port=port, runs_dir_fp=runs_dir_fp)


def _write_orca_home_registry_unlocked(*, port: int, runs_dir_fp: str) -> None:
    """原子写用户级登记（**不加锁**——供 ``exclusive_port_decision`` 临界区内调用）。

    P1「公开 API 禁嵌套调用」：``exclusive_port_decision`` 已持锁，写回必须用此 unlocked
    变体，否则 ``fcntl.flock`` 同进程二次 acquire 不同 fd → 死锁（Linux）。
    """
    path = orca_home_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"port": port, "runs_dir_fp": runs_dir_fp}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def exclusive_port_decision() -> Iterator[tuple]:
    """「决策+spawn+bind+socket-ready+写回」临界区（SPEC §13.2 B-6）。

    ``orca open`` 决定是否 spawn 新 server 时，必须在同一 exclusive 锁内完成：

      1. 读既有登记；
      2. 决定 spawn；
      3. bind socket（占用端口）；
      4. socket ready；
      5. 写回登记。

    持锁直到步骤 5 完成。本 contextmanager 让 ``_open_run`` 持锁；两并发 ``orca open``
    第二个进来时第一个已 bind 端口，第二个读到登记并 probe 复用。

    Yields 一个 ``(write_back)`` callable，调用方在临界区内 spawn+ready 后调它写回
    （内部用 unlocked 写，避免嵌套 flock 死锁）。
    """
    with _registry_lock():
        def write_back(*, port: int, runs_dir_fp: str) -> None:
            _write_orca_home_registry_unlocked(
                port=port, runs_dir_fp=runs_dir_fp
            )

        yield write_back


def migrate_legacy_registry(runs_dir: Path | str) -> None:
    """旧 per-project 登记静默迁移到用户级（SPEC §13.1 M-7）。

    语义：
      - 旧文件存在且新文件**不存在**：拷贝 port/fp 到新文件 + 旧文件改名 ``.migrated``。
      - 新文件已存在：跳过（新权威）。
      - 旧文件已是 ``.migrated`` 或不存在：跳过。

    静默：任何异常（permission / 路径问题）忽略（迁移只是 best-effort）。
    """
    new_path = orca_home_registry_path()
    if new_path.exists():
        return
    old_path = registry_path(runs_dir)
    migrated = old_path.with_name(old_path.name + _LEGACY_MIGRATED_SUFFIX)
    if migrated.exists():
        return
    try:
        data = json.loads(old_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    port = data.get("port")
    fp = data.get("runs_dir_fp")
    if not isinstance(port, int) or not isinstance(fp, str):
        return
    try:
        with _registry_lock():
            if new_path.exists():
                return
            new_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"port": port, "runs_dir_fp": fp, "migrated_from": str(old_path)}
            tmp = new_path.with_name(new_path.name + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, new_path)
            old_path.rename(migrated)
    except OSError:
        return
