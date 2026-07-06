"""registry.py —— ProcessRegistry：子进程 spawn/kill 全局注册表（ADR §4.7 / phase-11 §1-2）。

回答「Orca spawn 的子进程（claude/opencode/codex/grep/bash/node）怎么不漏掉？
cancel 时孙子进程怎么办？」：

  1. **每个 spawn 必须注册**（铁律 1）：``asyncio.create_subprocess_exec`` 调用方在
     spawn 后立刻调 ``registry.acquire(proc, ...)``，把进程 + 进程组 + cleanup hooks
     登记进来。``atexit`` + 信号 handler 通过 ``shutdown()`` 兜底清理未 release 的进程。
  2. **进程组隔离**（铁律 2，SPEC §2.1）：spawn 用 ``start_new_session=True``（POSIX）
     或 ``CREATE_NEW_PROCESS_GROUP``（Windows），让子进程成为新 process group leader。
     cancel 时用 ``os.killpg``（POSIX）/ ``CTRL_BREAK_EVENT``（Windows）整组杀——
     孙子进程（claude spawn 的 grep/bash/node）不变孤儿。**推翻 phase-3-events.md §2.5
     旧决策**（理由见 phase-11-process-lifecycle §2.1）。
  3. **三段式 cancel**（铁律 3）：SIGTERM（grace 期让 agent 写完文件 / 释放锁）
     → SIGKILL（强杀）→ cleanup hooks（关 fd / 删临时文件）。**禁止**直接 SIGKILL 起手。
  4. **清理必幂等**（铁律 5）：``shutdown()`` 多次调用不报错；``kill_one`` 对未注册
     pid 直接 return。

依赖注入（ADR §4.7，闭环审视 B8）：
  phase-11 v1 曾用 class-level ``_instance`` + ``_lock`` singleton——并行 pytest (xdist)
  与测试隔离会破坏。**v2 改 DI**：

    - production：``orchestrator(registry=get_default_registry())``，
      ``get_default_registry()`` 返回模块级惰性单例（lazily-created module global）。
    - 测试：``process_local`` fixture 注入独立实例（``tests/conftest.py``），每个测试隔离。

依赖单向：本模块只依赖标准库（os/signal/sys/time/threading/subprocess/logging），
不依赖 orca 其他子模块——它是纯进程管理基础设施，backend / shell 无关。
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    import asyncio

logger = logging.getLogger(__name__)

IS_WINDOWS: bool = sys.platform == "win32"

# grace 期上限（SPEC §2.3）：超过 = 阻塞 cancel，用户感知卡死，禁止。
_MAX_GRACE_SECONDS: float = 10.0
# shutdown() 默认 grace 期（短于 kill_one 默认值，让 atexit 兜底不拖太久）。
_SHUTDOWN_GRACE_SECONDS: float = 1.0
# kill_one poll 间隔（SPEC §2.2 example）。
_POLL_INTERVAL_SECONDS: float = 0.05


@dataclass
class RegisteredProcess:
    """单个注册子进程的元数据（SPEC §1.1）。

    Attributes:
        pid: 子进程 pid。
        pgid: 进程组 id。POSIX ``start_new_session=True`` 时 = pid；Windows 为 None
            （无 killpg 概念）。
        backend: backend 名（``claude`` / ``opencode`` / ``codex`` / ``script`` / ...）。
        run_id: 关联的 Orca run id（cancel 时按 run 批清理用）。
        node_id: 关联的 agent node 名；非 agent spawn（如 gates hook script）为 None。
        started_at: ``time.time()`` 启动时间戳（诊断 / 日志用）。
        cleanup_hooks: 注册时挂的清理回调（关 fd / 删临时文件），kill_one Stage 4 调。
    """

    pid: int
    pgid: int | None
    backend: str
    run_id: str
    node_id: str | None
    started_at: float
    cleanup_hooks: list[Callable[[], None]] = field(default_factory=list)


def spawn_kwargs_for_process_group() -> dict:
    """返回让子进程进新进程组的 subprocess kwargs（SPEC §2.1 / §2.4 平台分支）。

    POSIX：``start_new_session=True``（child 成为 session+group leader）。
    Windows：``creationflags=CREATE_NEW_PROCESS_GROUP``（CTRL_BREAK_EVENT 可寻址）。

    业务层不感知平台差异：调用方把返回值 ``**`` 展开到 ``create_subprocess_exec``。
    """
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _pid_exists(pid: int) -> bool:
    """``pid`` 是否仍存活（kill_one poll 用，SPEC §2.2）。

    POSIX：``os.kill(pid, 0)``——``ProcessLookupError`` = 不存在；``PermissionError``
    = 存在但不归我们管（仍算存活，继续 grace 等）。
    Windows：``os.kill(pid, 0)`` 不可靠，用 ``OpenProcess`` 探测。
    """
    if pid <= 0:
        return False
    if IS_WINDOWS:
        # ctypes 在函数内 import，避免 POSIX 进程拉起 ctypes 加载开销。
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但不归我们管（如 root 进程）——仍算存活，让 SIGKILL 兜底。
        return True
    return True


class ProcessRegistry:
    """子进程 spawn/kill 注册表（SPEC §1 / ADR §4.7）。

    **DI 注入**（非 class-level singleton，ADR §4.7 闭环 B8）：
      - production：``get_default_registry()`` 模块级惰性单例。
      - 测试：``process_local`` fixture 每测试独立实例。

    用法（典型）::

        # spawn
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=..., **spawn_kwargs_for_process_group(),
        )
        entry = registry.acquire(proc, backend="claude", run_id=run_id)

        # 正常退出
        await proc.wait()
        registry.release(proc.pid)

        # cancel / 兜底
        registry.kill_one(proc.pid, grace_seconds=2.0)
        registry.shutdown()  # atexit / signal handler

    线程安全：``_lock`` 保护 ``_procs`` dict 与 ``_shutting_down`` flag。
    ``kill_one`` 在锁外做信号发送 + poll（避免长持锁）。
    """

    def __init__(self) -> None:
        self._procs: dict[int, RegisteredProcess] = {}
        self._lock: threading.Lock = threading.Lock()
        self._shutting_down: bool = False
        self._atexit_registered: bool = False

    # ── 注册 / 注销 ─────────────────────────────────────────────────────────

    def acquire(
        self,
        proc: asyncio.subprocess.Process | subprocess.Popen,
        *,
        backend: str,
        run_id: str,
        node_id: str | None = None,
        cleanup_hooks: list[Callable[[], None]] | None = None,
    ) -> RegisteredProcess:
        """spawn 后立刻调。登记 proc + 进程组（SPEC §1.1）。

        幂等性：同一 pid 重复 acquire 覆盖旧 entry（诊断警告，正常不应发生——
        pid 复用窗口极小）。

        Args:
            proc: ``asyncio.subprocess.Process`` 或 ``subprocess.Popen``（只要有 ``.pid``）。
            backend: backend 名（claude/opencode/codex/script）。
            run_id: 关联 Orca run id。
            node_id: 关联 agent node 名（非 agent spawn 为 None）。
            cleanup_hooks: kill_one Stage 4 调的清理回调列表（关 fd / 删临时文件）。

        Returns:
            ``RegisteredProcess`` entry（调用方可持引用，便于后续 kill_one / release）。

        Raises:
            RuntimeError: registry 正在 shutdown（race：shutdown 期间又 spawn）。
        """
        pid = proc.pid
        pgid: int | None = None
        if not IS_WINDOWS:
            try:
                # start_new_session=True 时 pgid == pid；调 os.getpgid 取真实值（不假设）。
                pgid = os.getpgid(pid)
            except ProcessLookupError:
                # 极端 race：spawn 返回后 proc 立刻退出 → getpgid 失败。pgid 留 None，
                # kill_one 会走单进程分支（os.kill 而非 os.killpg）。
                pgid = None
        entry = RegisteredProcess(
            pid=pid,
            pgid=pgid,
            backend=backend,
            run_id=run_id,
            node_id=node_id,
            started_at=time.time(),
            cleanup_hooks=list(cleanup_hooks or []),
        )
        with self._lock:
            if self._shutting_down:
                # shutdown 已开始又来 acquire：拒绝，避免新进程进将死的 registry。
                raise RuntimeError(
                    "ProcessRegistry 正在 shutdown，拒绝 acquire"
                    f"（pid={pid}, backend={backend}）"
                )
            existing = self._procs.get(pid)
            if existing is not None:
                logger.warning(
                    "pid=%d 被 acquire 覆盖（旧 entry: backend=%s, run_id=%s）；"
                    "正常路径不应发生——是否漏了 release？",
                    pid, existing.backend, existing.run_id,
                )
            self._procs[pid] = entry
            # 首次 acquire 注册 atexit 兜底（一次性，避免重复注册）。
            if not self._atexit_registered:
                try:
                    atexit.register(self.shutdown)
                    self._atexit_registered = True
                except Exception:
                    # atexit.register 在某些嵌入环境（如 daemon thread）会失败——
                    # 不阻断 acquire，但记 warning 让运维可见。
                    logger.warning(
                        "atexit.register 失败（已吞）；shutdown 兜底依赖 signal handler",
                        exc_info=True,
                    )
        return entry

    def release(self, pid: int) -> None:
        """进程正常退出后调（SPEC §1.1）。

        幂等：pid 未注册时不报错（normal path：release 可能被多次调——
        acquire → 自然退出 release → kill_one 内又 release）。
        """
        with self._lock:
            self._procs.pop(pid, None)

    # ── cancel ─────────────────────────────────────────────────────────────

    def kill_one(self, pid: int, *, grace_seconds: float = 2.0) -> None:
        """三段式 cancel 单个进程（SPEC §2.2）。

        Stage 1: SIGTERM 整个进程组（POSIX ``os.killpg`` / Windows
                 ``CTRL_BREAK_EVENT``）；``ProcessLookupError`` = 已退出，release + return。
        Stage 2: poll grace 期（``_pid_exists`` 每 50ms）；grace 内退出 → 跳 Stage 3。
        Stage 3: 仍存活 → SIGKILL 整个进程组（强杀兜底）。
        Stage 4: cleanup hooks（关 fd / 删临时文件）；hook 抛异常不阻塞 shutdown
                 （warning + 继续），但记 log 让运维可见。

        ``grace_seconds`` 上限 10s（SPEC §2.3）：超过阻塞 cancel，用户感知卡死。

        幂等：未注册 pid 直接 return（多次调同一 pid 不报错）。
        """
        if grace_seconds > _MAX_GRACE_SECONDS:
            raise ValueError(
                f"grace_seconds={grace_seconds} 超过上限 {_MAX_GRACE_SECONDS}"
                "（SPEC §2.3：阻塞 cancel 用户感知卡死）"
            )
        # 锁内取 entry snapshot（不持锁做信号发送 + poll，避免阻塞其他 acquire/release）。
        with self._lock:
            entry = self._procs.get(pid)
        if entry is None:
            return  # 已清 / 未注册——幂等

        self._send_term(entry)
        # Stage 2: poll grace 期。用 monotonic 避免系统时钟跳变。
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not _pid_exists(entry.pid):
                break
            time.sleep(_POLL_INTERVAL_SECONDS)

        # Stage 3: 仍存活 → SIGKILL 整组。
        if _pid_exists(entry.pid):
            self._send_kill(entry)

        # Stage 4: cleanup hooks（SPEC §2.2 注释：cleanup 不阻塞 shutdown，但记 log）。
        for hook in entry.cleanup_hooks:
            try:
                hook()
            except Exception:
                logger.warning(
                    "cleanup hook 抛异常（pid=%d，已吞，不阻塞 shutdown）",
                    entry.pid, exc_info=True,
                )

        self.release(pid)

    def shutdown(self) -> None:
        """atexit / signal handler / RunManager.shutdown 三处都调此（SPEC §1.1）。

        幂等（铁律 5）：``_shutting_down`` flag 保护，多次调直接 return。
        grace 期 = ``_SHUTDOWN_GRACE_SECONDS``（1s，让 atexit 不拖太久）。
        cleanup hooks 由各进程的 ``kill_one`` 自行调。
        """
        # double-checked locking pattern：先在锁内判 flag + 取 pids snapshot。
        with self._lock:
            if self._shutting_down:
                return
            self._shutting_down = True
            pids = list(self._procs.keys())
        if pids:
            logger.info(
                "ProcessRegistry.shutdown：清理 %d 个未释放子进程", len(pids),
            )
        for pid in pids:
            try:
                self.kill_one(pid, grace_seconds=_SHUTDOWN_GRACE_SECONDS)
            except Exception:
                # shutdown 路径不能因单个 kill 失败中止其余清理。
                logger.warning(
                    "shutdown kill_one pid=%d 异常（已吞，继续清其余）", pid,
                    exc_info=True,
                )
        # 兜底清空（kill_one 已 release 各 pid，但锁竞争下可能有遗漏）。
        with self._lock:
            self._procs.clear()

    # ── 信号发送（平台分支，SPEC §2.4）─────────────────────────────────────

    def _send_term(self, entry: RegisteredProcess) -> None:
        """Stage 1 信号：POSIX ``os.killpg(SIGTERM)`` / Windows ``CTRL_BREAK_EVENT``。"""
        try:
            if IS_WINDOWS or entry.pgid is None:
                # Windows / 未知 pgid：退化为单进程信号。Windows 上若有
                # CTRL_BREAK_EVENT，可寻址整个 CREATE_NEW_PROCESS_GROUP 组。
                if hasattr(signal, "CTRL_BREAK_EVENT"):
                    os.kill(entry.pid, signal.CTRL_BREAK_EVENT)
                else:
                    os.kill(entry.pid, signal.SIGTERM)
            else:
                os.killpg(entry.pgid, signal.SIGTERM)
        except ProcessLookupError:
            # 进程已退出：release + 让 kill_one 走完 cleanup hooks（直接 return 不跳
            # cleanup——SPEC §2.2 流程要求 cleanup 总是跑）。
            pass
        except PermissionError:
            # 进程不归我们管（如 root 启动的）：warning，let SIGKILL path try。
            logger.warning(
                "SIGTERM pid=%d/pgid=%s PermissionError（进程不归当前用户管）",
                entry.pid, entry.pgid, exc_info=True,
            )

    def _send_kill(self, entry: RegisteredProcess) -> None:
        """Stage 3 信号：POSIX ``os.killpg(SIGKILL)`` / Windows ``SIGKILL`` 单进程。"""
        try:
            if IS_WINDOWS or entry.pgid is None:
                os.kill(entry.pid, signal.SIGKILL)
            else:
                os.killpg(entry.pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # race：SIGTERM grace 内退出，SIGKILL 时已 reap。
        except PermissionError:
            logger.warning(
                "SIGKILL pid=%d/pgid=%s PermissionError（无法强杀）",
                entry.pid, entry.pgid, exc_info=True,
            )


# ── 模块级惰性单例（production；测试用 process_local fixture）────────────────


_default_registry: ProcessRegistry | None = None
_default_lock = threading.Lock()


def get_default_registry() -> ProcessRegistry:
    """模块级惰性 singleton（ADR §4.7 / SPEC §1.2）。

    production 用：``orchestrator(registry=get_default_registry())``。
    测试**不**用此——用 ``process_local`` fixture 注入独立实例（避免跨测试状态污染）。

    线程安全：``_default_lock`` 保护首次创建。
    """
    global _default_registry
    with _default_lock:
        if _default_registry is None:
            _default_registry = ProcessRegistry()
        return _default_registry
