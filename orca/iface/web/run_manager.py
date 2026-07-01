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
from orca.gates.pending import pending_gates_from_tape
from orca.gates.types import HumanGate
from orca.run.lifecycle import gen_run_id
from orca.run.orchestrator import Orchestrator
from orca.schema import Event, RunState, Workflow

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

RunStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


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

    # ── 程序化客户端查询（MCP / 其它 shell）—— tape-only query path ─────────

    def pending_gates(self, run_id: str) -> list[HumanGate]:
        """返回 run 当前未 resolved 的 gate（SPEC phase-10 §3.3 / §5.1）。

        **tape-only**：派生自 ``pending_gates_from_tape(handle.tape)``，不读
        ``gate_handler._pending`` / ``_gates_meta``（runtime await 状态，重启即丢）。
        重启进程后仍能查（tape 在磁盘），多壳读同一份不漂移。

        未知 run_id → KeyError（fail loud，SPEC §6.0 铁律 4）。
        """
        handle = self._require_handle(run_id)
        return pending_gates_from_tape(handle.tape)

    def run_summary(self, run_id: str) -> dict | None:
        """MCP / 其它程序化客户端友好的 run 摘要（SPEC phase-10 §3.3）。

        返回 dict（不含 ``_hint``——``_hint`` 是 MCP 层加的引导字段，§9.10）::

            {
                "task_id": str,
                "status": "running" | "needs_decision" | "completed" | "failed" | "cancelled",
                "current_node": str | None,
                "progress": "3/7",  # done/total
                "cost": float,
                "elapsed": float,
                "gate": dict | None,  # 仅 needs_decision 时填充
                "output": dict | None,  # 仅 completed 时填充（来自 workflow_completed.data.outputs）
                "error": str | None,  # 仅 failed 时填充
            }

        未知 run_id → None（MCP ``get_task_status`` 据此返回 ``status="unknown"``）。

        全部数据派生自 tape + handle.status（实时），无并行真相源。
        """
        handle = self._runs.get(run_id)
        if handle is None:
            return None
        meta = self._meta_from_handle(handle)
        state = replay_state(handle.tape)
        gates = pending_gates_from_tape(handle.tape)
        status = self._derive_mcp_status(meta.status, gates)
        summary: dict = {
            "task_id": run_id,
            "status": status,
            "current_node": state.current_node,
            "progress": meta.progress,
            "cost": meta.cost,
            "elapsed": meta.elapsed,
            "gate": None,
            "output": None,
            "error": None,
        }
        if status == "needs_decision" and gates:
            summary["gate"] = _gate_to_dict(gates[0])
        elif status == "completed":
            # outputs 来自 workflow_completed 事件 data.outputs（reducer 不进 context）。
            # 扫 tape 找最后一个 workflow_completed（幂等，SPEC §3 单一读路径）。
            summary["output"] = _outputs_from_tape(handle.tape)
        elif status == "failed":
            summary["error"] = meta.error
        return summary

    @staticmethod
    def _derive_mcp_status(
        run_status: RunStatus, pending_gates: list[HumanGate]
    ) -> str:
        """RunStatus + pending_gates → MCP status（SPEC phase-10 §3.3）。

        映射规则：
          - 终态优先（completed / failed / cancelled 直接返回）
          - 非终态且有 pending gate → ``needs_decision``（优先于 running）
          - 其它 → ``running``（含 queued，对 MCP 客户端而言 queued 等价 running）

        ``needs_decision`` 优先于 ``running``：哪怕 status 显示 running，只要 tape 里
        有未 resolved gate，MCP 客户端第一关心的是"该决策了"。
        """
        if run_status == "completed":
            return "completed"
        if run_status == "failed":
            return "failed"
        if run_status == "cancelled":
            return "cancelled"
        if pending_gates:
            return "needs_decision"
        return "running"

    async def cancel_run(self, run_id: str, reason: str | None = None) -> bool:
        """取消 run（SPEC phase-10 §5.3）。

        步骤（顺序重要）：
          1. ``bus.emit("workflow_cancelled", ...)`` 写 tape（**唯一真相**，重启后
             replay 仍见 cancelled，不漂移）—— 必须在 task.cancel 前，避免与 task
             finally 的 teardown（关 bus）竞态。
          2. ``handle.status = "cancelled"``（runtime 状态，list_runs 立刻反映）。
          3. cancel asyncio task（停止编排，触发 task 内 finally 收尾）。
          4. await task 完成（让 _run_with_sem 的 finally 跑完，teardown 幂等）。
          5. teardown gate_handler + bus（幂等，可能已被 task finally 调用）。

        返回值：
          - True：成功 cancel（task 已 cancel + tape 已写 cancelled）
          - False：已终态（completed / failed / cancelled），业务可恢复（§6.1 验收）
          - KeyError：未知 run_id（fail loud，§6.0 铁律 4）
        """
        handle = self._require_handle(run_id)
        if handle.status in ("completed", "failed", "cancelled"):
            return False

        # 1. emit workflow_cancelled 写 tape（唯一真相，SPEC §5.4 决策 9）。
        # 先 emit 后 cancel task：task finally 会 teardown 关 bus，emit 必须在 bus 还
        # 活着时完成，避免 RuntimeError: Tape 已 close。
        try:
            await handle.bus.emit(
                "workflow_cancelled",
                data={"reason": reason or "user_cancelled"},
            )
        except Exception:  # noqa: BLE001 — emit 失败不阻断 cancel（tape 可能已 close）
            logger.warning(
                "run %s emit workflow_cancelled 失败（tape 可能已 close）",
                run_id,
                exc_info=True,
            )

        # 2. runtime status 转 cancelled（list_runs 立刻反映）
        handle.status = "cancelled"

        # 3. cancel asyncio task（若有 in-flight orchestrator.run）
        task = handle._task
        if task is not None and not task.done():
            task.cancel()

        # 4. await task 完成（让 finally 跑完，避免 leaked task）。task 被 cancel 后会
        # 抛 CancelledError——这是预期路径，正常吞。其它异常（编排失败）也吞——cancel
        # 本就是用户主动终止，编排异常已被 status 转 cancelled 覆盖（用户语义优先）。
        if task is not None and not task.done():
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        # 5. teardown（与正常终态路径一致，幂等——task finally 可能已调过）
        await self._teardown_handle(handle)
        return True

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


