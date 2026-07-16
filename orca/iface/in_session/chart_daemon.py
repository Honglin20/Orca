"""chart_daemon.py —— in-session 路径的 per-run chart ingestor 守护进程（phase-13 §3 in-session 衔接）。

**回答的问题**：web/tars-run 路径下 ``ClaudeExecutor`` spawn 子进程时一次性注入 ``ORCA_*`` env
+ 起 per-run ingestor；in-session 路径下节点子代理由**宿主 session（opencode/CC）派发**，
不经 executor，env 缺、ingestor 也没人起 → ``render_chart`` 在 env 检查处 raise。本模块补这个缺口：
``orca <wf>`` bootstrap 时 detach 起一个**脱离 bootstrap CLI 存活**的守护进程，bind 该 run 的
chart socket + 跑 ``chart_ingestor`` 收 script 推来的 chart，经 bus 落 tape。

**为什么是独立进程**：bootstrap / next 都是一次性 CLI（per-call），子代理执行发生在宿主 session
的时间窗里（``orca next`` 之间），任何 Orca CLI 进程都不能持有 ingestor。守护必须跨 bootstrap
退出存活 → ``start_new_session=True`` 脱离 controlling terminal + process group。

**生命周期**：
  - bootstrap 启动（``cli._spawn_chart_daemon``）；
  - 自退条件 1：tail 该 run 的 tape 见 ``workflow_completed`` / ``workflow_failed`` /
    ``workflow_cancelled`` 即退（``_watch_terminal``）；
  - 自退条件 2（兜底）：TTL（默认 6h）超时 → 强退（防任何 bug 导致的泄漏）；
  - 退出前：cancel ingestor task（其 finally ``unlink`` socket）+ ``bus.close`` + 兜底
    ``sock_path.unlink(missing_ok=True)``。

**跨进程 tape 写协调（"单一写路径" 铁律不破）**：in-session 下两类进程写同一 tape ——
  ① 本守护（script 推 chart → ``bus.emit("custom", ...)``）；
  ② ``orca next`` / ``orca stop`` CLI（emit ``node_completed`` / ``route_taken`` / ``workflow_completed`` 等）。
时序上几乎不重叠（subagent 完成后 host 才 next），但**正确性靠互斥**：本守护用
``_FlockSafeTape``（``Tape`` 子类）在每次 ``append`` 前 ``fcntl.flock(LOCK_EX)`` 阻塞抢
``<tape>.lock`` + 从 disk 刷新 ``_last_seq``，与 ``cli._try_acquire_flock`` 同锁文件、同路径。
Web/tars-run 路径继续用基类 ``Tape``（零改动，OCP：扩展不修改核心）。

**协议不变**：``chart_ingestor(sock_path, bus, run_id)`` 协议逻辑（短连接 + JSON 行 + ack）
零改动；本守护仅提供「正确 tape 写者」+「生命周期守护」，协议常量同源 ``orca.chart._limits``。

依赖单向：本模块依赖 ``orca.events.{bus,tape,chart_ingestor}`` + ``orca.chart._paths`` +
stdlib（asyncio/fcntl/json/...）。是 iface 层，符合 schema→compile→exec→run→events→iface 铁律。
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import signal
import sys
import time
from pathlib import Path

from orca.chart._paths import chart_sock_path
from orca.events.bus import EventBus
from orca.events.chart_ingestor import chart_ingestor, make_crash_callback
from orca.events.tape import Tape

logger = logging.getLogger(__name__)

# SPEC §3.1 in-session 衔接：守护自退 TTL 兜底（防泄漏）。6h 覆盖任何合理的长跑 workflow；
# 真实运行通常由 ``_watch_terminal`` 在终态事件时早退。CLI ``--ttl`` 可覆盖（测试用短值）。
_DEFAULT_TTL_SECONDS = 6 * 3600

# ``_watch_terminal`` 的 tape 增量 poll 间隔：平衡「终态感知延迟」与「IO 频率」。2s 足够
# （主 session 调 next 间隔通常 ≥10s；chart 实时性由 socket 直接保证，与此 poll 无关）。
_WATCH_POLL_SECONDS = 2.0

# 终态事件类型（与 ``run_manager._follow_tape`` 同集）：见到任一即守护退出。
_TERMINAL_EVENT_TYPES = ("workflow_completed", "workflow_failed", "workflow_cancelled")


class _FlockSafeTape(Tape):
    """跨进程安全的 Tape（in-session chart 守护专用）。

    ``append`` / ``append_batch`` override：在基类的 in-process ``asyncio.Lock`` 之外，再加
    一层**跨进程** ``fcntl.flock(LOCK_EX)``（阻塞，与 ``cli._try_acquire_flock`` 互斥），并在
    抢锁后、调基类 ``append`` 前从 disk 重算 ``_last_seq`` —— 因为上次 chart 落盘与本次之间，
    ``orca next`` CLI 可能已 append 多行（``node_completed``/``route_taken``/``node_started``）。

    为什么不共享基类 ``_last_seq``：基类 ``Tape`` 的 ``_last_seq`` 是 in-memory counter，仅在同
    进程并发下正确；跨进程没有共享内存。disk 重算保证守护每次 append 都基于 tape 真实末态。

    **构造用 ``resume=False``**：基类 ``resume=True`` 会调 ``_truncate_trailing_partial`` 在 flock
    外 ``read_text``/``write_text`` 触发潜在截断（跨进程写契约违反）。``resume=False`` 仅读扫
    （``_scan_last_seq`` + 末尾残行 warning），且守护首次 ``append`` 会用 ``_read_max_seq_from_disk``
    重置 ``_last_seq``，构造时的扫描值被覆盖（无害）。

    Web/tars-run 路径（单进程 orchestrator 持 ingestor）继续用基类 ``Tape`` —— 零回归。
    """

    def __init__(self, path: Path, run_id: str, *, flock_path: Path) -> None:
        super().__init__(path, run_id=run_id, resume=False)
        self._flock_path = Path(flock_path)
        # 增量扫描缓存（``_read_max_seq_from_disk`` 用）：首次全扫，之后 O(delta)。
        # 守护进程内单实例，append 之间状态保留 → 高频 chart 不重扫全 tape。
        self._scan_offset: int = 0
        self._scan_max_seq: int = 0

    async def append(self, event_data: dict) -> int:
        lock_fd = open(self._flock_path, "w")
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)  # 阻塞；CLI 持锁时等
            self._last_seq = self._read_max_seq_from_disk()
            return await super().append(event_data)
        finally:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            finally:
                lock_fd.close()

    async def append_batch(self, items: list[dict]) -> list[int]:
        # 守护只走单条 ``append``（chart ingestor 每消息一 emit），但 batch 也正确覆写以保完整。
        lock_fd = open(self._flock_path, "w")
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            self._last_seq = self._read_max_seq_from_disk()
            return await super().append_batch(items)
        finally:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            finally:
                lock_fd.close()

    def _read_max_seq_from_disk(self) -> int:
        """增量扫描 tape 取 max seq（首次 O(N)，之后 O(delta)，0 if missing/empty）。

        持 flock 调用：此时无其他写者，文件状态一致。维护实例级 ``_scan_offset`` +
        ``_scan_max_seq`` 缓存：每次仅读「上次 offset → EOF」的新字节，找新行中的 max seq
        累加进缓存。partial-line race 防护同 ``_watch_terminal``：仅解析到最后一行完整
        （含 ``\\n``），partial 尾字节不推进 offset，下次重读。

        性能（相比 O(N) 全扫）：典型 30k 行 tape + 5 chart/run → 首次 ~15ms，后续每次
        <1ms。chart-heavy 长跑（100k+ 行 + 高频 chart）从「每次 ~50ms × N chart」降到
        「首次 ~50ms + 每次 <1ms × N chart」，消除 flock 持有时间随 tape 增长的线性扩张。
        """
        try:
            cur_size = self.path.stat().st_size
        except FileNotFoundError:
            self._scan_offset = 0
            self._scan_max_seq = 0
            return 0
        except OSError:
            self._scan_offset = 0
            self._scan_max_seq = 0
            return 0

        if cur_size < self._scan_offset:
            # tape 被截断（不应发生）→ 重置缓存，下次全扫。
            self._scan_offset = 0
            self._scan_max_seq = 0

        if cur_size == self._scan_offset:
            # 无新字节 → 返缓存（O(1)）。
            return self._scan_max_seq

        # 读新字节（从 _scan_offset 到 EOF）。
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                f.seek(self._scan_offset)
                chunk = f.read(cur_size - self._scan_offset)
        except OSError:
            logger.warning("chart 守护读 tape %s max_seq 失败（OSError）",
                           self.path, exc_info=True)
            return self._scan_max_seq

        # partial-line race 防护：仅推进到 chunk 中最后一个 \n 之后。
        last_nl = chunk.rfind("\n")
        if last_nl < 0:
            # 整段 partial；不推进 offset，下次重读。
            return self._scan_max_seq
        complete = chunk[: last_nl + 1]
        self._scan_offset = self._scan_offset + last_nl + 1

        for line in complete.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue  # 完整行解析失败 → 损坏行，跳过（不影响 max_seq）
            seq = obj.get("seq")
            if isinstance(seq, int) and seq > self._scan_max_seq:
                self._scan_max_seq = seq
        return self._scan_max_seq


async def _watch_terminal(
    tape_path: Path,
    ttl_seconds: float,
    *,
    poll_interval: float = _WATCH_POLL_SECONDS,
) -> str | None:
    """tail tape 监听终态事件，返退出原因（``"terminal"`` / ``"ttl"``）。

    增量读（按 ``stat().st_size`` 增量）：首次从 offset=0 扫已存在内容（覆盖「守护启动时
    run 已终态」的边界，如毫秒级 workflow），之后按 size 增量读，避免每 poll 全扫 tape。
    终态事件出现即返 ``"terminal"``；TTL 超时返 ``"ttl"``。

    ``poll_interval``：测试可传短值加速；生产用默认 ``_WATCH_POLL_SECONDS``。

    **partial-line race 防护**：``last_size`` 仅推进到本次 chunk 的最后一个 ``\\n`` 之后；
    末尾不足一行（无 ``\\n``）的字节不推进 ``last_size``，下个 poll 重读。理由：POSIX
    ``write(2)`` 对普通文件**不保证原子性**（仅 PIPE_BUF 对管道保证），若 poll 落在 write
    中途（已写 N 字节但行尾 ``\\n`` 未写），chunk 是 partial JSON。直接 ``last_size=cur_size``
    会丢这行，**终态事件可能永远丢失 → 守护 6h TTL 才退**。推进到 ``\\n`` 保证 partial 尾
    被下次重读，终态事件最终必被捕获。

    容错：tape 文件消失 / 缩小（理论不应发生）→ 重置 last_size=0 重跟，不崩。
    """
    deadline = time.monotonic() + ttl_seconds
    last_size = 0   # 首次从 0 起，扫已存在内容（边界鲁棒）

    while True:
        if time.monotonic() > deadline:
            return "ttl"
        try:
            cur_size = tape_path.stat().st_size
        except FileNotFoundError:
            # tape 消失（用户清理 runs/）→ 等 TTL 兜底；不立即退（可能短暂）。
            await asyncio.sleep(poll_interval)
            continue
        except OSError:
            await asyncio.sleep(poll_interval)
            continue

        if cur_size < last_size:
            # tape 被截断/重建（不应发生）→ 重跟。
            last_size = 0
        if cur_size > last_size:
            try:
                with open(tape_path, "r", encoding="utf-8") as f:
                    f.seek(last_size)
                    chunk = f.read(cur_size - last_size)
            except OSError:
                logger.debug("chart 守护 _watch_terminal 读 %s 失败（OSError，下个 poll 重试）",
                             tape_path, exc_info=True)
                chunk = ""

            # partial-line race 防护：只解析到最后一行完整（含 \\n）；末尾 partial 字节
            # 保留在 last_size 之前，下次 poll 重读。
            last_nl = chunk.rfind("\n")
            if last_nl < 0:
                # chunk 内无换行 → 整段 partial；不推进 last_size，等下个 poll。
                await asyncio.sleep(poll_interval)
                continue
            complete = chunk[: last_nl + 1]
            last_size = last_size + last_nl + 1
            for line in complete.split("\n"):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue  # 完整行（含 \\n）却解析失败 → 损坏，跳过（不致命）
                if obj.get("type") in _TERMINAL_EVENT_TYPES:
                    return "terminal"
        await asyncio.sleep(poll_interval)


async def _run_daemon(
    tape_path: Path,
    run_id: str,
    sock_path: Path,
    flock_path: Path,
    ttl_seconds: float,
) -> None:
    """守护主协程：起 ingestor + 监听终态/信号 + 清理。

    步骤：
      1. 构造 ``_FlockSafeTape`` + ``EventBus``；
      2. ``asyncio.create_task(chart_ingestor(sock_path, bus, run_id))`` bind socket + 收 chart；
         ``make_crash_callback`` 挂 ``add_done_callback``（SPEC §3.4 自重起）；
      3. ``_watch_terminal`` 等终态或 TTL；
      4. SIGTERM/SIGINT 经 ``loop.add_signal_handler`` → set ``signal_event`` → 守护退出
         （graceful：finally 跑清理 unlink socket）；不裸 ``raise SystemExit``（SPEC §3.3
         守门：除 ``iface/exit_codes.py`` / ``__main__.py`` 外禁裸退出）；
      5. finally：cancel ingestor task（其 finally ``unlink`` socket）+ ``bus.close``。
    """
    tape = _FlockSafeTape(tape_path, run_id, flock_path=flock_path)
    bus = EventBus(tape)

    ingestor_task = asyncio.create_task(
        chart_ingestor(sock_path, bus, run_id),
        name=f"orca-in-session-chart-ingestor-{run_id}",
    )
    ingestor_task.add_done_callback(make_crash_callback(sock_path, bus, run_id))

    # 信号 → event：让 _watch_terminal 提前结束，走正常 finally 清理（不裸 SystemExit）。
    loop = asyncio.get_running_loop()
    signal_event = asyncio.Event()

    def _on_signal() -> None:
        logger.info("run %s: chart 守护收到信号，触发 graceful 退出", run_id)
        signal_event.set()

    signal_handlers_registered: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
            signal_handlers_registered.append(sig)
        except (NotImplementedError, RuntimeError):
            # Windows / 非主线程：``add_signal_handler`` 不支持。本守护依赖 POSIX
            # （fcntl.flock / unix socket），Windows 本就不支持；best-effort 跳过。
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
        logger.info("run %s: chart 守护退出（reason=%s）—— 清理中", run_id, reason)
    except asyncio.CancelledError:
        logger.info("run %s: chart 守护被 cancel，清理中", run_id)
        raise
    finally:
        # 取消未完任务（watcher / signal_waiter / ingestor），让 finally 跑清理。
        for t in (watcher_task, signal_waiter):
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    # watcher / signal_waiter 正常不应抛；记 debug 让排查可见（不阻塞退出）。
                    logger.debug("run %s: 收尾时 task 抛异常", run_id, exc_info=True)
        for sig in signal_handlers_registered:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                pass
        if not ingestor_task.done():
            ingestor_task.cancel()
            try:
                await ingestor_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                # ingestor task 自身异常已由 ``make_crash_callback`` 处理；此处仅确保退出。
                logger.debug("run %s: ingestor 收尾抛异常（已由 crash callback 处理）",
                             run_id, exc_info=True)
        # ``chart_ingestor`` 的 finally 已 unlink socket；此处幂等兜底（crash 重起路径可能漏）。
        try:
            sock_path.unlink(missing_ok=True)
        except OSError as e:  # noqa: BLE001
            logger.warning("run %s: 守护退出 unlink %s 失败: %r", run_id, sock_path, e)
        try:
            bus.close()
        except Exception:  # noqa: BLE001
            logger.warning("run %s: 守护退出 bus.close 异常", run_id, exc_info=True)


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s [chart-daemon]: %(message)s",
    )


def main() -> int:
    """console entry：``python -m orca.iface.in_session.chart_daemon --run-id X --tape Y``。

    被 ``cli._spawn_chart_daemon`` detach spawn。argv 仅含 run_id + tape（+ 可选 ttl/log_level），
    所有派生值（sock_path / flock_path）按 run_id / tape 派生 —— 单向信息流，子代理无法干扰。
    """
    parser = argparse.ArgumentParser(
        prog="orca-chart-daemon",
        description="in-session per-run chart ingestor 守护（由 orca bootstrap detach 启动）",
    )
    parser.add_argument("--run-id", required=True, help="Orca run id")
    parser.add_argument("--tape", required=True, help="run 的 tape 文件绝对路径")
    parser.add_argument(
        "--ttl", type=int, default=_DEFAULT_TTL_SECONDS,
        help=f"守护 TTL 兜底秒数（默认 {_DEFAULT_TTL_SECONDS}s = 6h）",
    )
    parser.add_argument("--log-level", default="INFO", help="INFO/DEBUG/WARN/ERROR")
    args = parser.parse_args()

    _setup_logging(args.log_level)

    tape_path = Path(args.tape)
    sock_path = chart_sock_path(args.run_id)
    # 与 cli._flock_path 同源：tape 同目录加 ``.lock`` 后缀。cli import 复用保证不漂移。
    from orca.iface.in_session.cli import _flock_path
    flock_path = _flock_path(tape_path)

    logger.info(
        "run %s: chart 守护启动（tape=%s, sock=%s, ttl=%ss）",
        args.run_id, tape_path, sock_path, args.ttl,
    )
    try:
        asyncio.run(
            _run_daemon(tape_path, args.run_id, sock_path, flock_path, float(args.ttl))
        )
    except KeyboardInterrupt:
        # asyncio.run 在某些边界（如 signal handler 注册 race）可能抛 KeyboardInterrupt；
        # 兜底清理 socket 即可（守护退出码不参与 CI 契约，无人检查）。
        pass
    finally:
        # 进程退出前最终兜底：socket 文件清理（即便协程 finally 漏跑）。
        try:
            sock_path.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


# 入口：``python -m orca.iface.in_session.chart_daemon ...``。
# 不用 ``sys.exit(main())`` —— SPEC §3.3 grep 守门禁裸 ``sys.exit`` 在 ``iface/exit_codes.py``
# 与 ``__main__.py`` 之外；本模块是 ``chart_daemon.py``。main() 返 0，进程自然退出 0；
# 守护退出码不参与 CI 契约（无人据其判断 workflow 结果）。
if __name__ == "__main__":
    main()
