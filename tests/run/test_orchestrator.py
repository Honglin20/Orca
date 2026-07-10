"""tests/run/test_orchestrator.py —— 单指针主循环（SPEC §4.2 / 计划 R4.4）。

覆盖：
  - 线性推进（entry→A→B→$end）
  - 条件分支（output 决定去 B 或 C）
  - 回环 + max_iter → workflow_failed（MaxIterations）
  - $end 终止 → workflow_completed
  - NoRouteMatch（全 when 不匹配）→ workflow_failed
  - outputs 求值（Jinja2 渲染 wf.outputs）
  - ctx 累积（B 能读 A 的 output）
  - executor 失败 → workflow_failed
  - route_taken 事件正确 emit（reducer current_node 跟踪）
  - task 注入 inputs.task

策略：用 SetExecutor / ScriptExecutor（确定性，不 spawn）；失败路径 monkeypatch
``make_executor`` 注入 FakeExecutor。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orca.run.orchestrator import Orchestrator
from orca.run.router import RouteError
from orca.run.errors import MaxIterationsError
from orca.schema import (
    AgentNode,
    InputDef,
    Route,
    ScriptNode,
    SetNode,
    Workflow,
)
from tests.run.conftest import FakeExecutor, make_bus, run_async


def _linear_wf() -> Workflow:
    """entry a → b → c → $end（全 script，零 token，确定性）。"""
    return Workflow(
        name="demo_linear",
        entry="a",
        nodes=[
            ScriptNode(name="a", command="echo step_a", routes=[Route(to="b")]),
            ScriptNode(name="b", command="echo step_b", routes=[Route(to="c")]),
            ScriptNode(name="c", command="echo step_c", routes=[Route(to="$end")]),
        ],
        outputs={"result": "{{ c.output.stdout }}"},
    )


def _orch(wf, tmp_path, **kw) -> Orchestrator:
    bus, _ = make_bus(tmp_path)
    return Orchestrator(wf, bus, **kw)


def _orch_run(orch):
    return run_async(orch.run())


# ── 线性推进 ──────────────────────────────────────────────────────────────────


def test_linear_workflow_completes(tmp_path):
    """a→b→c→$end：status=completed，outputs.result 正确，事件流完整。"""
    orch = _orch(_linear_wf(), tmp_path)
    state = _orch_run(orch)

    assert state.status == "completed"
    assert state.workflow_name == "demo_linear"
    # reducer 把 raw output 直接存 state.context[node]（非 {"output": raw} 包装）
    assert state.context["c"]["stdout"].strip() == "step_c"
    # run_state 不含 final outputs（reducer 不投影 workflow_completed.data.outputs），
    # 但每个 node 的 output 累积在 context
    assert state.node_status == {"a": "done", "b": "done", "c": "done"}


def test_linear_emits_full_lifecycle_events(tmp_path):
    """Tape 事件流：workflow_started → (node_started/completed ×3) → route_taken ×3 → workflow_completed。"""
    orch = _orch(_linear_wf(), tmp_path)
    _orch_run(orch)
    types = [e.type for e in orch.bus.tape.replay()]

    assert types[0] == "workflow_started"
    assert types[-1] == "workflow_completed"
    # 3 个 node 各 started+completed
    assert types.count("node_started") == 3
    assert types.count("node_completed") == 3
    # route_taken 出现（current→next）
    assert types.count("route_taken") == 3
    # route_taken 最后一条 to=$end
    route_events = [e for e in orch.bus.tape.replay() if e.type == "route_taken"]
    assert route_events[-1].data == {"from": "c", "to": "$end"}


def test_route_taken_updates_current_node_in_replay(tmp_path):
    """reducer 据 route_taken 跟踪 current_node（最后一条 route_taken.to=$end）。"""
    orch = _orch(_linear_wf(), tmp_path)
    state = _orch_run(orch)
    # workflow_completed 把 current_node 置 None（reducer 语义）
    assert state.current_node is None


# ── ctx 累积 ──────────────────────────────────────────────────────────────────


def test_ctx_accumulates_across_nodes(tmp_path):
    """b 能读到 a 的 output（ctx.outputs 累积，render 跨 node 引用）。"""
    wf = Workflow(
        name="ctx_test",
        entry="a",
        nodes=[
            ScriptNode(name="a", command="echo hello", routes=[Route(to="b")]),
            # b 的 command 引用 a 的 stdout（验证 ctx 累积 + render）
            ScriptNode(
                name="b",
                command="echo got:$(echo '{{ a.output.stdout }}' | tr -d ' ')",
                routes=[Route(to="$end")],
            ),
        ],
        outputs={},
    )
    orch = _orch(wf, tmp_path)
    state = _orch_run(orch)
    assert state.status == "completed"
    # b 的 output 含 a 透传的 hello（reducer 存 raw output 在 context[node]）
    assert "hello" in state.context["b"]["stdout"]


# ── 条件分支 ──────────────────────────────────────────────────────────────────


def _conditional_wf(path_value: str) -> Workflow:
    """decide(set) → 条件路由 high/low agent → $end。

    path_value 确定性（不靠 claude），决定走 high_agent 还是 low_agent。
    """
    return Workflow(
        name="demo_conditional",
        entry="decide",
        nodes=[
            SetNode(
                name="decide",
                values={"path": path_value},
                routes=[
                    Route(when="output.path == 'high'", to="high_branch"),
                    Route(to="low_branch"),
                ],
            ),
            ScriptNode(name="high_branch", command="echo HIGH", routes=[Route(to="$end")]),
            ScriptNode(name="low_branch", command="echo LOW", routes=[Route(to="$end")]),
        ],
        outputs={"taken": "{{ decide.output.path }}"},
    )


def test_conditional_takes_high_branch(tmp_path):
    orch = _orch(_conditional_wf("high"), tmp_path)
    state = _orch_run(orch)
    assert state.status == "completed"
    assert "high_branch" in state.node_status  # 走了 high
    assert "low_branch" not in state.node_status  # 没走 low


def test_conditional_falls_back_to_low(tmp_path):
    """path=low → 首 when 不命中 → 兜底 low_branch。"""
    orch = _orch(_conditional_wf("low"), tmp_path)
    state = _orch_run(orch)
    assert state.status == "completed"
    assert "low_branch" in state.node_status
    assert "high_branch" not in state.node_status


# ── 回环 + max_iter ───────────────────────────────────────────────────────────


def _loop_wf(tmp_path: Path) -> Workflow:
    """counter（script）每轮 n+1（用 tmpfile 持久化计数），n>=3 停 → done；否则回环。

    用 script + tmpfile 实现「真递增」计数器（set 节点是无状态的，无法自引用上轮 output
    —— 首轮 counter.output.n 未定义触发 UndefinedError）。这更贴近真实循环编排：状态靠
    外部副作用（文件 / agent 记忆）跨轮保持，编排层只管路由。
    """
    counter_file = tmp_path / "counter.txt"

    def _cmd() -> str:
        # 首次：文件不存在 → n=1；否则 n = 上一轮 + 1。写回文件 + echo n。
        return (
            f'n=$(cat {counter_file} 2>/dev/null || echo 0); '
            f'n=$((n + 1)); echo $n > {counter_file}; echo $n'
        )

    return Workflow(
        name="demo_loop",
        entry="counter",
        nodes=[
            ScriptNode(
                name="counter",
                command=_cmd(),
                parse_json=False,
                routes=[
                    Route(when="output.stdout | int >= 3", to="done"),
                    Route(to="counter"),
                ],
            ),
            ScriptNode(name="done", command="echo done", routes=[Route(to="$end")]),
        ],
        outputs={},
    )


def _dead_loop_wf() -> Workflow:
    """永不终止的回环（n 永远=1，不引用上轮）→ max_iter 命中。"""
    return Workflow(
        name="demo_max_iter",
        entry="counter",
        nodes=[
            SetNode(
                name="counter",
                values={"n": "{{ inputs.start | default(0) | int + 1 }}"},
                routes=[
                    Route(when="output.n | int >= 999", to="done"),  # 永不命中
                    Route(to="counter"),
                ],
            ),
            ScriptNode(name="done", command="echo done", routes=[Route(to="$end")]),
        ],
        outputs={},
    )


def test_loop_terminates_at_condition(tmp_path):
    """counter 每轮 +1（tmpfile 持久化），n>=3 停 → done（循环正常终止）。"""
    orch = _orch(_loop_wf(tmp_path), tmp_path)
    state = _orch_run(orch)
    assert state.status == "completed"
    assert "done" in state.node_status
    # counter 最终 stdout=3（3 轮后停）—— reducer last-writer-wins 存最后一次 counter output
    assert state.context["counter"]["stdout"].strip() == "3"


def test_max_iter_produces_workflow_failed(tmp_path):
    """永不终止回环（n 永远 1）→ MaxIterations → workflow_failed{kind: business_config}。

    phase-11 v2.1：``MaxIterationsError`` 是 ``ExecError`` 子类（kind=BUSINESS_CONFIG），
    orchestrator ``_classify_error`` 透传 ``e.kind.value``。
    """
    orch = _orch(_dead_loop_wf(), tmp_path)
    state = _orch_run(orch)
    assert state.status == "failed"
    failed_ev = [e for e in orch.bus.tape.replay() if e.type == "workflow_failed"][0]
    assert failed_ev.data["kind"] == "business_config"


def test_max_iter_override_low(tmp_path):
    """max_iter=2 覆盖：回环第 3 步前就 fail（验证 cli_override 生效）。"""
    orch = _orch(_dead_loop_wf(), tmp_path, max_iter=2)
    state = _orch_run(orch)
    assert state.status == "failed"
    # 只有 counter 执行（done 没到）—— 验证 max_iter 之前就停
    assert "done" not in state.node_status


# ── NoRouteMatch ──────────────────────────────────────────────────────────────


def test_no_route_match_workflow_failed(tmp_path):
    """全 when 不匹配且无兜底 → RouteError → workflow_failed。"""
    wf = Workflow(
        name="no_match",
        entry="decide",
        nodes=[
            SetNode(
                name="decide",
                values={"x": "5"},
                routes=[Route(when="output.x | int > 100", to="rare")],  # 无兜底，x=5 不命中
            ),
            ScriptNode(name="rare", command="echo r", routes=[Route(to="$end")]),
        ],
        outputs={},
    )
    orch = _orch(wf, tmp_path)
    state = _orch_run(orch)
    assert state.status == "failed"
    failed_ev = [e for e in orch.bus.tape.replay() if e.type == "workflow_failed"][0]
    # phase-11 v2.1：RouteError 是 ExecError 子类（kind=BUSINESS_CONFIG，phase=route_deadlock）。
    assert failed_ev.data["kind"] == "business_config"
    assert failed_ev.data["node"] == "decide"


# ── executor 失败 ─────────────────────────────────────────────────────────────


def test_executor_failure_workflow_failed(tmp_path, monkeypatch):
    """executor node_failed → workflow_failed（透传 kind + node 名，F1）。

    phase-11 v2.1：``error_type`` 旧字面值（ExecTimeout）→ ``kind`` 值（transport_timeout），
    ``ExecError.from_failed_data`` 读兼容期经 ``_LEGACY_ERROR_TYPE_TO_KIND`` 反向映射。
    """

    def fake_make_executor(node, agent_tools_server=None, bus=None, **kwargs):
        return FakeExecutor.failing(
            error_type="ExecTimeout", message="超时了", phase="timeout", node_name=node.name,
        )

    # orchestrator / parallel / foreach 都 lazy import make_executor，patch 源模块即统一生效
    monkeypatch.setattr("orca.exec.factory.make_executor", fake_make_executor)

    wf = Workflow(
        name="fail_test",
        entry="a",
        nodes=[ScriptNode(name="a", command="echo", routes=[Route(to="$end")])],
        outputs={},
    )
    orch = _orch(wf, tmp_path)
    state = _orch_run(orch)
    assert state.status == "failed"
    failed_ev = [e for e in orch.bus.tape.replay() if e.type == "workflow_failed"][0]
    # kind=transport_timeout（TRANSPORT_TIMEOUT，由 from_failed_data 反向映射 ExecTimeout）
    assert failed_ev.data["kind"] == "transport_timeout"
    # F1 修复：workflow_failed.data.node 含失败 node 名（SPEC §3.4）
    assert failed_ev.data["node"] == "a"


# ── outputs 求值 ──────────────────────────────────────────────────────────────


def test_workflow_outputs_evaluated(tmp_path):
    """wf.outputs 的 Jinja2 模板渲染正确（orchestrator._evaluate_outputs）。"""
    orch = _orch(_linear_wf(), tmp_path)
    state = _orch_run(orch)
    # workflow_completed.data.outputs 含渲染后的 result
    completed_ev = [e for e in orch.bus.tape.replay() if e.type == "workflow_completed"][0]
    assert "result" in completed_ev.data["outputs"]
    assert "step_c" in completed_ev.data["outputs"]["result"]


# ── phase-10 技术债回填：setup_outputs 注入 runtime context ──────────────────


def test_setup_outputs_injected_and_rendered(tmp_path):
    """setup_outputs 透传 → ctx.setup → ``{{ setup.<agent>.output.<field> }}`` 可渲染。

    MCP 壳主 session 替 setup agent 跑完收集的 outputs，经 orchestrator 包成
    ``{agent: {"output": ...}}`` 存 ctx.setup，execute phase 节点能消费。
    """
    wf = Workflow(
        name="setup_inject",
        entry="a",
        setup=[AgentNode(name="collector", prompt="collect the host")],
        nodes=[
            ScriptNode(
                name="a",
                command="echo {{ setup.collector.output.host }}",
                routes=[Route(to="$end")],
            ),
        ],
        outputs={"result": "{{ a.output.stdout }}"},
    )
    orch = _orch(
        wf, tmp_path, setup_outputs={"collector": {"host": "orbittest"}}
    )
    state = _orch_run(orch)
    assert state.status == "completed"
    completed_ev = [e for e in orch.bus.tape.replay() if e.type == "workflow_completed"][0]
    # setup_outputs 注入后 render 出 orbittest（非空、非原文字面量）
    assert "orbittest" in completed_ev.data["outputs"]["result"]


def test_setup_outputs_none_does_not_break_normal_workflow(tmp_path):
    """无 setup_outputs（None）→ ctx.setup 空 dict，普通 workflow 照常跑（向后兼容）。"""
    orch = _orch(_linear_wf(), tmp_path)  # 不传 setup_outputs
    state = _orch_run(orch)
    assert state.status == "completed"


# ── task 注入 ─────────────────────────────────────────────────────────────────


def test_task_injected_into_inputs(tmp_path):
    """task 位置参数 → inputs.task（render 用 {{ inputs.task }}）。"""
    wf = Workflow(
        name="task_test",
        entry="worker",
        inputs={"task": InputDef(type="string", required=True)},
        nodes=[
            SetNode(
                name="worker",
                values={"echo": "{{ inputs.task }}"},
                routes=[Route(to="$end")],
            ),
        ],
        outputs={"reply": "{{ worker.output.echo }}"},
    )
    orch = _orch(wf, tmp_path, task="做某事")
    state = _orch_run(orch)
    assert state.status == "completed"
    # reducer 存 raw output 在 context[node]（set 的 output 是 {echo: ...}）
    assert state.context["worker"]["echo"] == "做某事"
    completed_ev = [e for e in orch.bus.tape.replay() if e.type == "workflow_completed"][0]
    assert completed_ev.data["outputs"]["reply"] == "做某事"


# ── parallel / foreach 经 orchestrator 主循环（单元层验证 ctx 传递）──────────


def test_orchestrator_runs_parallel_group_and_merges(tmp_path, monkeypatch):
    """parallel 组经主循环：branches 并行执行，下游 merger 能读组聚合输出。

    branch_a/b 用 FakeExecutor 注入确定 output；start/merger 用真 SetExecutor（渲染模板）。
    验证：组聚合 → ctx.outputs['split'] 含 outputs.branch_a.v；merger 跨组引用解析。
    """
    from orca.exec.factory import make_executor as real_make_executor

    def fake_make_executor(node, agent_tools_server=None, bus=None, **kwargs):
        if node.name == "branch_a":
            return FakeExecutor.produces({"v": "A"}, node_name=node.name)
        if node.name == "branch_b":
            return FakeExecutor.produces({"v": "B"}, node_name=node.name)
        # start / merger 等用真 SetExecutor（保留模板渲染语义）
        return real_make_executor(node)

    monkeypatch.setattr("orca.exec.factory.make_executor", fake_make_executor)

    from orca.schema import ParallelGroup

    wf = Workflow(
        name="par_test",
        entry="start",
        nodes=[
            SetNode(name="start", values={"go": "1"}, routes=[Route(to="split")]),
            SetNode(name="branch_a", values={"v": "A"}, routes=[Route(to="$end")]),
            SetNode(name="branch_b", values={"v": "B"}, routes=[Route(to="$end")]),
            # merger 读组聚合：split.output.outputs.branch_a.v
            SetNode(
                name="merger",
                values={"merged": "{{ split.output.outputs.branch_a.v }}+{{ split.output.outputs.branch_b.v }}"},
                routes=[Route(to="$end")],
            ),
        ],
        parallel=[
            ParallelGroup(
                name="split", branches=["branch_a", "branch_b"],
                failure_mode="continue_on_error", routes=[Route(to="merger")],
            ),
        ],
        outputs={"result": "{{ merger.output.merged }}"},
    )
    orch = _orch(wf, tmp_path)
    state = _orch_run(orch)
    assert state.status == "completed"
    completed_ev = [e for e in orch.bus.tape.replay() if e.type == "workflow_completed"][0]
    assert completed_ev.data["outputs"]["result"] == "A+B"
    # route_taken 链：start→split→merger→$end
    route_tos = [e.data["to"] for e in orch.bus.tape.replay() if e.type == "route_taken"]
    assert route_tos == ["split", "merger", "$end"]


def test_orchestrator_runs_foreach_with_locals(tmp_path, monkeypatch):
    """foreach 经主循环：source 取数组，body 收到 item/locals，聚合后下游可读 count。

    maker 用真 SetExecutor（产出 items 数组）；body worker 用 CapturingFake 验证 locals 注入。
    """
    from orca.exec.factory import make_executor as real_make_executor
    from orca.schema import ForeachNode

    seen_items = []

    def fake_make_executor(node, agent_tools_server=None, bus=None, **kwargs):
        if node.name == "worker":
            class CapturingFake(FakeExecutor):
                def __init__(self):
                    super().__init__(
                        FakeExecutor.produces({"done": True}, node_name="worker")._events,
                        node_name="worker",
                    )

                async def exec(self, n, ctx):
                    seen_items.append(ctx.locals.get("item"))
                    async for e in super().exec(n, ctx):
                        yield e
            return CapturingFake()
        # maker 等用真 SetExecutor（保留模板渲染 + 产出 items 数组）
        return real_make_executor(node)

    monkeypatch.setattr("orca.exec.factory.make_executor", fake_make_executor)

    wf = Workflow(
        name="fe_test",
        entry="maker",
        nodes=[
            SetNode(name="maker", values={"items": "[1,2,3]"}, routes=[Route(to="processor")]),
            ForeachNode(
                name="processor",
                source="maker.output.items",
                item_var="item",
                body=ScriptNode(name="worker", command="echo {{ item }}", routes=[]),
                max_concurrent=2,
                routes=[Route(to="$end")],
            ),
        ],
        outputs={"count": "{{ processor.output.count }}"},
    )
    orch = _orch(wf, tmp_path)
    state = _orch_run(orch)
    assert state.status == "completed"
    assert sorted(seen_items) == [1, 2, 3]
    completed_ev = [e for e in orch.bus.tape.replay() if e.type == "workflow_completed"][0]
    assert completed_ev.data["outputs"]["count"] == "3"


# ── _classify_error / _error_node（workflow_failed.data 形状）────────────────


def test_workflow_failed_carries_node_for_route_error(tmp_path):
    """NoRouteMatch → workflow_failed.data.node = 卡住的 node（F1）。"""
    wf = Workflow(
        name="nm", entry="decide",
        nodes=[
            SetNode(name="decide", values={"x": "5"},
                    routes=[Route(when="output.x | int > 100", to="rare")]),
            ScriptNode(name="rare", command="echo", routes=[Route(to="$end")]),
        ],
        outputs={},
    )
    orch = _orch(wf, tmp_path)
    _orch_run(orch)
    failed_ev = [e for e in orch.bus.tape.replay() if e.type == "workflow_failed"][0]
    assert failed_ev.data["node"] == "decide"


def test_workflow_failed_carries_node_for_max_iter(tmp_path):
    """MaxIterations → workflow_failed.data.node = 卡住的 current（F1）。"""
    wf = Workflow(
        name="mi", entry="counter",
        nodes=[
            SetNode(name="counter", values={"n": "1"},
                    routes=[Route(when="output.n | int >= 999", to="done"), Route(to="counter")]),
            ScriptNode(name="done", command="echo", routes=[Route(to="$end")]),
        ],
        outputs={},
    )
    orch = _orch(wf, tmp_path, max_iter=3)
    _orch_run(orch)
    failed_ev = [e for e in orch.bus.tape.replay() if e.type == "workflow_failed"][0]
    assert failed_ev.data["node"] == "counter"  # 卡在 counter 回环


# ── continue_on_error 部分失败的聚合经主循环透传到下游（intent 覆盖）────────────


def test_parallel_continue_on_error_partial_failure_aggregation_visible_downstream(
    tmp_path, monkeypatch,
):
    """parallel 组 ``continue_on_error`` + 一败一成 → 组不抛，下游 merger 能读聚合。

    意图（覆盖 review gap）：验证「部分失败时聚合 dict（含 outputs/errors/count/succeeded）
    真的进了 ``ctx.outputs[group]``，下游 node 可引用 ``group.output.outputs.x`` 与
    ``group.output.errors.y``」。之前单测只验证 run_parallel_group 的返回 dict，
    未覆盖经 orchestrator 累加后的跨节点引用 —— 这是 parallel 部分失败的核心契约。
    """
    from orca.exec.factory import make_executor as real_make_executor
    from orca.schema import ParallelGroup

    def fake_make_executor(node, agent_tools_server=None, bus=None, **kwargs):
        if node.name == "branch_a":
            return FakeExecutor.produces({"v": "A"}, node_name=node.name)
        if node.name == "branch_b":
            return FakeExecutor.failing(error_type="ExecError", message="b 挂了", node_name=node.name)
        return real_make_executor(node)

    monkeypatch.setattr("orca.exec.factory.make_executor", fake_make_executor)

    wf = Workflow(
        name="par_partial",
        entry="start",
        nodes=[
            SetNode(name="start", values={"go": "1"}, routes=[Route(to="split")]),
            SetNode(name="branch_a", values={"v": "A"}, routes=[Route(to="$end")]),
            SetNode(name="branch_b", values={"v": "B"}, routes=[Route(to="$end")]),
            SetNode(
                name="merger",
                values={
                    "ok": "{{ split.output.outputs.branch_a.v }}",
                    "failed": "{{ split.output.errors.branch_b }}",
                    "count": "{{ split.output.count }}",
                    "succeeded": "{{ split.output.succeeded }}",
                },
                routes=[Route(to="$end")],
            ),
        ],
        parallel=[
            ParallelGroup(
                name="split", branches=["branch_a", "branch_b"],
                failure_mode="continue_on_error", routes=[Route(to="merger")],
            ),
        ],
        outputs={
            "ok": "{{ merger.output.ok }}",
            "failed": "{{ merger.output.failed }}",
            "count": "{{ merger.output.count }}",
        },
    )
    orch = _orch(wf, tmp_path)
    state = _orch_run(orch)
    # continue_on_error + 部分成功 → 不抛 → workflow_completed
    assert state.status == "completed"
    completed_ev = [e for e in orch.bus.tape.replay() if e.type == "workflow_completed"][0]
    outputs = completed_ev.data["outputs"]
    assert outputs["ok"] == "A"          # 成功 branch 的值透传到下游
    assert "b 挂了" in outputs["failed"]  # 失败 branch 的 error message 可见
    assert outputs["count"] == "2"       # 聚合 count 透传


def test_foreach_continue_on_error_partial_failure_aggregation_visible_downstream(
    tmp_path, monkeypatch,
):
    """foreach ``continue_on_error`` + 部分失败 → 不抛，下游能读 count/succeeded/errors。

    意图（覆盖 review gap）：验证「foreach 部分失败时聚合 dict 经 orchestrator 累加进
    ``ctx.outputs[node]``，下游 node 可引用 ``node.output.count`` /
    ``node.output.succeeded``」。之前单测只验证 run_foreach 的返回 dict。
    """
    from orca.exec.factory import make_executor as real_make_executor
    from orca.schema import ForeachNode

    def fake_make_executor(node, agent_tools_server=None, bus=None, **kwargs):
        if node.name == "worker":
            # 第 0 / 2 个 item 成功，第 1 个失败 —— 用 ctx.locals._index 区分
            class _ItemAware(FakeExecutor):
                async def exec(self, n, ctx):
                    if ctx.locals.get("_index") == 1:
                        async for e in FakeExecutor.failing(
                            error_type="ExecError", message="item1 挂了", node_name="worker",
                        ).exec(n, ctx):
                            yield e
                        return
                    async for e in FakeExecutor.produces(
                        {"done": True}, node_name="worker",
                    ).exec(n, ctx):
                        yield e
            return _ItemAware(
                FakeExecutor.produces({"done": True}, node_name="worker")._events,
                node_name="worker",
            )
        return real_make_executor(node)

    monkeypatch.setattr("orca.exec.factory.make_executor", fake_make_executor)

    wf = Workflow(
        name="fe_partial",
        entry="maker",
        nodes=[
            SetNode(name="maker", values={"items": "[0,1,2]"}, routes=[Route(to="processor")]),
            ForeachNode(
                name="processor",
                source="maker.output.items",
                item_var="item",
                body=ScriptNode(name="worker", command="echo {{ item }}", routes=[]),
                max_concurrent=2,
                failure_mode="continue_on_error",
                routes=[Route(to="reporter")],
            ),
            SetNode(
                name="reporter",
                values={
                    "count": "{{ processor.output.count }}",
                    "succeeded": "{{ processor.output.succeeded }}",
                },
                routes=[Route(to="$end")],
            ),
        ],
        outputs={
            "count": "{{ reporter.output.count }}",
            "succeeded": "{{ reporter.output.succeeded }}",
        },
    )
    orch = _orch(wf, tmp_path)
    state = _orch_run(orch)
    assert state.status == "completed"
    completed_ev = [e for e in orch.bus.tape.replay() if e.type == "workflow_completed"][0]
    outputs = completed_ev.data["outputs"]
    assert outputs["count"] == "3"       # 总数 3
    assert outputs["succeeded"] == "2"   # 2 成功（item 0 / 2），1 失败（item 1）
