"""_daemon_liveness.py —— detached daemon liveness 探测公共 helper（SPEC §5 S9）。

回答「chart 守护 / sidechain 守护还活着吗」的 bool 判定，DRY 掉 ``cli._chart_daemon_alive``
（socket connect-probe）与 ``sidechain_daemon._sidechain_daemon_alive``（pidfile +
``/proc/<pid>/cmdline``）两份独立实现。两守护的 respawn 决策（``_ensure_chart_daemon`` /
``_ensure_sidechain_daemon``）调本 helper。

**保守 false 原则**（假阴性比假阳性安全）：所有异常路径 → False。最坏产生一个无害孤儿守护
（新守护 unlink+rebind 把 socket 路径指向自己 / 写新 pidfile 覆盖老的），老守护监听的 inode
失去路径 / pidfile 不再代表它，由终态事件 / TTL 自清。判活而实际死了才会真丢 chart / 子 agent
过程事件 → 不可接受。

**副作用 = 鐱**：
  - socket 探 connect 成功后立即 close（守护 ``accept`` 一条短连接 ``readline`` 读 EOF 静默返回，
    ``chart_ingestor._make_handler`` 的 ``if not line`` 分支）。
  - pidfile 探只读 ``pidfile`` + ``/proc/<pid>/cmdline``（Linux）或 ``ps -p <pid> -o args=``
    （macOS/BSD），零写。

POSIX（Linux 走 ``/proc``，macOS/BSD 走 ``ps`` subprocess；与 ``fcntl.flock`` / Unix socket
同前提，项目 ADR I3.3 已锚定 POSIX）。

依赖单向：仅 stdlib（socket / pathlib / subprocess / logging）；无 Orca 内部依赖（最底层 utility）。
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# connect 探测超时：守护同机 Unix socket，正常 <10ms；500ms 仅是高负载下的保守上界。
# 超时（活但 event loop 阻塞 >500ms，如大 tape 首次扫 / GC）→ 保守视 dead → 触发 respawn。
_DEFAULT_PROBE_TIMEOUT = 0.5


def socket_daemon_alive(
    sock_path: Path, *, timeout: float = _DEFAULT_PROBE_TIMEOUT,
) -> bool:
    """探 Unix socket 守护是否有监听者（确定性健康探，**不靠进程名 grep**）。

    回答「守护还活着吗」的三态问题，归一成 bool（活/不活）::

        connect 成功           → 有监听者，守护活（True）
        ConnectionRefusedError → socket 文件在但无监听者（stale，守护被 SIGKILL/SIGTERM 退）→ False
        FileNotFoundError     → 无 socket 文件（守护未起 / 已 graceful 退出并 unlink）→ False
        其它 OSError（超时等） → 视 dead（保守：触发 respawn；假阴性比假阳性安全 —— 最坏产生
                                一个无害孤儿守护，由终态/TTL 自清；见 ``_ensure_chart_daemon``）

    为什么 connect 而非 pgrep/pidfile：Unix socket 的 ``connect`` 是**协议级**判定 —— 文件
    存在 ≠ 有人 listen（SIGKILL 不跑 finally unlink → stale 文件残留）。connect 才区分「监听者
    在」与「孤儿 socket 文件」。进程名 grep 不可靠（同名进程 / 重命名 / 守护名变）；pidfile 要
    做额外 liveness 检查（pid 活 ≠ 在跑这个守护）—— connect 一举覆盖。

    **对守护的副作用 = 零**：connect 成功后立即 close（``with`` 管理语境）→ 守护 accept 一条短
    连接，``readline`` 读到 EOF（空行）→ handler 走「client 提前 close」debug 分支静默返回，
    不 emit、不写 tape（见 ``chart_ingestor._make_handler`` 的 ``if not line`` 分支）。
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sock_path))
    except OSError:
        # ``ConnectionRefusedError``（stale socket，无监听者）/ ``FileNotFoundError``（无 socket
        # 文件）/ 超时等 —— 均为 ``OSError`` 子类，一律视 dead（保守：触发 respawn，假阴性比
        # 假阳性安全）。connect 成功 = 有监听者 = 守护活。
        return False
    return True


