"""sidechain_daemon.py —— per-run 子 agent 过程 ingestor 守护进程（SPEC-B v4 §3/§4/§5/§7）。

**回答的问题**：in-session 路径下，宿主 session 派的子 agent 过程（msg+tool+thinking）怎么
进 tape？答：本守护 detach 起一个进程，**主动 tail / 查询**子 agent 事件源（CC sidechain
jsonl / opencode sqlite），映射 ``RawAgentEvent`` → ``SidechainIngestor.ingest`` → ``bus.emit``
→ ``_FlockSafeTape``（唯一写路径）。

**与 chart_daemon 的关系（R4 七组件逐字复刻，零 DRY）**：
  - 直接 import ``_FlockSafeTape`` / ``_watch_terminal`` / ``_TERMINAL_EVENT_TYPES`` /
    ``_DEFAULT_TTL_SECONDS``（复用，零改造）。
  - ``_flock_path`` 从 ``cli`` import（同锁文件路径，防漂移）。
  - signal handler + crash callback 同款模式（graceful，不裸 ``sys.exit``）；crash callback
    重建 ingestor（重扫 tape source_id set）+ 重起 driver（reset cursors）。

**唯一增量（B2 vs chart）**：
  - **主体非 socket server**：tail jsonl / 查 sqlite（adapter 抽象），不是 ``asyncio.start_unix_server``。
  - **source_id 查重**（R3）：``SidechainIngestor`` 内存 set；chart_ingestor 无（socket 短连接无重发）。
  - **U1 node 派生**（§6）：emit 前增量扫 tape 取最后 ``node_started`` 的 node。

**生命周期**：
  - bootstrap 启动（``cli._spawn_sidechain_daemon``）；next respawn（``_ensure_sidechain_daemon``）。
  - 自退 1：tail tape 见终态事件 → 退（``_watch_terminal`` 复用）。
  - 自退 2：TTL 兜底（6h，防泄漏）。
  - 退出前：cancel driver task + bus.close + pidfile unlink。

**跨进程 tape 写协调（"单一写路径" 铁律不破）**：本守护与 ``cli.next`` 写同一 tape。``_FlockSafeTape``
在每次 ``append`` 前 flock + 重算 ``_last_seq``，与 ``cli._try_acquire_flock`` 同锁文件、同路径。

**接口同一性 + grep 守门（SPEC §0/§9 AC5）**：backend 选择**只**在 ``_make_adapter``（本模块内）。
ingestor / IR / 前端零 backend 分支。本模块内 ``if backend ==`` 是「启动参数」例外（守门豁免）。

依赖单向：iface 层（依赖 events.{bus,raw_agent_event,sidechain_ingestor,adapters.*} +
chart_daemon + cli._flock_path + stdlib）。不反向依赖 schema/run/exec。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import signal
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from orca.events.adapters.cc_jsonl import CCJsonlAdapter
from orca.events.adapters.opencode_sqlite import OpencodeSqliteAdapter
from orca.events.bus import EventBus
from orca.events.raw_agent_event import ReadAdapter, RawAgentEvent
from orca.events.sidechain_ingestor import SidechainIngestor
from orca.iface.in_session._daemon_liveness import pidfile_daemon_alive
from orca.iface.in_session.chart_daemon import (  # R4 七组件复刻：逐字 import
    _DEFAULT_TTL_SECONDS,
    _FlockSafeTape,
    _watch_terminal,
)

logger = logging.getLogger(__name__)

# SPEC §2 ≤~2s 上界：poll 0.5s（discover + stream + ingest 各 <100ms，加 WS tick ≤2s）。
_DEFAULT_POLL_SECONDS = 0.5

# 守护模块名（``/proc/<pid>/cmdline`` 匹配用；与 ``main`` 的 argv
# ``-m orca.iface.in_session.sidechain_daemon`` 对齐）。SPEC §5 S9：``_sidechain_daemon_alive``
# 薄 wrapper 经此常量调 ``pidfile_daemon_alive``。
_SIDECHAIN_MODULE_NAME = "orca.iface.in_session.sidechain_daemon"


if TYPE_CHECKING:
    # driver 工厂类型（crash callback 用）：0-arg callable 返新 ``_SidechainDriver``。
    from typing import Callable
    _DriverFactory = "Callable[[], _SidechainDriver]"


# ── pidfile（liveness probe 用；cli._ensure_sidechain_daemon 读此判活）─────────


def _sidechain_pidfile_path(run_id: str) -> Path:
    """``/tmp/orca-sidechain-<sha1(run_id)[:10]>.pid``（与 ``chart_sock_path`` 同款短路径）。

    sha1(run_id)[:10]：同机并发 run 碰撞概率可忽略；run_id 不入路径（可能很长）。
    """
    short = hashlib.sha1(run_id.encode("utf-8")).hexdigest()[:10]
    return Path(tempfile.gettempdir()) / f"orca-sidechain-{short}.pid"


def _sidechain_daemon_alive(run_id: str) -> bool:
    """探 sidechain 守护是否存活（pidfile + ``/proc/<pid>/cmdline`` 校验）。

    SPEC §5 S9：实现抽到 ``_daemon_liveness.pidfile_daemon_alive``（与 chart 守护的
    socket connect-probe 共享 helper，DRY）。本函数为薄 wrapper，保旧测试 import +
    ``cli._ensure_sidechain_daemon`` 调用点不改。

    实现语义 / pid 复用防御 / 异常策略详见 ``_daemon_liveness.pidfile_daemon_alive`` docstring
    （pidfile 不存在 / /proc 无对应 pid / cmdline 不含模块名+run_id → 一律 False 保守触发 respawn）。
    """
    return pidfile_daemon_alive(
        _sidechain_pidfile_path(run_id),
        module_name=_SIDECHAIN_MODULE_NAME,
        run_id=run_id,
    )


# ── driver（主循环：adapter → ingestor → bus.emit）────────────────────────────


class _SidechainDriver:
    """主循环：``adapter.discover_children`` + ``adapter.stream`` → ``ingestor.ingest``。

    State：
      - ``_cursors``：per-child cursor dict（in-memory；crash 丢失 → rebuild from cursor=0）。
      - 持 adapter + ingestor + host_session。

    并发：单 task 串行调；无 in-process race。跨进程 tape 写由 ``_FlockSafeTape`` flock 守。

    Lifecycle：
      - ``run()`` 永不返回（正常情况）。
      - ``CancelledError``（守护退出）→ 静默退出。
      - 其它异常 → 抛给 ``add_done_callback``（crash callback 重起 ingestor + driver）。
    """

    def __init__(
        self,
        adapter: ReadAdapter,
        ingestor: SidechainIngestor,
        host_session: str,
        *,
        poll_interval: float = _DEFAULT_POLL_SECONDS,
        since_ts: int = 0,
    ) -> None:
        self._adapter = adapter
        self._ingestor = ingestor
        self._host_session = host_session
        self._poll_interval = poll_interval
        self._since_ts = since_ts
        self._cursors: dict[str, int] = {}

    async def run(self) -> None:
        """主循环：每 ``poll_interval`` 秒扫所有子 agent 的增量事件 → ingest。

        每 iteration：
          1. ``adapter.discover_children``：发现本 host_session 的子 agent（glob / 查询）。
          2. 对每个 child：``adapter.stream``（增量）→ ``ingestor.ingest``（dedup + emit）。
          3. ``asyncio.sleep(poll_interval)``。

        异常策略：
          - ``CancelledError``：propagate（守护退出）。
          - adapter/ingestor iteration 异常：log + continue（下次重试；不退出，避免短暂故障
            如文件被删 mid-read 触发 crash 重起）。
          - 致命错误（OOM / unhandled）：进程被 OS kill → ``_ensure_sidechain_daemon`` 重起。
        """
        logger.info(
            "sidechain driver 启动（host_session=%s, poll=%.2fs, since_ts=%d）",
            self._host_session, self._poll_interval, self._since_ts,
        )
        while True:
            try:
                await self._iterate_once()
            except asyncio.CancelledError:
                logger.info("sidechain driver 被 cancel，退出")
                raise
            except Exception:
                # iteration 异常不退出（transient 错误重试，致命错误由 OS kill 触发 respawn）。
                logger.warning(
                    "sidechain driver iteration 异常（将 sleep 后重试）", exc_info=True,
                )
            await asyncio.sleep(self._poll_interval)

    async def _iterate_once(self) -> None:
        """单次扫所有子 agent。"""
        for child in self._adapter.discover_children(self._host_session, self._since_ts):
            cursor = self._cursors.get(child, 0)
            new_cursor = cursor
            try:
                for raw, nxt in self._adapter.stream(child, cursor):
                    await self._ingestor.ingest(raw)
                    new_cursor = nxt
            finally:
                # 无论 stream 正常完成还是中途抛异常，都持久化最后成功推进的 cursor。
                # 减少 crash 重起后的重读开销（即便漏推，source_id dedup 兜底不双 emit）。
                if new_cursor != cursor:
                    self._cursors[child] = new_cursor


# ── crash callback（与 chart_ingestor.make_crash_callback 同款模式）────────────


def _make_sidechain_crash_callback(
    make_driver: "_DriverFactory",
    run_id: str,
):
    """构造 ``add_done_callback``：driver crash → 重建 ingestor + 重起 driver。

    Args:
        make_driver: 0-arg factory；每次调用返一个**新的** ``_SidechainDriver``（含新的
            ``SidechainIngestor`` + 重建的 source_id set + 重置的 cursors dict）。
        run_id: 仅日志。

    行为：
      - task cancel → 静默返。
      - task 正常完成（不应发生）→ debug log，不重起。
      - task 抛异常 → log warning + 用 ``make_driver()`` 重起 + 重挂 callback（递归）。

    生产中 ``_SidechainDriver.run`` 的 ``while True`` 内 ``except Exception`` 吞 iteration
    异常（transient 重试），故此 callback 的「重起」分支主要兜底 **BaseException**（MemoryError
    等不被 ``except Exception`` 捕获）+ 备未来 driver 异常策略变更。与 ``chart_ingestor`` 同款
    防御模式（结构上对称），即便日常不常触发，仍保留以守「单点故障不灭整个守护」语义。

    重起窗口期 in-flight 事件会丢（adapter cursor 内存态丢失；source_id dedup 兜底后续重读
    不会双 emit，但 crash 那刻未 ingest 的事件可能漏）。
    """

    def _on_crash(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            logger.debug("run %s: sidechain driver 正常退出（不应发生），不重起", run_id)
            return
        logger.warning(
            "run %s: sidechain driver crash: %r — 重起中（in-flight 事件可能丢）",
            run_id, exc, exc_info=True,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        new_driver = make_driver()
        new_task = loop.create_task(
            new_driver.run(),
            name=f"orca-sidechain-driver-{run_id}-restart",
        )
        new_task.add_done_callback(_make_sidechain_crash_callback(make_driver, run_id))

    return _on_crash


# ── 守护主协程（起 driver + 监听终态/信号 + 清理）─────────────────────────────


async def _run_sidechain_daemon(
    tape_path: Path,
    run_id: str,
    host_session: str,
    flock_path: Path,
    ttl_seconds: float,
    backend: str,
    since_ts: int,
    *,
    poll_interval: float = _DEFAULT_POLL_SECONDS,
    family: str | None = None,
) -> None:
    """守护主协程（与 ``chart_daemon._run_daemon`` 同款骨架，主体新写）。

    Steps：
      1. 构造 ``_FlockSafeTape`` + ``EventBus`` + ``SidechainIngestor``（rebuild_from_tape）。
      2. ``_make_adapter(backend, host_session)`` 选 adapter（唯一 backend 分支）。
      3. ``_SidechainDriver.run`` 起 driver task + ``_make_sidechain_crash_callback``。
      4. ``_watch_terminal`` 等终态或 TTL（复用 chart_daemon）。
      5. signal handler（SIGTERM/SIGINT）→ ``signal_event.set`` → 守护退出（graceful）。
      6. finally：cancel driver + watcher + signal_waiter + bus.close + pidfile unlink。
    """
    tape = _FlockSafeTape(tape_path, run_id, flock_path=flock_path)
    bus = EventBus(tape)

    # R3.3：crash restart 一次性扫 tape 重建 source_id set。
    ingestor = SidechainIngestor(bus, tape_path)
    ingestor.rebuild_from_tape()
    logger.info(
        "run %s: sidechain ingestor rebuild（%d source_ids 已 ingest，current_node=%r）",
        run_id, len(ingestor.seen_source_ids), ingestor.current_node,
    )

    adapter = _make_adapter(backend, host_session, family=family)

    # driver factory：每次返新 driver + 新 ingestor（含 rebuild）。
    def make_driver() -> _SidechainDriver:
        new_ingestor = SidechainIngestor(bus, tape_path)
        new_ingestor.rebuild_from_tape()
        return _SidechainDriver(
            adapter, new_ingestor, host_session,
            poll_interval=poll_interval, since_ts=since_ts,
        )

    driver_task = asyncio.create_task(
        make_driver().run(),
        name=f"orca-sidechain-driver-{run_id}",
    )
    driver_task.add_done_callback(_make_sidechain_crash_callback(make_driver, run_id))

    # 信号 → event：让 watcher 提前结束（graceful），走 finally 清理。不裸 ``raise SystemExit``
    # （SPEC §3.3 grep 守门豁免：本模块是 ``sidechain_daemon.py``，但仍遵循同款 pattern）。
    loop = asyncio.get_running_loop()
    signal_event = asyncio.Event()

    def _on_signal() -> None:
        logger.info("run %s: sidechain 守护收到信号，触发 graceful 退出", run_id)
        signal_event.set()

    signal_handlers_registered: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
            signal_handlers_registered.append(sig)
        except (NotImplementedError, RuntimeError):
            # Windows / 非主线程：``add_signal_handler`` 不支持。本守护依赖 POSIX
            # （fcntl.flock / /proc / unix path），Windows 本就不支持；best-effort 跳过。
            pass

    signal_waiter = asyncio.create_task(signal_event.wait())
    watcher_task = asyncio.create_task(_watch_terminal(tape_path, ttl_seconds))

    try:
        done, _pending = await asyncio.wait(
            {watcher_task, signal_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if watcher_task in done:
            reason = watcher_task.result()
        else:
            reason = "signal"
        logger.info("run %s: sidechain 守护退出（reason=%s）—— 清理中", run_id, reason)
    except asyncio.CancelledError:
        logger.info("run %s: sidechain 守护被 cancel，清理中", run_id)
        raise
    finally:
        # 取消未完任务（watcher / signal_waiter / driver + crash callback 可能新建的
        # 递归 driver task），让 finally 跑清理。即便 ``asyncio.run`` 退出时会强 tear down
        # 残留 task，显式 cancel 提供更清晰的日志诊断（避免新 task 在 ``bus.close()`` 后
        # 尝试 emit 抛 RuntimeError 触发又一轮 crash callback 的有界螺旋）。
        tasks_to_cancel = {watcher_task, signal_waiter, driver_task}
        # crash callback 若新建了递归 driver task，按 name 匹配追入。
        for t in asyncio.all_tasks():
            if (t.get_name().startswith(f"orca-sidechain-driver-{run_id}")
                    and t not in tasks_to_cancel):
                tasks_to_cancel.add(t)
        for t in tasks_to_cancel:
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    logger.debug("run %s: 收尾时 task 抛异常", run_id, exc_info=True)
        for sig in signal_handlers_registered:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                pass
        # bus.close 关 tape 写句柄；即便 driver 还有 in-flight emit，bus 已 close 会 raise
        # （driver 下一 iteration catch 住，但因 task 已 cancel 不再跑）。
        try:
            bus.close()
        except Exception:  # noqa: BLE001
            logger.warning("run %s: 守护退出 bus.close 异常", run_id, exc_info=True)


# ── backend adapter dispatch（SPEC §0：唯一 backend 分支，grep 守门豁免）─────


def _make_adapter(
    backend: str, host_session: str, *, family: str | None = None,
) -> ReadAdapter:
    """选 adapter（唯一 ``if backend ==`` 分支；``sidechain_daemon.py`` 内允许，grep 守门豁免）。

    SPEC §0：backend 差异**只许**在 ``*_adapter.py`` + ``*_daemon.py`` 启动参数。本函数 + argv
    ``--backend`` 是「启动参数」例外；ingestor / IR / 前端零 backend 感知。

    Args:
        backend: ``"cc"`` / ``"opencode"``（argv ``--backend``）。
        host_session: 宿主 session id。
        family: SPEC §P4 家族（``"cc"``/``"cac"``/``"opencode"``/``"nga"``），由 cli 从
            config ``sidechain.family`` 读入经 argv ``--family`` 透传；None → adapter 走探测。
            **与 backend 独立**：``family`` 决定路径 dotdir（cc vs cac），``backend`` 决定 adapter
            类（CCJsonl vs OpencodeSqlite）。adapter 自身不读 config（events 层依赖铁律）。
    """
    # 用 ``backend ==`` 字面比较（grep 守门 pattern 不命中本文件 —— 本文件名是
    # ``sidechain_daemon.py``，不在 ``cc_jsonl_adapter.py`` / ``opencode_sqlite_adapter.py``
    # 通配范围；且 SPEC §0 明示 ``*_daemon.py`` 豁免）。
    if backend == "cc":
        return CCJsonlAdapter(host_session, family=family)
    if backend == "opencode":
        return OpencodeSqliteAdapter(host_session, family=family)
    # fail loud：未知 backend 不静默回退。
    raise ValueError(
        f"unknown backend {backend!r}（预期 'cc' 或 'opencode'）"
    )


# ── 入口（``python -m orca.iface.in_session.sidechain_daemon ...``）───────────


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s [sidechain-daemon]: %(message)s",
    )


def _read_run_start_ts(tape_path: Path) -> int:
    """读 tape 首条 ``workflow_started`` 的 timestamp → since_ts（过滤旧 subagent 文件）。

    缺失 / 解析失败 → 0（不过滤；source_id dedup 兜底）。
    """
    if not tape_path.is_file():
        return 0
    try:
        with open(tape_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "workflow_started":
                    ts = obj.get("timestamp")
                    if isinstance(ts, (int, float)):
                        return int(ts)
                    return 0
    except OSError:
        pass
    return 0


def main() -> int:
    """console entry：``python -m orca.iface.in_session.sidechain_daemon ...``。

    被 ``cli._spawn_sidechain_daemon`` detach spawn。argv 含 run_id + tape + backend +
    host_session + 可选 ttl/log_level/poll_interval。派生值（pidfile/flock_path）按 run_id /
    tape 派生 —— 单向信息流，子代理无法干扰。
    """
    parser = argparse.ArgumentParser(
        prog="orca-sidechain-daemon",
        description="in-session per-run 子 agent 过程 ingestor 守护（SPEC-B v4 B2）",
    )
    parser.add_argument("--run-id", required=True, help="Orca run id")
    parser.add_argument("--tape", required=True, help="run 的 tape 文件绝对路径")
    parser.add_argument(
        "--backend", required=True, choices=["cc", "opencode"],
        help="宿主 backend（cc=claude code / opencode）；选对应 adapter",
    )
    parser.add_argument(
        "--host-session", required=True,
        help="宿主 session id（CC CLAUDE_CODE_SESSION_ID / opencode ORCA_HOST_SESSION_ID）",
    )
    parser.add_argument(
        "--family", default=None,
        help="SPEC §P4 家族覆盖（cc/cac 对 backend=cc；opencode/nga 对 backend=opencode）；"
             "省略 → resolver 探测（.claude/.cac / .opencode/.nga 哪个存在）。"
             "cli 从 config sidechain.family 读入透传。",
    )
    parser.add_argument(
        "--ttl", type=int, default=_DEFAULT_TTL_SECONDS,
        help=f"守护 TTL 兜底秒数（默认 {_DEFAULT_TTL_SECONDS}s = 6h）",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=_DEFAULT_POLL_SECONDS,
        help=f"discover/stream poll 间隔秒（默认 {_DEFAULT_POLL_SECONDS}s）",
    )
    parser.add_argument("--log-level", default="INFO", help="INFO/DEBUG/WARN/ERROR")
    args = parser.parse_args()

    _setup_logging(args.log_level)

    tape_path = Path(args.tape)
    # 与 cli._flock_path 同源（import 复用，防漂移）。
    from orca.iface.in_session.cli import _flock_path
    flock_path = _flock_path(tape_path)

    # 写 pidfile（供 cli._sidechain_daemon_alive 探测）。失败不 fail spawn（probe fallback dead）。
    pidfile = _sidechain_pidfile_path(args.run_id)
    try:
        pidfile.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        logger.warning(
            "run %s: 写 pidfile %s 失败（_ensure_sidechain_daemon 探测会误判 dead 触发 respawn）",
            args.run_id, pidfile, exc_info=True,
        )

    since_ts = _read_run_start_ts(tape_path)

    logger.info(
        "run %s: sidechain 守护启动（backend=%s, host_session=%s, tape=%s, poll=%.2fs, ttl=%ss）",
        args.run_id, args.backend, args.host_session, tape_path,
        args.poll_interval, args.ttl,
    )

    try:
        asyncio.run(
            _run_sidechain_daemon(
                tape_path=tape_path,
                run_id=args.run_id,
                host_session=args.host_session,
                flock_path=flock_path,
                ttl_seconds=float(args.ttl),
                backend=args.backend,
                since_ts=since_ts,
                poll_interval=args.poll_interval,
                family=args.family,
            )
        )
    except KeyboardInterrupt:
        # 兜底（asyncio.run 在 signal handler race 时可能抛）。
        pass
    finally:
        # 进程退出前清 pidfile（graceful 路径）。
        try:
            pidfile.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


# SPEC §3.3 grep 守门：本模块是 ``sidechain_daemon.py``，对齐 ``chart_daemon.py`` 同款 pattern
# （main 返 0，进程自然退；不裸 ``sys.exit``）。
if __name__ == "__main__":
    main()
