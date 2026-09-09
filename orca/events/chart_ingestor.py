"""chart_ingestor.py —— per-run Unix socket listener（phase-13 SPEC §3）。

回答「Orca 进程怎么接收 script 推来的 chart？」：每个 run 一个 ``runs/<run_id>.sock``，
``asyncio.start_unix_server`` accept 短连接，逐行读 JSON，调 ``bus.emit("custom", ...)`` 走
**单一写路径**（EventBus → Tape），返回 ack 含分配的 seq。

协议（SPEC §3.2）：
  script → server: 单行 UTF-8 JSON ``{"node": str, "session_id": str, "payload": ChartPayload}``
  server → script: 单行 UTF-8 JSON ``{"ok": bool, "seq": int?, "error": str?}``
  强制短连接（client 发 1 帧 → 等 ack → close；server 处理完一帧后 writer.close）。

铁律（§0.1）：
  - **chart 是事件**：ingestor 唯一调用 ``bus.emit("custom", ...)``，无第二条 emit 路径（§8.5）。
  - **唯一真相源不变**：socket 是传输通道（不持久化任何状态），收到即 emit + 丢弃；
    socket 文件 run 结束删除。
  - **fail loud 优先**：malformed / 超限 → 回 ack ok=False；emit 抛 → 回 ack error；
    server task 自身崩 → ``done_callback`` 重起（SPEC §3.4）。

常量同源：``MAX_MESSAGE_BYTES`` 从 ``orca.chart._limits`` import（client lib 同源，防 drift）。

依赖单向：本模块依赖 ``orca.events.bus``（EventBus）+ ``orca.chart._limits``（纯常量）。
``_limits`` 是 chart 客户端 lib 包的常量子集（零 Orca runtime 依赖），ingestor 依赖常量层
不违反单向（chart 包不反向 import events）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orca.chart._limits import MAX_MESSAGE_BYTES
from orca.chart._paths import (
    chart_port_file_path,
    write_chart_port_file,
)

if TYPE_CHECKING:
    from orca.events.bus import EventBus

logger = logging.getLogger(__name__)

# SPEC §5.2：ingestor 端复核上限（防 client 绕过 _render 直接写 socket）。
# 两端同源常量：``orca.chart._limits.MAX_MESSAGE_BYTES``。
_MAX_INCOMING_BYTES = MAX_MESSAGE_BYTES

_IS_WINDOWS = sys.platform == "win32"


async def chart_ingestor(sock_path: Path, bus: "EventBus", run_id: str) -> None:
    """per-run chart listener（SPEC §3.1 / §3.2）。

    传输层平台分支（协议字节级不变：单行 JSON + ack，短连接）：
      - POSIX：Unix socket ``<sock_path>``（现状，零改动）。
      - Windows：Python 不暴露 ``AF_UNIX`` → TCP ``127.0.0.1:<临时端口>``（
        ``asyncio.start_server``）。端口 bind 期确定，原子写 ``<sock_path>.port``
        sidecar（``chart_port_file_path``）；cancel/异常 finally 删除。

    RunHandle 启动时 ``asyncio.create_task``；run 终态时 cancel + 端点文件由调用方
    unlink（``RunHandle._teardown_handle``）。

    Args:
        sock_path: ``<tmp>/orca-<sha1(run_id)[:10]>.sock`` 绝对路径（POSIX 用；Windows
            仅作端口文件派生源）。函数内 mkdir parent + 清 stale socket（POSIX）。
        bus: 该 run 的 EventBus（emit 走 Tape 单一写路径）。
        run_id: 仅用于日志（路由不需要——sock_path 已含 run_id 寻址）。

    Lifecycle:
      - ``serve_forever`` 永不返回（正常情况）。
      - ``CancelledError``（teardown）→ 静默退出，finally 清端点文件。
      - 其它异常 → 抛给 ``add_done_callback``（``make_crash_callback`` 限流重起）。
    """
    port_file: Path | None = None
    if _IS_WINDOWS:
        port_file = chart_port_file_path(sock_path)
        server = await asyncio.start_server(
            _make_handler(bus, run_id),
            host="127.0.0.1",
            port=0,  # 内核分配临时端口，写 sidecar 供两端发现
            limit=MAX_MESSAGE_BYTES + 1024,
        )
        port = server.sockets[0].getsockname()[1]
        write_chart_port_file(port_file, port)
    else:
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        if sock_path.exists():
            # stale socket（前次 run crash 残留）。SPEC §3.2 注释：先 unlink 再 bind，
            # 否则 ``start_unix_server`` 报 AddressAlreadyInUse。
            logger.info("run %s: 清理 stale socket %s", run_id, sock_path)
            sock_path.unlink()

        server = await asyncio.start_unix_server(
            _make_handler(bus, run_id),
            path=str(sock_path),
            # SPEC §5.2：单条消息上限 2MB。asyncio ``StreamReader.readline`` 默认 64KB，
            # 必须显式提到 ``MAX_MESSAGE_BYTES``——否则 ~64KB-2MB 区间的合法 chart payload 会被
            # asyncio 误拒（LimitOverrunError）。设为 2MB+1024 后：
            #   - ≤ 2MB 合法 payload：handler 内 size check 放行（< MAX_MESSAGE_BYTES）
            #   - 2MB < payload ≤ 2MB+1024：handler 内 size check reject（ack ok=False "too large"）
            #   - > 2MB+1024：readline 抛 LimitOverrunError → handler 通用 except catch → ack
            #     可能因 stream 异常对 client 不可读（client 看到 EOF/timeout，仍 fail loud 在
            #     ``_render.py`` 的 ``ack_raw==b""`` / ``socket.timeout`` 路径）。SPEC §5.2 核心
            #     契约「不写 tape」始终满足。
            limit=MAX_MESSAGE_BYTES + 1024,
        )
    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        # teardown：正常退出路径（RunHandle cancel）。
        pass
    finally:
        # 兜底清端点文件（即使 serve_forever 因异常退出）。
        try:
            if port_file is not None:
                port_file.unlink(missing_ok=True)
            else:
                sock_path.unlink(missing_ok=True)
        except OSError as e:  # noqa: BLE001 — unlink 失败不应阻塞收尾
            logger.warning("run %s: 端点文件清理失败 %s: %r", run_id, sock_path, e)


def _make_handler(bus: "EventBus", run_id: str):
    """构造 ``start_unix_server`` 的 client_connected_callback（闭包绑 bus + run_id）。"""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """处理一条短连接：readline → 校验 → emit → ack → close。

        每条消息独立（短连接，无 keep-alive）。任何异常都通过 ack 回给 client（fail loud）；
        server 不断（继续服下一条）。
        """
        try:
            line = await reader.readline()
            # SPEC §5.2：先查大小（防 client 端绕过 _render 直接发巨型 payload）。
            if len(line) > _MAX_INCOMING_BYTES:
                await _ack(writer, ok=False, error=f"payload too large: {len(line)} bytes")
                return

            # 空行（client 提前 close）→ 静默返回（不是错误，但记 debug）。
            if not line:
                logger.debug("run %s: client 提前 close（空行）", run_id)
                return

            try:
                msg = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                await _ack(writer, ok=False, error=f"invalid JSON: {type(e).__name__}: {e}")
                return

            # SPEC §3.2 协议契约：node/session_id 是 str，payload 是 dict（ChartPayload）。
            node = msg.get("node")
            sid = msg.get("session_id")
            payload = msg.get("payload")
            if not (isinstance(node, str) and isinstance(sid, str) and isinstance(payload, dict)):
                await _ack(writer, ok=False, error="malformed message: node/session_id 必为 str, payload 必为 dict")
                return

            # emit 走单一写路径（EventBus → Tape.append）。emit 内部分配 seq 并落盘。
            # 失败（如 tape 已 close）→ 抛，外层捕获回 ack error。
            event = await bus.emit(
                "custom",
                {"kind": "chart", "chart": payload},
                node=node,
                session_id=sid,
            )
            await _ack(writer, ok=True, seq=event.seq)
        except Exception as e:  # noqa: BLE001 — 任何异常都回 ack（fail loud + 不断 server）
            logger.warning("run %s: ingestor handle 异常: %r", run_id, e, exc_info=True)
            try:
                await _ack(writer, ok=False, error=f"{type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001 — ack 写也失败（client 已 close / pipe broken）
                logger.warning("run %s: ack 写失败（client 可能已 close）", run_id, exc_info=True)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 — broken pipe 等，已尽力 ack
                pass

    return handle


async def _ack(
    writer: asyncio.StreamWriter,
    *,
    ok: bool,
    seq: int | None = None,
    error: str | None = None,
) -> None:
    """写单行 JSON ack。SPEC §3.2 server → script 协议。"""
    msg: dict[str, Any] = {"ok": ok}
    if seq is not None:
        msg["seq"] = seq
    if error is not None:
        msg["error"] = error
    writer.write((json.dumps(msg) + "\n").encode("utf-8"))
    await writer.drain()


def make_crash_callback(
    sock_path: Path, bus: "EventBus", run_id: str,
    *, max_restarts: int = 5, window_seconds: float = 60.0,
):
    """构造 ``add_done_callback``（SPEC §3.4 crash 恢复 + **限流熔断**）。

    用法（RunHandle 启动时）::

        task = asyncio.create_task(chart_ingestor(...))
        task.add_done_callback(make_crash_callback(sock_path, bus, run_id))

    行为：
      - task 正常 cancel（teardown）→ 静默返回。
      - task 抛异常 → log warning + 清 stale 端点 + 创建新 task 重起 ingestor
        + 把**本闭包**挂上新 task（重起链全链共享同一 ``crash_times``，计数不随重起清零）。
      - **熔断**（2026-09-08）：滚动窗口（默认 60s）内 crash > ``max_restarts``（默认 5）次
        → 放弃重起 + ``logger.error`` fail loud。旧实现无限重起，Windows 上
        ``start_unix_server`` 缺失这类「恒崩」错误会以每秒数千次的速度空转饿死事件循环
        （run 停摆 + 日志刷盘），熔断保证最坏退化 = chart 不可用而非整个 run 不可用。
      - **重起窗口期 in-flight chart 会丢**（SPEC §0.1 #4：socket 仅传输，不保证 exactly-once）。
      - **重起不更新 RunHandle 字段**（teardown 走 sock unlink + name 找 task 兜底）。

    熔断状态粒度（D3 修订）：``crash_times`` 是**本工厂调用的闭包变量**——web 与
    in-session 均为每 run 一次工厂调用，per-run 链共享同一列表（run A 的 crash 计数
    不污染 run B；重起链经同一闭包自挂接，计数跨重起持续）。禁模块级全局。
    """
    crash_times: list[float] = []  # 闭包滚动窗口（monotonic 秒；重起链共享同一列表）

    def _on_crash(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            # serve_forever 永不返回，正常退出不应发生；记 debug 不重起（防无限循环）。
            logger.debug("run %s: ingestor 正常退出（不应发生），不重起", run_id)
            return
        now = time.monotonic()
        while crash_times and now - crash_times[0] > window_seconds:
            crash_times.pop(0)
        crash_times.append(now)
        if len(crash_times) > max_restarts:
            logger.error(
                "run %s: chart_ingestor %ss 内 crash %d 次（上限 %d）——熔断，不再重起。"
                "chart 推送对本 run 不可用（不影响 run 推进）；根因见最后一条 crash 日志：%r",
                run_id, window_seconds, len(crash_times), max_restarts, exc,
                exc_info=exc,  # 恒崩根因定位依赖 traceback（fail loud，非静默限流）
            )
            return
        logger.warning(
            "run %s: chart_ingestor crash: %r — 重起中（%d/%d，in-flight chart 可能丢）",
            run_id, exc, len(crash_times), max_restarts,
            exc_info=exc,
        )
        try:
            sock_path.unlink(missing_ok=True)
        except OSError as e:  # noqa: BLE001
            logger.warning("run %s: crash 重起前 unlink %s 失败: %r", run_id, sock_path, e)
        # 在 task 所在 loop 创建新 task。done_callback 在 task 完成的线程上被调用
        # （asyncio task 同 loop），故 ``get_running_loop`` 安全（Python 3.10+）。
        # 防御：若 loop 已关（极端，不应发生），fallback 创建新 loop（避免 crash 重起本身崩溃）。
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        new_task = loop.create_task(
            chart_ingestor(sock_path, bus, run_id),
            name=f"orca-chart-ingestor-{run_id}-restart",
        )
        # D3 修订：挂**本闭包**（不重新走工厂）——重起链共享同一 ``crash_times``，
        # 否则每代重起新建闭包/列表，计数永远清零，熔断成为死代码。
        new_task.add_done_callback(_on_crash)

    return _on_crash