def _gate_to_dict(gate: HumanGate) -> dict:
    """HumanGate → MCP 友好的 dict（run_summary 的 gate 字段）。

    返回字段（SPEC phase-10 §2.2 / §3.3）：gate_id / prompt / options / context /
    source / run_id / node / session_id。客户端据此渲染决策 UI + 调 resolve_gate。
    """
    return {
        "gate_id": gate.id,
        "prompt": gate.prompt,
        "options": gate.options,
        "context": gate.context,
        "source": gate.source,
        "run_id": gate.run_id,
        "node": gate.node,
        "session_id": gate.session_id,
    }


def _outputs_from_tape(tape: Tape) -> dict | None:
    """扫 tape 找最后一个 workflow_completed 事件的 data.outputs（run_summary 用）。

    reducer 不把 outputs 投影进 RunState.context（node_completed 累积的是每 node 的
    output，键是 node 名，非 "outputs"）；workflow 级最终 outputs 在
    ``workflow_completed.data.outputs`` 字段。返回 None 表示无 completed 事件 / 无
    outputs 字段（如纯 script run 也应有 ``{}`` 至少）。
    """
    outputs: dict | None = None
    for event in tape.replay():
        if event.type == "workflow_completed":
            data_outputs = event.data.get("outputs")
            if isinstance(data_outputs, dict):
                outputs = data_outputs
    return outputs
