"""wait.py —— WaitExecutor（asyncio.sleep 节点，可被 Ctrl+G 打断，SPEC §9.7）。

回答「怎么跑一个 wait node？」：``asyncio.sleep(duration)``，``interruptible=True`` 时
注册一个 ``asyncio.Event`` 到 wait-handle registry，InterruptHandler 收到 Ctrl+G 调
``bus.notify_all_waits()`` 把它 ``set()``，wait 立即结束（``interrupted=True``）。

执行流程（SPEC §9.7.4）：
  1. ``session_id = uuid4().hex``（入口生成，铁律 5）
  2. ``yield node_started{kind:"wait"}``
  3. ``duration_str = render_template(node.duration, ctx)``（Jinja2 渲染）
  4. ``duration = parse_duration(duration_str)`` —— 非法 → ``node_failed{RenderError}``；
     超过 ``MAX_DURATION`` → ``node_failed{ConfigError}``（硬上限防配置错）
  5. ``yield wait_started{duration_seconds, reason}``
  6. ``interruptible=True``：``register_wait_handle`` → ``asyncio.wait([sleep, evt.wait()],
     FIRST_COMPLETED)`` → cancel pending → ``unregister_wait_handle``（finally）。
     ``interruptible=False``：``await asyncio.sleep(duration)``。
  7. ``yield wait_completed{elapsed_seconds, interrupted}``
  8. ``yield node_completed{output:{interrupted}, elapsed}``

关键约束（SPEC §9.7.5）：
  - 非法 duration / 超上限 → ``node_failed``（fail loud，不静默跳过）。
  - ``interruptible=False`` 必须等满，Ctrl+G 不打断（等下一 node 边界）。
  - parallel group 内的 wait 与其他 branch 并行 sleep，互不干扰（每个 wait 各自的 handle）。

依赖单向（铁律 2，SPEC §7.0）：exec/ 不直接持有 events bus —— 本模块只依赖
``WaitHandleRegistry`` Protocol（register/unregister 一个 handle 的狭窄能力，定义在本模块）。
events bus 结构化实现该 Protocol（duck typing），``make_executor`` 把 orchestrator 持有
的 bus 实例透传进来。executor 无法经此 Protocol 写 tape / emit —— 能力被裁剪到最小。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Protocol

from orca.exec.context import RunContext
from orca.exec.error import ExecError
from orca.exec.interface import Executor
from orca.exec.render import render_template
from orca.schema import Event, WaitNode

logger = logging.getLogger(__name__)

# 24h 硬上限：防配置错（如把 "30s" 写成 "30d"），超过直接 node_failed（SPEC §9.7.5）。
MAX_DURATION_SECONDS = 24 * 60 * 60

# 支持的单位 → 乘数（小写化后匹配）。
_DURATION_UNITS: dict[str, float] = {
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}


class WaitHandleRegistry(Protocol):
    """WaitExecutor 需要的「wait-handle 注册」能力（SPEC §9.7.6，依赖倒置）。

    为什么要 Protocol 而非直接依赖 events bus？铁律 2（SPEC §7.0）：exec/ 不持有
    events bus —— executor 产出 ``AsyncIterator[Event]``，写 tape / emit 归 orchestrator。
    但 WaitExecutor 的 interruptible 路径要把一个 ``asyncio.Event`` 注册到某处，让
    InterruptHandler 能经 ``notify_all_waits`` 打断它。这是「注册一个可被打断的 handle」的
    狭窄能力，与「写真相源」无关——抽成 Protocol 让 exec/ 依赖**能力**而非具体 bus，
    满足 ISP / DIP（events bus 结构化实现该 Protocol，duck typing，无需显式声明）。

    ``make_executor`` 把 orchestrator 持有的 bus 实例作为 ``WaitHandleRegistry``
    传入（结构化兼容）；executor 无法经此 Protocol 写 tape / emit —— 能力被裁剪到最小。
    """

    def register_wait_handle(self, handle: asyncio.Event) -> None: ...
    def unregister_wait_handle(self, handle: asyncio.Event) -> None: ...


def parse_duration(s: str) -> float:
    """``"30s" → 30.0`` / ``"5m" → 300.0`` / ``"2h" → 7200.0`` / ``"1d" → 86400.0`` / ``"30" → 30.0``。

    纯数字（无单位）= 秒。非法（空 / 未知单位 / 非数字）→ ``ValueError``。
    """
    text = s.strip().lower()
    if not text:
        raise ValueError("empty duration")
    # 纯数字 = 秒（最后一个字符是数字）
    if text[-1].isdigit():
        try:
            return float(text)
        except ValueError as e:  # pragma: no cover - float() 对 isdigit() 末位不会失败
            raise ValueError(f"invalid duration {s!r}: {e}") from e
    unit = text[-1]
    mult = _DURATION_UNITS.get(unit)
    if mult is None:
        raise ValueError(f"unknown duration unit {unit!r} in {s!r}")
    number_part = text[:-1].strip()
    try:
        value = float(number_part)
    except ValueError as e:
        raise ValueError(f"invalid duration {s!r}: {e}") from e
    if value < 0:
        raise ValueError(f"negative duration {s!r}")
    return value * mult


class WaitExecutor(Executor):
    """``asyncio.sleep`` 实现 wait node，``interruptible=True`` 时可被 Ctrl+G 打断（SPEC §9.7.4）。

    需要一个 ``WaitHandleRegistry``：``interruptible`` 路径要 ``register_wait_handle`` /
    ``unregister_wait_handle``（InterruptHandler 经 ``notify_all_waits`` 打断）。
    registry 由 ``make_executor`` 注入（orchestrator 持有的 bus 实例，结构化满足 Protocol）。
    """

    def __init__(self, bus: WaitHandleRegistry):
        self._bus = bus

    async def exec(self, node: WaitNode, ctx: RunContext) -> AsyncIterator[Event]:
        """执行 wait node，产出完整生命周期事件流（SPEC §9.7.4）。"""
        session_id = uuid.uuid4().hex
        start = time.monotonic()

        def _ev(event_type: str, data: dict[str, Any]) -> Event:
            return Event(
                seq=0,  # 占位：orchestrator 在 tape.append 时重分配（决策 2）
                type=event_type,  # type: ignore[arg-type]
                timestamp=time.time(),
                node=node.name,
                session_id=session_id,
                data=data,
            )

        yield _ev("node_started", {"kind": "wait", "duration": node.duration})

        # 渲染 + 解析 duration（非法 → raise ExecError(phase=render)；与 set_node 对称，
        # 走标准 except ExecError emit 分支，确保 classifier 能正确分类）
        try:
            try:
                duration_str = render_template(node.duration, ctx)
                duration_seconds = parse_duration(duration_str)
            except ValueError as e:
                raise ExecError(
                    phase="render",
                    message=f"invalid duration {node.duration!r}: {e}",
                ) from e

            # 硬上限（防配置错，SPEC §9.7.5）
            if duration_seconds > MAX_DURATION_SECONDS:
                raise ExecError(
                    phase="config",
                    message=(
                        f"duration {duration_seconds}s exceeds max "
                        f"{MAX_DURATION_SECONDS}s (24h)"
                    ),
                )

            yield _ev(
                "wait_started",
                {"duration_seconds": duration_seconds, "reason": node.reason},
            )

            interrupted = False
            if node.interruptible:
                # 注册 wait handle：InterruptHandler 收到 Ctrl+G 时 registry.notify_all_waits()
                # 把它 set()，asyncio.wait FIRST_COMPLETED 立即返回（interrupted=True）。
                interrupt_evt = asyncio.Event()
                self._bus.register_wait_handle(interrupt_evt)
                try:
                    sleep_task = asyncio.create_task(
                        asyncio.sleep(duration_seconds), name=f"wait-sleep-{node.name}"
                    )
                    int_task = asyncio.create_task(
                        interrupt_evt.wait(), name=f"wait-interrupt-{node.name}"
                    )
                    done, pending = await asyncio.wait(
                        {sleep_task, int_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    # await 已取消的 pending 任务，让取消传播落地（防「Task was destroyed but
                    # pending」警告）；CancelledError / 其他异常都吞掉（结果不重要）。
                    for task in pending:
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                    interrupted = int_task in done
                finally:
                    self._bus.unregister_wait_handle(interrupt_evt)
            else:
                # 不可打断：必须等满（Ctrl+G 等下一 node 边界生效，与中断系统既有契约一致）。
                await asyncio.sleep(duration_seconds)

            elapsed = time.monotonic() - start
            yield _ev(
                "wait_completed",
                {"elapsed_seconds": elapsed, "interrupted": interrupted},
            )
            yield _ev(
                "node_completed",
                {"output": {"interrupted": interrupted}, "elapsed": elapsed},
            )

        except ExecError as e:
            elapsed = time.monotonic() - start
            err_data = {
                "kind": e.kind.value,
                "error_type": e.error_type,
                "message": e.message,
                "phase": e.phase,
            }
            yield _ev("node_failed", err_data)
            yield _ev("error", err_data)

