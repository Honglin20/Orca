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
    from orca.events.tape import Tape
    from orca.exec.context import RunContext
    from orca.gates.interrupt import InterruptHandler
    from orca.gates.types import InterruptRequest
    from orca.schema import Node, RunState, Workflow

logger = logging.getLogger(__name__)


class Orchestrator:
    """单指针推进的编排器（SPEC §4.2）。

    不持有可变运行态在 ``self`` —— 每次跑都从 fresh ``ctx`` 开始（可重入 / 可测）。
    """

    # drive_loop / _dispatch / _make_ctx / _next_node_after 依赖的实例字段清单。
    # ``__init__`` 与 resume 专用 ``_bare_instance``（bypass ``__init__``）都必须设置全部
    # 这些字段。``_drive_from`` 在入口调 ``_assert_drive_fields_complete`` 校验，任一缺失
    # 立即 fail loud（防字段漂移：未来给 ``__init__`` 加字段却忘了同步 ``_bare_instance``
    # 时，此处报清晰错误，而非延迟到 AttributeError）。review §鲁棒性 🔴 建议。
    _DRIVE_REQUIRED_FIELDS = (
        "wf", "bus", "run_id", "ctx", "task", "max_iter",
        "_node_by_name", "_parallel_by_name", "_guidance_acc",
        "_interrupt_handler", "_interrupt_pending", "_interrupt_answer",
    )

    def _assert_drive_fields_complete(self) -> None:
        """校验实例含 drive_loop 所需全部字段（resume bypass __init__ 的安全网）。

        正常 ``__init__`` 路径天然满足；``_bare_instance``（resume）bypass ``__init__``，
        手动设字段，本方法确保不漏。缺失 → RuntimeError fail loud（清晰归因，非AttributeError）。
        """
        missing = [
            name for name in self._DRIVE_REQUIRED_FIELDS
            if not hasattr(self, name)
        ]
        if missing:
            raise RuntimeError(
                f"Orchestrator 缺 drive 所需字段：{missing}。"
                "正常 __init__ 路径不应触发；若用 from_tape/_bare_instance，"
                "请检查是否漏设字段（字段漂移）。"
            )

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

    # ── phase 11 §7：Checkpoint Resume（SPEC §7.2）─────────────────────────────

    @classmethod
    def from_tape(
        cls,
        tape_path: Path,
        bus: EventBus,
        wf: Workflow,
    ) -> Orchestrator:
        """从 Tape 重放构造 Orchestrator，恢复到崩溃前状态（SPEC §7.2）。

        - 读 Tape（``resume=True`` 截断末尾残行，SPEC §7.3 fail-soft）。
        - ``replay_state`` → 拿已完成 node 列表 + outputs aggregate。
        - typed exception 对每个失败模式（CLI 层映射 exit code）：
            * ``EmptyTapeError``：Tape 无事件。
            * ``AlreadyCompletedError``：已是 workflow_completed 终态。
            * ``ParallelGroupMidCrashError``：崩溃在 parallel 组中间。
            * ``MidFileCorruptError``：Tape 中段损坏（replay 不可信）。
        - resume entry = 最后一个 node_completed 的下一 node（用 routes 求值，
          与 drive_loop 同一 ``_next_node_after``，避免自造路由逻辑）。
        - RunContext 携带已完成 outputs（从 state 派生，不手搓）。

        ``bus`` 由调用方传入（其 Tape 已用 ``resume=True`` 构造，残行已截断）。本方法
        只读 Tape（``replay_state``）+ 校验 + 构造 Orchestrator 实例，不写 Tape。
        """
        from orca.events.replay import replay_state
        from orca.run.resume import (
            AlreadyCompletedError,
            EmptyTapeError,
            MidFileCorruptError,
            _detect_parallel_mid_crash,
            _find_first_corrupt_line,
            _outputs_acc_from_state,
        )

        # 1) 中段损坏检测（严格，区别于 replay 的 fail-soft）+ 顺带计 valid 事件数。
        #    复用本次扫描的 valid_event_count 作为 replayed_events（避免再多读一遍 tape）。
        corrupt, event_count = _find_first_corrupt_line(tape_path)
        if corrupt is not None:
            lineno, preview = corrupt
            raise MidFileCorruptError(tape_path, lineno, preview)

        # 2) replay（bus.tape 已 resume=True 截断残行；此处用同一路径再读，状态一致）
        tape = bus.tape
        state = replay_state(tape)

        # 3) 失败模式判定（按 SPEC §7.3 顺序）。
        if event_count == 0:
            raise EmptyTapeError(tape_path)
        if state.status == "completed":
            raise AlreadyCompletedError(state.run_id)
        parallel_err = _detect_parallel_mid_crash(state, wf)
        if parallel_err is not None:
            raise parallel_err

        # 4) 定位 resume 起点：current_node（reducer 据 route_taken 维护）即崩溃点的
        #    下一 node。若 current_node 为 None（如 tape 末尾正好是 node_completed 但
        #    route_taken 还没 emit），回退到「最后一个 done node 的下一 node」。
        resume_node = state.current_node
        outputs_acc = _outputs_acc_from_state(state)
        if resume_node is None or resume_node == "$end":
            # 无明确 current_node：找最后一个 done node，对其 routes 求值下一 node。
            done_nodes = [
                n for n, s in state.node_status.items() if s == "done"
            ]
            if not done_nodes:
                # 一个 node 都没完成（如首个 node 崩溃）→ 从 wf.entry 重跑。
                resume_node = wf.entry
            else:
                # 最后完成的 node 名（按 tape 顺序，reducer 的 node_status 不保序；
                # 用 state.context 的 key 集合 + wf.nodes 顺序推断最后完成者）。
                last_done = cls._find_last_done_node_name(tape, done_nodes)
                resume_node = cls._next_node_for_resume(wf, last_done, outputs_acc)

        # 5) 构造 Orchestrator（bypass __init__：避免重新 gen run_id / 重置 ctx）。
        orch = cls._bare_instance(wf, bus, state, resume_node, outputs_acc)
        # 记录 replayed 事件数，供 run_from_state emit workflow_resumed 用。
        orch._resume_replayed_events = event_count
        orch._resume_initial_outputs = outputs_acc
        orch._resume_start_node = resume_node
        return orch

    @staticmethod
    def _next_node_for_resume(
        wf: Workflow, last_done: str | None, outputs_acc: dict[str, Any]
    ) -> str:
        """对 ``last_done`` 的 routes 求值下一 node（resume 起点）。

        ``last_done=None``（无任何 node 完成）→ ``wf.entry``。
        """
        if last_done is None:
            return wf.entry
        from orca.run.router import resolve

        node_by_name = {n.name: n for n in wf.nodes}
        parallel_by_name = {g.name: g for g in wf.parallel}
        if last_done in parallel_by_name:
            routes = parallel_by_name[last_done].routes
        else:
            routes = node_by_name[last_done].routes
        # ctx 仅用于 route 求值（outputs 已含 last_done 的 output）。
        from orca.exec.context import RunContext

        ctx = RunContext(
            inputs={}, outputs=dict(outputs_acc), run_id="", task=None,
        )
        return resolve(routes, outputs_acc.get(last_done, {}).get("output"), ctx)

    @staticmethod
    def _bare_instance(
        wf: Workflow,
        bus: EventBus,
        state: RunState,
        resume_node: str,
        outputs_acc: dict[str, Any],
    ) -> Orchestrator:
        """构造 bypass ``__init__`` 的 Orchestrator（resume 专用，复用 drive 逻辑）。

        不重新 gen run_id（沿用 state.run_id，保 tape 连续性）；不重跑 inputs default
        填充（已完成 node 不再需要 inputs 校验）；其余索引 / 控制字段同 ``__init__``。
        """
        from orca.exec.context import RunContext

        orch = Orchestrator.__new__(Orchestrator)
        orch.wf = wf
        orch.bus = bus
        orch.run_id = state.run_id
        # RunContext：inputs 从 workflow_started 事件取（保 render 能拿 inputs.*）。
        # 若取不到（罕见，如 workflow_started 残行被截断）回退空 dict。
        inputs = Orchestrator._inputs_from_tape(bus.tape)
        orch.ctx = RunContext(
            inputs=inputs, outputs={}, run_id=state.run_id, task=None,
        )
        orch.task = None
        orch.max_iter = resolve_max_iter(wf, inputs)
        orch._node_by_name = {n.name: n for n in wf.nodes}
        orch._parallel_by_name = {g.name: g for g in wf.parallel}
        orch._guidance_acc: list[str] = []
        # 中断字段：resume 不接 interrupt handler（交互态不跨进程恢复）。
        orch._interrupt_handler = None
        orch._interrupt_pending = None
        orch._interrupt_answer = None
        # resume 专用状态（run_from_state 消费）。
        orch._resume_replayed_events = 0
        orch._resume_initial_outputs = outputs_acc
        orch._resume_start_node = resume_node
        return orch

    @staticmethod
    def _inputs_from_tape(tape: Tape) -> dict[str, Any]:
        """从 Tape 的 ``workflow_started.data.inputs`` 取原始 inputs（render 用）。

        取不到（罕见：workflow_started 残行被截断 / 异常 tape）→ 返空 dict + warning。
        空 inputs 下 render ``{{ inputs.x }}`` 会 UndefinedError，warning 让归因可见
        （review §鲁棒性 建议：不静默返 {}）。
        """
        for event in tape.replay():
            if event.type == "workflow_started":
                inputs = event.data.get("inputs")
                if isinstance(inputs, dict):
                    return inputs
        logger.warning(
            "resume：Tape %s 未找到 workflow_started.data.inputs，回退空 inputs"
            "（后续 render {{ inputs.* }} 可能 UndefinedError）",
            getattr(tape, "path", "?"),
        )
        return {}

    @staticmethod
    def _find_last_done_node_name(tape: Tape, done_nodes: list[str]) -> str | None:
        """扫 Tape 找最后一个 ``node_completed`` 的 node 名（保序，区别于 state.node_status）。

        ``state.node_status`` 是 dict（无序），无法直接知道哪个 done node 是最后完成的。
        本方法扫 Tape 的 ``node_completed`` 事件序列，返回序列中最后一个其 node 在
        ``done_nodes`` 里的名字。无匹配返回 None。
        """
        done_set = set(done_nodes)
        last: str | None = None
        for event in tape.replay():
            if event.type == "node_completed" and event.node in done_set:
                last = event.node
        return last

    async def run_from_state(self) -> RunState:
        """从 ``from_tape`` 恢复的状态续跑（SPEC §7.2）。

        先 emit ``workflow_resumed``（写 Tape，可观测），再调 ``_drive_from`` 从 resume
        node 续跑。错误处理 / workflow_completed / bus.close 与 ``run`` 一致（DRY：
        共用 ``_classify_error`` / ``_error_node`` / lifecycle helpers）。
        """
        from orca.events.replay import replay_state

        start_ts = now_monotonic()
        # workflow_resumed：data = {from_tape, resumed_node, replayed_events}（SPEC §2.2）。
        await self.bus.emit(
            "workflow_resumed",
            {
                "from_tape": str(self.bus.tape.path),
                "resumed_node": self._resume_start_node,
                "replayed_events": self._resume_replayed_events,
            },
        )

        try:
            final_outputs = await self._drive_from(
                self._resume_start_node, self._resume_initial_outputs
            )
        except (ExecError, RouteError, MaxIterationsError, GroupFailure, WorkflowAborted) as e:
            error_type = _classify_error(e)
            node = _error_node(e)
            t2, data2 = make_workflow_failed(error_type, str(e), node=node)
            await self.bus.emit(t2, data2)
            self.bus.close()
            return replay_state(self.bus.tape)

        elapsed = now_monotonic() - start_ts
        t3, data3 = make_workflow_completed(self.wf, final_outputs, elapsed=elapsed)
        await self.bus.emit(t3, data3)
        self.bus.close()
        return replay_state(self.bus.tape)

    async def _drive_loop(self) -> dict[str, Any]:
        """单指针主循环（SPEC §4.2）。返回 evaluate_outputs 的最终输出 dict。

        抛出 RouteError / MaxIterationsError / ExecError / GroupFailure / WorkflowAborted
        由 ``run`` 接住。

        phase 11 §7：循环体抽到 ``_drive_from``，``run_from_state``（resume）复用同一段
        node 边界 + dispatch + 路由逻辑，仅起始 node / 初始 outputs_acc 不同（DRY）。
        """
        return await self._drive_from(self.wf.entry, {})

    async def _drive_from(
        self, start_node: str, initial_outputs: dict[str, Any]
    ) -> dict[str, Any]:
        """单指针主循环的可参数化核心（``run`` 与 ``run_from_state`` 共享，DRY）。

        - ``start_node``：起始 node（首次 run = ``wf.entry``；resume = 崩溃点的下一 node）。
        - ``initial_outputs``：预填充的 outputs 累加器（resume 时含已完成 node 的 outputs；
          首次 run 为空 dict）。
        """
        # 安全网：校验实例含 drive 所需全部字段（resume bypass __init__ 时防字段漂移）。
        self._assert_drive_fields_complete()
        current = start_node
        # 可变 outputs 累加器：node 间构造新 frozen RunContext（_make_ctx 从此快照派生）
        outputs_acc: dict[str, Any] = dict(initial_outputs)

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
            ``request_interrupt`` 带入。调 ``handler.record_resolved`` **同步** emit requested +
            resolved 写 Tape（不交给 async broadcaster——避免与 ``run()`` 的 ``bus.close()``
            竞态丢事件，wave-1 e2e 修复）。**不经 await-future**——modal dismiss 在 node 边界
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
            # CLI 单壳路径：用户已答，record_resolved 同步 emit requested + resolved 写 Tape。
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
