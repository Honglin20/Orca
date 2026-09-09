"""_flock.py —— 跨进程文件锁 shim（POSIX ``fcntl.flock`` / Windows ``msvcrt.locking``）。

回答「in-session CLI/daemon 的 tape 互斥怎么上 Windows？」：三处（``cli.py`` /
``daemon.py`` / ``chart_daemon.py``）原直接 ``import fcntl``——Windows 无该模块，
**模块级 import 即崩**。本 shim 提供与 ``fcntl`` 等价的 ``flock(fd, flags)`` + 三常量，
调用方只换 import，锁语义不变：

- ``LOCK_EX | LOCK_NB`` 拿不到锁 → raise ``BlockingIOError``（与 POSIX flock 一致，
  调用方既有 ``except BlockingIOError`` 分支零改动）。
- 阻塞 ``LOCK_EX`` → POSIX 直传；Windows 循环 ``LK_NBLCK`` + 10ms sleep（``LK_LOCK``
  自带 10 次 1s 重试上限即抛，不适合长临界区等待）。
- ``LOCK_UN`` → POSIX 直传；Windows ``LK_UNLCK``，**未持锁时 no-op 成功**（对齐 POSIX
  ``flock(LOCK_UN)`` 对未持锁 fd 的 no-op 语义；msvcrt 对未锁区域 unlock 会 raise，
  不透传——否则「finally unlock + close」两平台行为不对称，spec 2026-09-08 D4 修订）。

**Windows 语义差异（有意接受）**：
  - msvcrt 锁是**文件区域锁**（此处恒锁 1 字节，先 ``lseek(0)`` 保证位置确定），随 fd
    关闭释放——现有「finally unlock + close」模式在两平台等价。
  - 同进程不同 fd 锁同一文件也会互斥（flock 是 per-open-file-description）——两平台
    现有调用模式（per-call 开 fd、用完即关）均不嵌套，不受影响。
  - 锁文件必须以可写模式打开（现有 ``open(path, "w")`` 满足）。

依赖单向：仅 stdlib；被 ``cli.py`` / ``daemon.py`` / ``chart_daemon.py`` import，
不 import 任何 Orca 模块（iface 层最底层 utility）。
"""

from __future__ import annotations

import errno
import os
import sys
import time

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    # 与 fcntl 常量位值无关的本地常量（仅本模块语义内使用）。
    LOCK_EX = 0x01
    LOCK_NB = 0x02
    LOCK_UN = 0x04

    def flock(fd: int, flags: int) -> None:
        """fcntl.flock 等价：Windows msvcrt 区域锁（恒锁 1 字节 @ offset 0）。"""
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)  # 区域锁基于当前位置，先归零保证确定性
        if flags & LOCK_UN:
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                # 未持锁 unlock → no-op 成功（对齐 POSIX flock(LOCK_UN) 语义，D4 修订）；
                # 调用方只在「自己 acquire 成功的 fd」上 unlock，坏 fd 不是真实路径。
                pass
            return
        if not (flags & LOCK_EX):
            raise ValueError(f"仅支持 LOCK_EX 语义（flags={flags:#x}）")
        if flags & LOCK_NB:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as e:
                # 锁被占 → 归一成 POSIX flock 的 BlockingIOError（调用方零改动）。
                raise BlockingIOError(
                    errno.EACCES, "resource busy (msvcrt LK_NBLCK conflict)"
                ) from e
            return
        # 阻塞语义：循环 NB + 短 sleep（LK_LOCK 的 10×1s 上限不适合长临界区等待）。
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.01)

else:
    import fcntl

    LOCK_EX = fcntl.LOCK_EX
    LOCK_NB = fcntl.LOCK_NB
    LOCK_UN = fcntl.LOCK_UN

    def flock(fd: int, flags: int) -> None:
        """POSIX 直传 fcntl.flock（行为零改动）。"""
        fcntl.flock(fd, flags)
