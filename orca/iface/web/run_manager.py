"""run_manager.py —— 多 run 真并发托管 + 懒加载元数据（SPEC §2）。

回答「后端怎么托管多个并发 run？」：每个 run 一个独立 ``RunHandle``（bus + tape +
gate_handler 全隔离），``RunManager`` 用 ``asyncio.Semaphore(max_concurrent)`` 真并发
跑（默认 3），超过的 queued。``list_runs`` 只返回**元数据**（不含事件，懒加载红线），
元数据从 ``replay_state(tape)`` 派生（保证与唯一真相源一致）。

设计规则（SPEC §0.1 五条铁律 / §2.3 / §9 决策）：
  - **每个 run 独立 bus + tape + gate_handler**（隔离：多 run 不串事件/gate，§9 决策 5）。
  - **真并发**：``_sem`` 限制同时跑的 run 数，sem 内 ``asyncio`` 自然并发（不是单活跃）。
  - **元数据从 tape 派生**：progress/cost 不另存，从 ``replay_state(handle.tape)`` 算
    （§9 决策 6，保证与真相源一致——断言覆盖）。
  - **懒加载**：``list_runs`` 只 ``RunMeta``，事件走 ``get_run_events`` → ``tape.replay()``
    （§0.1 铁律 2）。
  - **后端无并行内存事件 list**：本模块不维护 ``events: list``（§0.1 铁律 1 / §0.2 反模式①）。
  - **生命周期干净**：run 终态时 ``gate_handler.stop()`` + ``bus.close()``，无 leaked task。

依赖单向：本模块依赖 ``orca.{run, gates, events, schema, compile}``，不被任何模块 import
（SPEC §0.1 铁律 5）。不含编排/gate 决策逻辑——``Orchestrator.run()`` 才是编排，本模块只托管。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from orca.compile import ConfigurationError, load_workflow
from orca.events.bus import EventBus
from orca.events.replay import replay_state
from orca.events.tape import Tape
from orca.gates.context_registry import SessionContextRegistry
from orca.gates.handler import HumanGateHandler
from orca.run.lifecycle import gen_run_id
from orca.run.orchestrator import Orchestrator
from orca.schema import Event, RunState, Workflow

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

RunStatus = Literal["queued", "running", "completed", "failed"]


@dataclass
class RunHandle:
    """单个 run 的隔离句柄（SPEC §2.2）。

    每个 run 拥有自己的 bus + tape + gate_handler —— 多 run 事件/gate 永不串。
    ``status`` 在 start/complete/fail 时更新（``list_runs`` 反映最新）。
    """

    run_id: str
    wf: Workflow
    bus: EventBus
    tape: Tape
    gate_handler: HumanGateHandler
    status: RunStatus = "queued"
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    # run task（``_run_with_sem`` 创建）；``wait_done`` await 它。
    _task: asyncio.Task | None = field(default=None, repr=False)
    # gate_handler 是否已 start（收尾时只 stop 已 start 的，幂等）。
    _gate_started: bool = field(default=False, repr=False)


@dataclass
class RunMeta:
    """懒加载列表项：**只有元数据，不含事件**（SPEC §2.2 / §0.1 铁律 2）。

    ``progress`` 形如 ``"3/7"``（done/total，从 ``replay_state`` 派生）。
    """

    run_id: str
    workflow_name: str
    status: RunStatus
    progress: str
    cost: float
    elapsed: float
    error: str | None


class RunManager:
    """托管多个并发 run（SPEC §2.1）。

    用法::

        manager = RunManager(max_concurrent=3)
        run_id = await manager.start_run("wf.yaml", {}, None, None)
        metas = manager.list_runs()           # 元数据，无事件
        events = manager.get_run_events(run_id)  # 懒加载全量（tape.replay）
        handle = manager.get_handle(run_id)
    """

    def __init__(
        self,
        max_concurrent: int = 3,
        *,
        runs_dir: Path | str = "runs",
        registry: SessionContextRegistry | None = None,
    ):
        self._max_concurrent = max_concurrent
        self._runs: dict[str, RunHandle] = {}
        self._sem = asyncio.Semaphore(max_concurrent)
        self._lock = asyncio.Lock()
        self._runs_dir = Path(runs_dir)
        # 共享 registry：claude session_id → (run_id, node)。多 run 的 gate 路由从这里
        # 反查 run_id（routes/gate.py 的多 run 分发依赖它）。
        self._registry = registry or SessionContextRegistry()

    # ── 公开 API ───────────────────────────────────────────────────────────

    @property
    def registry(self) -> SessionContextRegistry:
        """共享 SessionContextRegistry（routes/gate.py 多 run 分发用）。"""
        return self._registry

    async def start_run(
        self,
        yaml_path: str | Path,
        inputs: dict | None = None,
        task: str | None = None,
        max_iter: int | None = None,
    ) -> str:
        """启动一个 run（后台 task，不阻塞）。返回 run_id。

        - 加载 + 校验 workflow（``ConfigurationError`` 透传给调用方 → routes 层 400）。
        - 构造独立 tape + bus + gate_handler + RunHandle。
        - 创建后台 task ``_run_with_sem``（sem 内并发 + 状态机）。

        不阻塞：``await`` 返回时 run 已注册（status=queued），实际执行在后台。
        """
        wf = load_workflow(Path(yaml_path))
        run_id = gen_run_id(wf.name)
        tape_path = self._runs_dir / f"{run_id}.jsonl"
        tape = Tape(tape_path, run_id=run_id)
        bus = EventBus(tape)
        gate_handler = HumanGateHandler(bus)
        handle = RunHandle(
            run_id=run_id,
            wf=wf,
            bus=bus,
            tape=tape,
            gate_handler=gate_handler,
            status="queued",
        )
        async with self._lock:
            self._runs[run_id] = handle
        handle._task = asyncio.create_task(
            self._run_with_sem(handle, inputs or {}, task, max_iter),
            name=f"orca-web-run-{run_id}",
        )
        return run_id

    def list_runs(self) -> list[RunMeta]:
        """返回所有 run 的元数据（**不含事件**，懒加载红线 SPEC §0.1 铁律 2）。

        元数据从 ``replay_state(handle.tape)`` 派生（progress/cost），保证与唯一真相源
        一致（§9 决策 6）。status 取 ``handle.status``（实时）。
        """
        metas: list[RunMeta] = []
        for handle in self._runs.values():
            metas.append(self._meta_from_handle(handle))
        return metas

    def get_run_events(self, run_id: str) -> list[Event]:
        """懒加载：返回某 run 的全量事件（``tape.replay()``，SPEC §0.1 铁律 1）。

        唯一真相源 = tape；本方法不维护并行内存 list。未知 run_id → KeyError。
        """
        handle = self._require_handle(run_id)
        return list(handle.tape.replay())

    def get_run_state(self, run_id: str) -> RunState:
        """返回某 run 的 RunState 快照（``replay_state(tape)``，SPEC §3.1）。"""
        handle = self._require_handle(run_id)
        return replay_state(handle.tape)

    def get_run_meta(self, run_id: str) -> RunMeta | None:
        """返回单个 run 的 RunMeta（从 tape 派生，懒加载）。

        比 ``list_runs`` 后过滤高效（单 run 算 replay_state，不 replay 全部 run，
        SPEC §3.1 单 run 端点是前端高频轮询路径）。未知 run_id → None。
        """
        handle = self._runs.get(run_id)
        if handle is None:
            return None
        return self._meta_from_handle(handle)

    def get_handle(self, run_id: str) -> RunHandle | None:
        """取 run 的 RunHandle（WS 订阅 / gate 分发用）。未知返回 None。"""
        return self._runs.get(run_id)

    async def wait_done(self, run_id: str, timeout: float = 30.0) -> None:
        """等某 run 到终态（completed/failed）。测试 + WS 收尾用。

        超时 raise ``asyncio.TimeoutError``（fail loud，不静默 hang）。
        """
        handle = self._require_handle(run_id)
        if handle._task is None:
            return
        await asyncio.wait_for(asyncio.shield(handle._task), timeout=timeout)

    async def shutdown(self, timeout: float = 5.0) -> None:
        """收尾：等所有在跑 run 到终态（限时）+ stop 各自 gate_handler。

        ``run_server`` lifespan 退出时调。保证无 leaked task / 未关 tape。

        - 每个未 done task ``wait_for(timeout)``：run 卡在 gate（SPEC §2.2 gate 无 timeout
          无限等）时超时 → cancel task 兜底，避免 server 退出 hang。
        - 之后逐 handle teardown（stop gate_handler + close bus）。
        """
        pending = [
            h._task for h in self._runs.values()
            if h._task is not None and not h._task.done()
        ]
        for task in pending:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "shutdown: run task %s %ss 未完成（可能卡在 gate），强制 cancel",
                    task.get_name(), timeout,
                )
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001 — task 自身异常（如编排失败），忽略（已记 error）
                pass
        for handle in list(self._runs.values()):
            await self._teardown_handle(handle)

    # ── 内部 ───────────────────────────────────────────────────────────────

    def _require_handle(self, run_id: str) -> RunHandle:
        handle = self._runs.get(run_id)
        if handle is None:
            raise KeyError(f"unknown run_id: {run_id}")
        return handle

    def _meta_from_handle(self, handle: RunHandle) -> RunMeta:
        """从 handle + tape 派生 RunMeta（progress/cost 从 replay_state，§9 决策 6）。

        ``replay_state`` 失败（tape 损坏等罕见）→ progress 退化为 "?/?"，status 仍取
        handle.status（fail loud 记 warning，不崩 list_runs）。
        """
        try:
            state = replay_state(handle.tape)
            total = len(handle.wf.nodes)
            done = sum(1 for s in state.node_status.values() if s == "done")
            progress = f"{done}/{total}"
            cost = _extract_cost(state)
            workflow_name = state.workflow_name or handle.wf.name
        except Exception:  # noqa: BLE001 — tape 读失败不应崩 list_runs
            logger.warning("run %s replay 失败，元数据退化", handle.run_id, exc_info=True)
            progress = f"?/{len(handle.wf.nodes)}"
            cost = 0.0
            workflow_name = handle.wf.name
        elapsed = time.time() - handle.started_at
        return RunMeta(
            run_id=handle.run_id,
            workflow_name=workflow_name,
            status=handle.status,
            progress=progress,
            cost=cost,
            elapsed=elapsed,
            error=handle.error,
        )

    async def _run_with_sem(
        self,
        handle: RunHandle,
        inputs: dict,
        task: str | None,
        max_iter: int | None,
    ) -> None:
        """sem 内跑 orchestrator（真并发 + max_concurrent 排队）。

        生命周期：acquire sem → start gate_handler → status=running →
        ``Orchestrator.run()`` → 终态（completed/failed）→ teardown。
        """
        async with self._sem:
            handle.status = "running"
            await handle.gate_handler.start()
            handle._gate_started = True
            orch = Orchestrator(
                handle.wf,
                handle.bus,
                inputs,
                task=task,
                max_iter=max_iter,
                run_id=handle.run_id,
            )
            try:
                await orch.run()
                handle.status = "completed"
            except Exception as e:  # noqa: BLE001 — 编排任何异常 → failed（fail loud 记 error）
                handle.status = "failed"
                handle.error = f"{type(e).__name__}: {e}"
                logger.exception("run %s 失败", handle.run_id)
            finally:
                await self._teardown_handle(handle)

    async def _teardown_handle(self, handle: RunHandle) -> None:
        """清理一个 handle 的资源（幂等）：stop gate_handler + close bus。"""
        if handle._gate_started:
            try:
                await handle.gate_handler.stop()
            except Exception:  # noqa: BLE001 — teardown 不应崩
                logger.warning("run %s gate_handler.stop 异常", handle.run_id, exc_info=True)
            handle._gate_started = False
        # bus.close 关 tape 句柄（幂等：Tape.close 内部 _closed guard）。
        try:
            handle.bus.close()
        except Exception:  # noqa: BLE001
            logger.warning("run %s bus.close 异常", handle.run_id, exc_info=True)


def _extract_cost(state: RunState) -> float:
    """从 RunState.usage 提取 cost（若有）。无 usage → 0.0。"""
    usage = state.usage
    if usage is None:
        return 0.0
    # UsageSummary 形态见 schema/state.py；cost 字段可能不存在（纯 script run 无 token）。
    return float(getattr(usage, "cost", 0.0) or 0.0)
