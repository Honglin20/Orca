"""orchestrator.py —— 单指针主循环（run/ 编排核心，SPEC §4.2）。

回答「workflow 怎么跑起来？」：``current`` 指针从 entry 推进，每步执行一个 node /
parallel 组 / foreach，累加 ``ctx.outputs``，路由 first-match-wins 决定下一步，遇
``$end`` 终止。事件全程 ``bus.emit`` 落 Tape（唯一写 Tape 处，铁律 1）。

主循环（SPEC §1.1 / §4.2）::

    current = wf.entry
    iterations = 0
    while current != "$end":
        iterations += 1
        if iterations > max_iter: raise MaxIterationsError
        output = await _dispatch(current)        # node / parallel / foreach 分派
        ctx.outputs[current] = {"output": output}  # 累加（render 约定形状）
        emit route_taken(from=current, to=next)   # 让 reducer 跟踪 current_node
        current = router.resolve(routes_of(current), output, ctx)
    emit workflow_completed(evaluate_outputs(wf.outputs, ctx))

fail loud（铁律 4）：三类错误均 emit ``workflow_failed``：
  - ``ExecError``（executor 失败）→ error_type 由 phase 映射（ExecTimeout / ...）
  - ``RouteError``（路由死锁）→ error_type=``NoRouteMatch``
  - ``MaxIterationsError``（超迭代）→ error_type=``MaxIterations``
  - ``GroupFailure``（parallel / foreach 内部失败）→ error_type=``GroupFailure``

依赖单向：本模块依赖 ``orca.{schema, events, exec}`` + ``orca.run.*`` 子模块；
是最上层消费者，不被任何模块 import。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from orca.exec.error import ExecError
from orca.run.aggregate import GroupFailure
from orca.run.errors import MaxIterationsError
from orca.run.executor_adapter import execute_and_emit
from orca.run.foreach import run_foreach
from orca.run.lifecycle import (
    gen_run_id,
    make_workflow_completed,
    make_workflow_failed,
    make_workflow_started,
    now_monotonic,
    resolve_max_iter,
)
from orca.run.parallel import run_parallel_group
from orca.run.router import RouteError, resolve

if TYPE_CHECKING:
    from pathlib import Path

    from orca.events.bus import EventBus
    from orca.exec.context import RunContext
    from orca.schema import Node, RunState, Workflow

logger = logging.getLogger(__name__)


class Orchestrator:
    """单指针推进的编排器（SPEC §4.2）。

    不持有可变运行态在 ``self`` —— 每次跑都从 fresh ``ctx`` 开始（可重入 / 可测）。
    """

    def __init__(
        self,
        wf: Workflow,
        bus: EventBus,
        inputs: dict | None = None,
        *,
        task: str | None = None,
        max_iter: int | None = None,
        run_id: str | None = None,
    ):
        self.wf = wf
        self.bus = bus
        # task 注入 inputs.task（SPEC §5：位置参数 task = -i task="..." 语法糖）
        merged_inputs = dict(inputs or {})
        if task is not None:
            merged_inputs.setdefault("task", task)
        # gen run_id（若调用方未传，内部 gen；测试可注入固定 id）
        self.run_id = run_id or gen_run_id(wf.name)
        # RunContext 构造（frozen，node 间构造新实例累加 outputs）
        from orca.exec.context import RunContext

        self.ctx = RunContext(
            inputs=merged_inputs,
            outputs={},
            run_id=self.run_id,
            task=task,
        )
        self.task = task
        self.max_iter = resolve_max_iter(wf, merged_inputs, cli_override=max_iter)

        # 索引：node 名 → node；parallel 组名 → ParallelGroup
        self._node_by_name: dict[str, Node] = {n.name: n for n in wf.nodes}
        self._parallel_by_name = {g.name: g for g in wf.parallel}

    async def run(self) -> RunState:
        """跑完整个 workflow，返回 replay_state(tape)（tape 派生的 RunState）。"""
        from orca.events.replay import replay_state

        start_ts = now_monotonic()
        # workflow_started（node=None, session_id=None）
        t, data = make_workflow_started(self.run_id, self.wf, self.ctx.inputs)
        await self.bus.emit(t, data)

        try:
            final_outputs = await self._drive_loop()
        except (ExecError, RouteError, MaxIterationsError, GroupFailure) as e:
            # fail loud：四类编排错误 → workflow_failed
            error_type = _classify_error(e)
            node = _error_node(e)
            t2, data2 = make_workflow_failed(
                error_type, str(e), node=node,
            )
            await self.bus.emit(t2, data2)
            self.bus.close()
            return replay_state(self.bus.tape)

        elapsed = now_monotonic() - start_ts
        t3, data3 = make_workflow_completed(self.wf, final_outputs, elapsed=elapsed)
        await self.bus.emit(t3, data3)
        self.bus.close()
        return replay_state(self.bus.tape)

    async def _drive_loop(self) -> dict[str, Any]:
        """单指针主循环。返回 evaluate_outputs 的最终输出 dict。

        抛出 RouteError / MaxIterationsError / ExecError / GroupFailure 由 ``run`` 接住。
        """
        current = self.wf.entry
        # 可变 outputs 累加器：node 间构造新 frozen RunContext（_make_ctx 从此快照派生）
        outputs_acc: dict[str, Any] = {}

        iterations = 0
        while current != "$end":
            iterations += 1
            if iterations > self.max_iter:
                raise MaxIterationsError(self.max_iter, current=current)

            # 执行步 ctx：含历史 outputs（不含本步 —— 本步尚未产出）
            step_ctx = self._make_ctx(outputs_acc)

            # 分派：parallel 组 / foreach / 普通 node
            raw_output = await self._dispatch(current, step_ctx)
            # 累加：包装成 {"output": raw}（render._namespace 约定）
            outputs_acc[current] = {"output": raw_output}

            # 路由求值（用更新后的 ctx —— 含本步 output）
            routes = self._routes_of(current)
            ctx_for_route = self._make_ctx(outputs_acc)
            try:
                nxt = resolve(routes, raw_output, ctx_for_route)
            except RouteError as e:
                # 补 node 名（resolve 不知 node；此处补全诊断）
                e.node = current
                raise
            # emit route_taken（让 reducer 的 current_node 跟踪对）
            await self.bus.emit("route_taken", {"from": current, "to": nxt})
            current = nxt

        return self._evaluate_outputs(outputs_acc)

    def _make_ctx(self, outputs_acc: dict[str, Any]) -> RunContext:
        """从累加的 outputs 构造 frozen RunContext 快照（DRY：dispatch / route / outputs 共用）。

        inputs / task / run_id 来自初始化（不可变），outputs 取当前累加快照（拷贝，避免
        frozen 实例持有可变引用）。
        """
        from orca.exec.context import RunContext

        return RunContext(
            inputs=self.ctx.inputs,
            outputs=dict(outputs_acc),
            run_id=self.run_id,
            task=self.task,
        )

    async def _dispatch(self, current: str, ctx: RunContext) -> Any:
        """按 current 类型分派：parallel 组 / foreach / 普通 node。"""
        if current in self._parallel_by_name:
            group = self._parallel_by_name[current]
            return await run_parallel_group(group, ctx, self.bus, self.wf)
        node = self._node_by_name.get(current)
        if node is None:
            # compile 层已保证 route.to 合法，到这里是 schema 漏校验 → fail loud
            raise ValueError(f"current {current!r} 既非 node 也非 parallel 组（schema 漏校验）")
        if node.kind == "foreach":
            # node.kind=="foreach" 已保证类型（schema 判别联合）；kind 判定即契约
            return await run_foreach(node, ctx, self.bus)  # type: ignore[arg-type]
        # 普通 node：make_executor + execute_and_emit
        from orca.exec.factory import make_executor

        executor = make_executor(node)
        return await execute_and_emit(executor, node, ctx, self.bus)

    def _routes_of(self, name: str) -> list:
        """取 current 的 routes（node 或 parallel 组）。"""
        if name in self._parallel_by_name:
            return self._parallel_by_name[name].routes
        return self._node_by_name[name].routes

    def _evaluate_outputs(self, outputs_acc: dict[str, Any]) -> dict[str, Any]:
        """渲染 ``wf.outputs`` 模板 → 最终输出 dict（SPEC §4.2 末）。

        用 Jinja2 渲染每个 value；ctx 含全部已完成 node 的 outputs。
        """
        from orca.exec.render import render_template

        if not self.wf.outputs:
            return {}
        ctx = self._make_ctx(outputs_acc)
        result: dict[str, Any] = {}
        for key, template in self.wf.outputs.items():
            result[key] = render_template(template, ctx)
        return result


def _classify_error(e: Exception) -> str:
    """编排错误 → workflow_failed 的 error_type（SPEC §3.4 / 铁律 4）。"""
    if isinstance(e, MaxIterationsError):
        return "MaxIterations"
    if isinstance(e, RouteError):
        return "NoRouteMatch"
    if isinstance(e, GroupFailure):
        return "GroupFailure"
    if isinstance(e, ExecError):
        return e.error_type  # 透传 executor 的 error_type（ExecTimeout / RenderError / ...）
    return e.__class__.__name__


def _error_node(e: Exception) -> str | None:
    """从异常中取导致失败的 node 名（workflow_failed.data.node，SPEC §3.4）。

    - ``RouteError``：路由死锁卡在的 node（resolve 处补全）
    - ``ExecError``：executor 失败的 node（adapter 注入 err.node）
    - ``GroupFailure``：parallel / foreach 组名
    - ``MaxIterationsError``：超迭代卡在的 node（current）
    """
    if isinstance(e, RouteError):
        return e.node
    if isinstance(e, ExecError):
        return e.node
    if isinstance(e, GroupFailure):
        return e.group_name
    if isinstance(e, MaxIterationsError):
        return e.current
    return None
