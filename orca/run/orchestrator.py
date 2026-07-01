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
from orca.run.errors import MaxIterationsError, WorkflowAborted
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
    from orca.gates.interrupt import InterruptHandler
    from orca.gates.types import InterruptRequest
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
        interrupt_handler: InterruptHandler | None = None,
    ):
        self.wf = wf
        self.bus = bus
        # phase 11 §3：可选 InterruptHandler 注入。None = 无中断支持（非交互 run /
        # 既有测试不受影响，向后兼容）。注入后 CLI 层经 ``request_interrupt`` 登记 pending。
        self._interrupt_handler = interrupt_handler
        self._interrupt_pending: InterruptRequest | None = None
        # phase 11 §3.1：CLI 单壳路径下用户已答完的 (action, guidance)，随 request_interrupt 带入。
        # node 边界 _handle_interrupt 直接消费它（不经 handler.request 的 await-future）。
        self._interrupt_answer: tuple[str, str | None] | None = None
        # task 注入 inputs.task（SPEC §5：位置参数 task = -i task="..." 语法糖）
        merged_inputs = dict(inputs or {})
        if task is not None:
            merged_inputs.setdefault("task", task)
        # 填充 wf.inputs 声明的 default：yaml 里 ``inputs.<name>.default`` 未被 CLI/-i
        # 覆盖时，必须生效进 ctx.inputs（SPEC phase-1 §3.2 InputDef 契约）。
        # 历史 gap：仅 ``iterations`` 的 default 在 resolve_max_iter 里被消费（特例），
        # 其它声明 default 的 input（如 ``target_project``）在 render 时 UndefinedError。
        # 必填 input 缺失 + 无 default → fail loud（启动前置条件不满足，类比 argparse）。
        for name, idef in wf.inputs.items():
            if name in merged_inputs:
                continue
            if idef.default is not None:
                merged_inputs[name] = idef.default
            elif idef.required:
                raise ValueError(
                    f"必填 input {name!r}（type={idef.type}）未提供且无 default"
                )
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
        # resolve_max_iter 在 __init__ 调用（run() 之前）：非法 iterations（如 "abc"）
        # 会 raise ValueError → 异常直透调用方，**不发 workflow_failed**（语义：配置错误
        # 属启动前置条件不满足，workflow 未真正开始；类比 argparse type=int 解析失败 exit）。
        # 运行期四类错误（ExecError/RouteError/MaxIterations/GroupFailure）才进 run() 的
        # except 链 → workflow_failed（SPEC §3.4 / 铁律 4）。
        self.max_iter = resolve_max_iter(wf, merged_inputs, cli_override=max_iter)

        # 索引：node 名 → node；parallel 组名 → ParallelGroup
        self._node_by_name: dict[str, Node] = {n.name: n for n in wf.nodes}
        self._parallel_by_name = {g.name: g for g in wf.parallel}

        # phase 11 §4：累积的用户 guidance（continue + guidance 时追加）。
        # 每次 _make_ctx 把它注入 RunContext.user_guidance（Step B 接 render_prompt）。
        # 单 run 生命周期内单调累加；用 list 因 frozen tuple 在 _make_ctx 里构造。
        self._guidance_acc: list[str] = []

    # ── phase 11：中断公开通道（SPEC §2.3）──────────────────────────────────

    def request_interrupt(
        self,
        ireq: InterruptRequest,
        answer: tuple[str, str | None] | None = None,
    ) -> None:
        """CLI 层（InterruptModal dismiss 后）调此方法登记一次中断请求。

        幂等：重复登记同一 run 仅保留最新一条（node 边界只消费一次）。
        无 interrupt_handler 注入时调用 = 配置错误（fail loud，记 warning）。

        SPEC §2.3 测试 A 修正：CLI 层经此**公开方法**设置 pending，**不**经任何
        ``_orchestrator_proxy``、**不**直接 mutate 私有属性。

        ``answer``（CLI 单壳路径，phase 11 §3.1）：用户在 InterruptModal 里已答完，
        ``(action, guidance)`` 随请求一起带上。node 边界 ``_handle_interrupt`` 直接消费它，
        **不**走 ``handler.request`` 的 await-future 机制（那是多壳竞速用，CLI 单壳不需要；
        且 await-future 要求 resolve 在 request 之后，但 CLI 的 resolve（modal dismiss）
        发生在 node 边界**之前**——时序不匹配，强行 await 会死锁）。

        ``answer=None``（多壳路径，phase 11 web/mcp，本 step 不启用）：``_handle_interrupt``
        退化为 ``await handler.request(ireq)`` 等任一壳 resolve。
        """
        if self._interrupt_handler is None:
            logger.warning(
                "request_interrupt 被调但 Orchestrator 未注入 InterruptHandler"
                "（非交互 run？中断请求被忽略，ireq.id=%s）",
                ireq.id,
            )
            return
        self._interrupt_pending = ireq
        self._interrupt_answer = answer

    async def run(self) -> RunState:
        """跑完整个 workflow，返回 replay_state(tape)（tape 派生的 RunState）。"""
        from orca.events.replay import replay_state

        start_ts = now_monotonic()
        # workflow_started（node=None, session_id=None）
        t, data = make_workflow_started(self.run_id, self.wf, self.ctx.inputs)
        await self.bus.emit(t, data)

        try:
            final_outputs = await self._drive_loop()
        except (ExecError, RouteError, MaxIterationsError, GroupFailure, WorkflowAborted) as e:
            # fail loud：五类编排错误 → workflow_failed
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

        抛出 RouteError / MaxIterationsError / ExecError / GroupFailure / WorkflowAborted
        由 ``run`` 接住。
        """
        current = self.wf.entry
        # 可变 outputs 累加器：node 间构造新 frozen RunContext（_make_ctx 从此快照派生）
        outputs_acc: dict[str, Any] = {}

        iterations = 0
        while current != "$end":
            iterations += 1
            if iterations > self.max_iter:
                raise MaxIterationsError(self.max_iter, current=current)

            # ── phase 11 §2.3：node 边界检查 interrupt pending ──────────────────
            # pending 由 CLI 层 ``request_interrupt`` 登记；node 边界消费（-p 路线限制：
            # 无法中断工具调用中，但 Conductor SDK 也只在 message 间——同粒度，实测够用）。
            if self._interrupt_pending is not None and self._interrupt_handler is not None:
                action = await self._handle_interrupt(current, outputs_acc)
                if action == "abort":
                    raise WorkflowAborted(current)
                if action == "skip":
                    # 当前 node 标 skipped，output 记 None（下游引用走兜底 route），
                    # 推进到下一 node（不执行当前）。继续 while 循环。
                    await self.bus.emit(
                        "node_skipped", {"reason": "user_interrupt_skip"}, node=current,
                    )
                    outputs_acc[current] = {"output": None, "skipped": True}
                    current = await self._next_node_after(current, outputs_acc, None)
                    continue
                # continue: guidance 已在 _handle_interrupt 累积进 _guidance_acc，
                # 下面 _make_ctx 自动带上（Step B 起 render_prompt 拼 [User Guidance]）。
            # ───────────────────────────────────────────────────────────────

            # 执行步 ctx：含历史 outputs（不含本步 —— 本步尚未产出）
            step_ctx = self._make_ctx(outputs_acc)

            # 分派：parallel 组 / foreach / 普通 node
            raw_output = await self._dispatch(current, step_ctx)
            # 累加：包装成 {"output": raw}（render._namespace 约定）
            outputs_acc[current] = {"output": raw_output}

            # 路由求值（用更新后的 ctx —— 含本步 output）
            current = await self._next_node_after(current, outputs_acc, raw_output)

        return self._evaluate_outputs(outputs_acc)

    async def _handle_interrupt(
        self, current: str, outputs_acc: dict[str, Any]
    ) -> str:
        """消费 ``_interrupt_pending`` → 拿用户答 ``(action, guidance)`` → 累积 guidance。

        SPEC §2.3 / §3.1 / §3.3 / §4.1。返回 action（``"continue"``/``"skip"``/``"abort"``）。
        continue 分支把 guidance 累积进 ``self._guidance_acc``（_make_ctx 注入 ctx）。

        两条取答路径（SPEC §3.1 时序）：
          - **CLI 单壳**（``_interrupt_answer`` 非 None）：用户在 InterruptModal 答完随
            ``request_interrupt`` 带入。调 ``handler.record_resolved`` emit requested + 入队
            resolved（broadcaster 写 Tape）。**不经 await-future**——modal dismiss 在 node 边界
            之前，await-future 会死锁（review §2.1 critical bug 的修复）。
          - **多壳**（``_interrupt_answer`` None）：``await handler.request(ireq)`` 等任一壳 resolve
            （web/mcp，phase 11 本 step 不启用，留接口）。
        """
        assert self._interrupt_handler is not None  # _drive_loop 调用前保证
        ireq = self._interrupt_pending
        assert ireq is not None  # _drive_loop 调用前保证
        answer = self._interrupt_answer
        # 消费 pending + answer（防内存泄漏 + 防二次消费）。
        self._interrupt_pending = None
        self._interrupt_answer = None

        if answer is not None:
            # CLI 单壳路径：用户已答，record_resolved emit requested + 入队 resolved 写 Tape。
            action, guidance = answer
            await self._interrupt_handler.record_resolved(ireq, action, guidance, ireq.source)
        else:
            # 多壳路径：await handler.request（emit requested + 等任一壳 resolve）。
            action, guidance = await self._interrupt_handler.request(ireq)

        if action == "continue" and guidance:
            # 累积 guidance → _make_ctx 自动注入后续 ctx → render_prompt 拼 [User Guidance] 段
            self._guidance_acc.append(guidance)
        return action

    async def _next_node_after(
        self, current: str, outputs_acc: dict[str, Any], raw_output: Any
    ) -> str:
        """对 current 的 routes 求值下一 node，emit route_taken（DRY：drive_loop / skip 共用）。

        skip 路径传 ``raw_output=None``：当前 node 未执行，下游 routes 据此求值（兜底
        route ``when=None`` 命中）。
        """
        routes = self._routes_of(current)
        ctx_for_route = self._make_ctx(outputs_acc)
        try:
            nxt = resolve(routes, raw_output, ctx_for_route)
        except RouteError as e:
            e.node = current
            raise
        # emit route_taken（让 reducer 的 current_node 跟踪对）
        await self.bus.emit("route_taken", {"from": current, "to": nxt})
        return nxt

    def _make_ctx(self, outputs_acc: dict[str, Any]) -> RunContext:
        """从累加的 outputs 构造 frozen RunContext 快照（DRY：dispatch / route / outputs 共用）。

        inputs / task / run_id 来自初始化（不可变），outputs 取当前累加快照（拷贝，避免
        frozen 实例持有可变引用）。

        phase 11 §4：把累积的 ``_guidance_acc``（Ctrl+G + CONTINUE 时追加）注入 ctx，
        render_prompt 拼 ``[User Guidance]`` 段（SPEC §10.3 修正 C3：走既有 _make_ctx，
        不新增 with_outputs）。空 acc = 无 guidance（向后兼容）。
        """
        from orca.exec.context import RunContext

        return RunContext(
            inputs=self.ctx.inputs,
            outputs=dict(outputs_acc),
            run_id=self.run_id,
            task=self.task,
            user_guidance=tuple(self._guidance_acc),
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
    if isinstance(e, WorkflowAborted):
        return "WorkflowAborted"
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
    - ``WorkflowAborted``：用户中止时正在跑的 node
    """
    if isinstance(e, RouteError):
        return e.node
    if isinstance(e, ExecError):
        return e.node
    if isinstance(e, GroupFailure):
        return e.group_name
    if isinstance(e, MaxIterationsError):
        return e.current
    if isinstance(e, WorkflowAborted):
        return e.node
    return None
