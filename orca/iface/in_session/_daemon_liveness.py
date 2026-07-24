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
  - pidfile 探只读 ``pidfile`` + ``/proc/<pid>/cmdline``，零写。

POSIX-only（与 ``fcntl.flock`` / Unix socket 同前提；项目 ADR I3.3 已锚定 POSIX）。

依赖单向：仅 stdlib（socket / pathlib）；无 Orca 内部依赖（最底层 utility）。
"""

from __future__ import annotations

import socket
from pathlib import Path

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
    """探 pidfile + ``/proc/<pid>/cmdline`` 的守护是否存活（无 socket 的守护用，如 sidechain）。

    与 ``socket_daemon_alive`` 的「connect socket 探」对应 —— sidechain 守护无 socket（无 ingress），
    改用 pidfile。pid 复用防御：读 ``/proc/<pid>/cmdline`` 必须**同时**含 ``module_name``（如
    ``orca.iface.in_session.sidechain_daemon``）；若给 ``run_id``，还要 ``--run-id`` 与
    ``run_id`` 都在 argv（/proc/<pid>/cmdline 是 ``\\x00`` 分隔的 argv，split 后逐项比）。

    不可逆（保守 False）：
      - pidfile 不存在 → False（守护未起 / 已 graceful 退）。
      - pidfile 存在但 ``/proc`` 无对应 pid → False（守护被 SIGKILL，pidfile 残留）。
      - pidfile 存在 + pid 活但 cmdline 不匹配 → False（pid 复用为其它进程）。
      - 非 Linux（/proc 不存在）→ False（保守判 dead 触发 respawn）。

    Args:
        pidfile: pidfile 路径（由调用方按 run_id 派生，如 sidechain 的
            ``_sidechain_pidfile_path(run_id)``）。
        module_name: 守护模块名（用于 ``/proc/<pid>/cmdline`` 匹配，如
            ``"orca.iface.in_session.sidechain_daemon"``）。
        run_id: 可选 run_id 核验（cmdline 必须含 ``--run-id`` 与该 run_id）；None → 跳过此项。
    """
    if not pidfile.is_file():
        return False
    try:
        pid_str = pidfile.read_text(encoding="utf-8").strip()
        pid = int(pid_str)
    except (ValueError, OSError):
        return False
    # /proc 校验（防 pid 复用）。非 Linux：/proc 不存在 → 保守判 dead（触发 respawn）。
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
