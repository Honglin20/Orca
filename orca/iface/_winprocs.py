"""_winprocs.py —— Windows 进程存活探测共享 helper（spec 2026-09-08 D5/D6）。

回答「Windows 上怎么安全地探 pid 活着吗」：``os.kill(pid, 0)`` 在 Windows 的语义是
``TerminateProcess``（把 sig 值当退出码**直接杀进程**），绝不可用于探活——2026-09-08
实测根因 6：``tars ps`` 探活 = 杀掉长跑 run 进程、``_daemon_liveness`` 误入 macOS 分支
= 每 ``next`` 杀一遍 sidechain daemon + respawn 风暴。本模块用 ctypes
``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`` + ``GetExitCodeProcess`` 做零杀伤探测。

**放中立位置的原因**：两个消费方 ``iface/cli/bg_runner``（``tars ps/wait`` 的 pid 检测）
与 ``iface/in_session/_daemon_liveness``（pidfile 探活）是并列子包，禁止互引；共享
helper 落 iface 层根（与 ``in_session/_flock`` 同级的底层 utility）。

语义（与 POSIX ``os.kill(pid, 0)`` 探活对齐）：
  - ``OpenProcess`` 成功 + exit code == ``STILL_ACTIVE``(259) → 活。
  - ``OpenProcess`` 失败且 ``GetLastError`` == ``ERROR_ACCESS_DENIED`` → 进程存在但
    不归当前用户 → 活（对齐 POSIX ``PermissionError`` → True）。
  - ``OpenProcess`` 其它失败（含 ``ERROR_INVALID_PARAMETER`` = pid 不存在）→ 死。
  - ``GetExitCodeProcess`` 查询失败 → 保守死（假阴性安全：触发 respawn / 标 crashed，
    与 ``_daemon_liveness`` 的「保守 false」原则一致）。
  - pid <= 0 → False（占位值不可能存活）。

已知取舍（用户决策 U2-A）：**无 cmdline 校验** → pid 复用假阳性（老守护死、pid 被新
进程复用 → 误判活）。镜像名校验列 follow-up；本轮防「探活变杀伤 / respawn 风暴」优先，
sidechain 非本轮验证面。

依赖单向：仅 stdlib（ctypes/sys）；不 import 任何 Orca 模块。仅可在 win32 调用——
非 win32 误用 → RuntimeError fail loud（POSIX 必须走 ``os.kill(pid, 0)`` 原生语义，
见 ``bg_runner.pid_alive`` 的平台分支）。
"""

from __future__ import annotations

import sys

IS_WINDOWS = sys.platform == "win32"

# Windows SDK 常量（不引 win32api 扩展，ctypes 直调 kernel32）。
_STILL_ACTIVE = 259  # GetExitCodeProcess 约定：259 = 进程仍在跑
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5


def pid_alive(pid: int) -> bool:
    """ctypes ``OpenProcess`` 探活（零杀伤，D5/D6 共用）。仅 win32；误用 → RuntimeError。

    Args:
        pid: 目标进程 id。

    Returns:
        见模块 docstring 的语义表（与 POSIX ``os.kill(pid, 0)`` 对齐）。
    """
    if pid <= 0:
        return False
    if not IS_WINDOWS:
        raise RuntimeError(
            "_winprocs.pid_alive 仅支持 win32（ctypes OpenProcess）；"
            "POSIX 请走 os.kill(pid, 0) 原生语义（bg_runner.pid_alive 平台分支）。"
        )
    import ctypes

    # use_last_error=True：让 ctypes.get_last_error() 拿到本次 API 调用的 GetLastError
    # （windll 默认不清 error 状态，跨调用读取不可靠）。
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
    )
    if not handle:
        # ACCESS_DENIED = 进程存在但无权限（对齐 POSIX PermissionError → 活）；
        # 其它（含 INVALID_PARAMETER = pid 不存在）→ 死。
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
