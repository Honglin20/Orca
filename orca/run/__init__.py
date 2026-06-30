"""orca.run —— 编排层（最上层消费者）。

回答「把校验过的 Workflow 跑起来」：``run_workflow(wf, ...) -> RunState``。
单指针推进 + parallel 组并行 + foreach 分批 + 路由 first-match-wins + 循环终止。
事件全程 ``bus.emit`` 落 Tape（run/ 是唯一写 Tape 处，铁律 1）。

依赖铁律：schema ← events ← exec ← **run**；run 是最上层，不被任何模块 import。

模块组成（SPEC §4.1）：
  - router.py            : Router.resolve（first-match-wins，纯函数）
  - context.py           : RunContext re-export（复用 phase 4，DRY）
  - lifecycle.py         : run_id 生成 + 生命周期事件 (type, data)
  - executor_adapter.py  : executor.exec() → bus.emit 桥接
  - orchestrator.py      : 单指针主循环
  - parallel.py          : 并行组执行（gather + failure_mode）
  - foreach.py           : 动态并行（Semaphore + 聚合）
  - aggregate.py         : failure_mode 三态决策（parallel / foreach 共享）
  - errors.py            : MaxIterationsError
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.run.errors import MaxIterationsError
from orca.run.executor_adapter import execute_and_emit
from orca.run.foreach import run_foreach
from orca.run.lifecycle import gen_run_id, resolve_max_iter
from orca.run.orchestrator import Orchestrator
from orca.run.parallel import run_parallel_group
from orca.run.router import RouteError, resolve

if TYPE_CHECKING:
    from orca.schema import RunState, Workflow

__all__ = [
    "Orchestrator",
    "run_workflow",
    "resolve",
    "RouteError",
    "MaxIterationsError",
    "execute_and_emit",
    "run_parallel_group",
    "run_foreach",
    "gen_run_id",
    "resolve_max_iter",
]


async def run_workflow(
    wf: Workflow,
    inputs: dict[str, Any] | None = None,
    *,
    task: str | None = None,
    max_iter: int | None = None,
    tape_path: Path | None = None,
    run_id: str | None = None,
) -> RunState:
    """编排入口（SPEC §5 / 计划 R5.1）。

    Args:
        wf: 校验过的 Workflow（``load_workflow`` 产物）。
        inputs: workflow 输入覆盖（``-i key=value`` 解析后传入）。
        task: 位置参数 task（注入 ``inputs.task``）。
        max_iter: ``--max-iter`` 覆盖（最高优先级）。
        tape_path: Tape 文件路径（测试传 tmp_path；默认 ``./runs/<run_id>.jsonl``）。
        run_id: 固定 run_id（测试用；默认 ``gen_run_id(wf.name)``）。

    Returns:
        RunState（tape 派生的最终状态）。

    生命周期：内部构造 Tape + EventBus，跑完 close（调用方不需管）。
    """
    from orca.run.lifecycle import gen_run_id as _gen

    actual_run_id = run_id or _gen(wf.name)
    path = tape_path if tape_path is not None else Path("runs") / f"{actual_run_id}.jsonl"
    tape = Tape(path, run_id=actual_run_id)
    bus = EventBus(tape)
    orch = Orchestrator(
        wf, bus, inputs, task=task, max_iter=max_iter, run_id=actual_run_id,
    )
    return await orch.run()