def pidfile_daemon_alive(
    pidfile: Path,
    *,
    module_name: str,
    run_id: str | None = None,
) -> bool:
    """探 pidfile + cmdline 的守护是否存活（无 socket 的守护用，如 sidechain）。

    与 ``socket_daemon_alive`` 的「connect socket 探」对应 —— sidechain 守护无 socket（无 ingress），
    改用 pidfile。pid 复用防御：读 cmdline 必须**同时**含 ``module_name``（如
    ``orca.iface.in_session.sidechain_daemon``）；若给 ``run_id``，还要 cmdline 含 ``--run-id``
    与 ``run_id``（子串匹配，见下）。

    平台分支（SPEC D finding 3）：
      - **Linux**：读 ``/proc/<pid>/cmdline``（``\\x00`` 分隔 argv，零 fork）。
      - **macOS/BSD**（``/proc`` 不存在）：先 ``os.kill(pid, 0)`` 探进程存在（zombie 仍响应，
        故 kill-0 成功 ≠ 活—— cmdline 校验 mandatory，禁止 short-circuit），再 ``ps -p <pid>
        -o args=`` 取 cmdline（fork-per-probe，每次 ``orca next`` 一次，非热循环，可接受）。

    cmdline 匹配（macOS 分支用 **子串匹配**，因 ``ps ... -o args=`` 输出是空格 join 后的 argv，
    无法逐项比；守护 module_name + run_id 联合在 cmdline 中已足够唯一）::

        module_name in cmdline AND (run_id is None or ("--run-id" in cmdline and run_id in cmdline))

    不可逆（保守 False，与原「假阴性比假阳性安全」原则一致）：
      - pidfile 不存在 → False（守护未起 / 已 graceful 退）。
      - pidfile 存在但 pid 不活（``/proc`` 无对应 / ``os.kill`` 抛 ``ProcessLookupError``）→ False。
      - pidfile 存在 + pid 活但 cmdline 不匹配 → False（pid 复用为其它进程 / zombie）。
      - ps subprocess 异常 → False（保守判 dead 触发 respawn）。

    Args:
        pidfile: pidfile 路径（由调用方按 run_id 派生，如 sidechain 的
            ``_sidechain_pidfile_path(run_id)``）。
        module_name: 守护模块名（用于 cmdline 匹配，如
            ``"orca.iface.in_session.sidechain_daemon"``）。
        run_id: 可选 run_id 核验（cmdline 必须含 ``--run-id`` 与该 run_id 子串）；None → 跳过此项。
    """
    if not pidfile.is_file():
        return False
    try:
        pid_str = pidfile.read_text(encoding="utf-8").strip()
        pid = int(pid_str)
    except (ValueError, OSError):
        return False

    # 平台分支：Linux 走 /proc（零 fork）；macOS/BSD 走 kill-0 + ps subprocess。
    if Path("/proc").is_dir():
        return _pidfile_alive_linux(pid, module_name, run_id)
    return _pidfile_alive_macos(pid, module_name, run_id)


def _cmdline_matches_substring(
    cmdline: str, module_name: str, run_id: str | None,
) -> bool:
    """cmdline 子串匹配（macOS ``ps -o args=`` 输出，空格 join 的 argv 无法逐项比）。

    SPEC D finding 3 §4.3：``module_name in cmdline AND (run_id is None OR
    ("--run-id" in cmdline AND run_id in cmdline))``。守护 module_name + run_id 联合在
    cmdline 中已足够唯一，跨参数误匹配概率可忽略（即便误匹配也只是多 spawn 一个守护，
    由 ownership unlink + 终态/TTL 自清）。
    """
    if module_name not in cmdline:
        return False
    if run_id is None:
        return True
    return "--run-id" in cmdline and run_id in cmdline


def _pidfile_alive_linux(
    pid: int, module_name: str, run_id: str | None,
) -> bool:
    """Linux 路径：``/proc/<pid>/cmdline`` 零 fork 读 + 逐项 argv 匹配。"""
    cmdline_path = Path("/proc") / str(pid) / "cmdline"
    try:
        cmdline_bytes = cmdline_path.read_bytes()
    except (FileNotFoundError, PermissionError, OSError):
        return False
    # /proc/<pid>/cmdline 是 \x00 分隔的 argv；split 后逐项比（防跨参数误匹配）。
    argv = cmdline_bytes.decode("utf-8", "replace").split("\x00")
    if not any(module_name in a for a in argv):
        return False
    if run_id is not None:
        if "--run-id" not in argv or run_id not in argv:
            return False
    return True


def _pidfile_alive_macos(
    pid: int, module_name: str, run_id: str | None,
) -> bool:
    """macOS/BSD 路径：``os.kill(pid, 0)`` 探进程存在 + ``ps`` subprocess 取 cmdline 校验。

    SPEC D finding 3 §4.3：``kill(pid, 0)`` 成功 ≠ 活（zombie pid 仍响应 kill-0），故 cmdline
    匹配 mandatory，禁止 kill-0 成功后 short-circuit ``return True``。``ps`` 取 cmdline 后
    走子串匹配（``_cmdline_matches_substring``）。

    fork-per-probe 成本：每次 ``orca next``（~10s 一次，非热循环）fork 一次 subprocess，可接受。
    """
    # 1. kill-0 探进程存在（POSIX 通用，零 fork）。
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # pid 不存在（守护被 SIGKILL / 已退）
    except PermissionError:
        # 进程存在但无权限（与 Linux /proc PermissionError 行为对齐，进 cmdline 校验）。
        pass
    except OSError:
        return False  # 其它 OSError → 保守 False
    # 2. ps subprocess 取 cmdline（防 pid 复用 + 防 zombie）。
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, check=False,
        )
    except (OSError, ValueError):
        # SPEC D §7 fail-path：ps 命令不可用 / 异常 → 保守 False + warning 提示检查 ps。
        logger.warning(
            "macOS/BSD ps subprocess 异常 pid=%s，视 dead（检查 ps 命令可用性）",
            pid, exc_info=True,
        )
        return False
    if proc.returncode != 0:
        # ps 非 0 退出：pid 不存在 / ps 错误。stdout 也应空——保守 False（无 warning，
        # 因 pid 不存在是常见正常路径，warning 会噪音）。
        return False
    cmdline = proc.stdout.strip()
    if not cmdline:
        return False
    return _cmdline_matches_substring(cmdline, module_name, run_id)
