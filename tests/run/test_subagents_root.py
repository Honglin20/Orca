"""tests/run/test_subagents_root.py —— point-to-file subagents_root populate 防漏点测试。

SPEC §4 reviewer3 新风险 #2 闭环：6 处 RunContext 构造点任一漏 populate → agent.md 引用
``{{ subagents_root }}`` 渲染成空串、子 agent Read 失败，且 ``StrictUndefined`` 不兜底（字段
有默认空串）。本测试断言「nas-supernet 场景 6 处构造点 ``ctx.subagents_root`` 非空」。

6 处构造点：
  - orchestrator.__init__（self.ctx）
  - orchestrator._next_node_for_resume（staticmethod，route-only → 空串，例外）
  - orchestrator._bare_instance（resume entry）
  - orchestrator._make_ctx（drive loop 每 node 快照）
  - step._build_ctx（in-session 路径，yaml_path 透传 → workflows_root）
  - app.py dialog ctx（post-run，不渲染 agent.md → 空串，例外）
"""

from __future__ import annotations

from pathlib import Path

from orca.exec.context import RunContext
from orca.run.orchestrator import _compute_subagents_root
from orca.run.step import _build_ctx, _workflows_root_from_yaml
from orca.schema import AgentNode, Workflow

# 复用 run 测试的 make_bus fixture 工厂（Tape 写 tmp_path，不污染 cwd）
from tests.run.conftest import make_bus


# ── _compute_subagents_root ──────────────────────────────────────────────────


def test_compute_subagents_root_existing_dir(tmp_path):
    """workflows_root / subagents / wf_name 存在 → 绝对路径字符串（v3 公式）。"""
    sub = tmp_path / "subagents" / "nas-supernet"
    sub.mkdir(parents=True)
    out = _compute_subagents_root(tmp_path, "nas-supernet")
    assert out == str(sub)
    assert Path(out).is_absolute() or Path(out).exists()  # tmp_path 本就绝对


def test_compute_subagents_root_missing_dir_empty(tmp_path):
    """目录不存在 → 空串（SPEC §3.3：无 subagents 的 workflow 正常）。"""
    assert _compute_subagents_root(tmp_path, "quant-ptq") == ""


def test_compute_subagents_root_none_workflows_root():
    """workflows_root=None（orchestrator 上下文不可达）→ 空串。"""
    assert _compute_subagents_root(None, "nas-supernet") == ""


def test_compute_subagents_root_empty_wf_name(tmp_path):
    """wf_name 空串（schema 不允许但防御）→ 空串。"""
    assert _compute_subagents_root(tmp_path, "") == ""


# ── 6 处 RunContext 构造点 ─────────────────────────────────────────────────────


def _make_wf(name: str = "nas-supernet") -> Workflow:
    node = AgentNode(name="n1", kind="agent", executor="claude", prompt="hello")
    return Workflow(name=name, description="d", entry="n1", nodes=[node], parallel=[])


def test_bare_runcontext_default_subagents_root_empty():
    """RunContext 默认 ``subagents_root=""``（向后兼容，旧 caller 不破）。"""
    ctx = RunContext(inputs={}, outputs={}, run_id="r1")
    assert ctx.subagents_root == ""


def test_runcontext_with_locals_carries_subagents_root():
    """``with_locals`` 经 ``dataclasses.replace`` 自动携带 subagents_root（frozen dataclass 防漏）。"""
    ctx = RunContext(
        inputs={}, outputs={}, run_id="r1",
        subagents_root="/abs/path",
    )
    derived = ctx.with_locals({"item": "x"})
    assert derived.subagents_root == "/abs/path"


def test_runcontext_with_guidance_carries_subagents_root():
    """``with_guidance`` 同款自动携带。"""
    ctx = RunContext(
        inputs={}, outputs={}, run_id="r1",
        subagents_root="/abs/path",
    )
    derived = ctx.with_guidance("do X")
    assert derived.subagents_root == "/abs/path"


def test_orchestrator_init_populates_subagents_root(tmp_path):
    """orchestrator.__init__ 的 self.ctx 在 nas-supernet 场景非空（SPEC §4 #1 of 6）。"""
    from orca.run.orchestrator import Orchestrator

    workflows_root = tmp_path
    (workflows_root / "subagents" / "nas-supernet").mkdir(parents=True)
    bus, _tape = make_bus(tmp_path, run_id="r1")
    orch = Orchestrator(_make_wf(), bus, workflows_root=workflows_root)
    assert orch.ctx.subagents_root == str(workflows_root / "subagents" / "nas-supernet")


def test_orchestrator_make_ctx_carries_subagents_root(tmp_path):
    """orchestrator._make_ctx（drive loop 每 node 快照）保留 subagents_root（SPEC §4 #4 of 6）。

    _make_ctx 是 render 实际用的 ctx 构造点——漏 populate 会让 render 拿不到 subagents_root。
    """
    from orca.run.orchestrator import Orchestrator

    workflows_root = tmp_path
    (workflows_root / "subagents" / "nas-supernet").mkdir(parents=True)
    bus, _tape = make_bus(tmp_path, run_id="r1")
    orch = Orchestrator(_make_wf(), bus, workflows_root=workflows_root)
    snapshot = orch._make_ctx({"n1": {"output": "ok"}})
    assert snapshot.subagents_root == str(workflows_root / "subagents" / "nas-supernet")


def test_orchestrator_bare_instance_populates_subagents_root(tmp_path):
    """orchestrator._bare_instance（resume entry）populate（SPEC §4 #3 of 6）。"""
    from orca.run.orchestrator import Orchestrator
    from orca.schema import RunState

    workflows_root = tmp_path
    (workflows_root / "subagents" / "nas-supernet").mkdir(parents=True)
    bus, _tape = make_bus(tmp_path, run_id="r1")
    state = RunState(
        run_id="r1", status="running", current_node="n1",
        workflow_name="nas-supernet",
    )
    orch = Orchestrator._bare_instance(
        _make_wf(), bus, state, "n1", {}, {}, workflows_root=workflows_root,
    )
    assert orch.ctx.subagents_root == str(workflows_root / "subagents" / "nas-supernet")


def test_orchestrator_next_node_for_resume_uses_empty_subagents_root():
    """orchestrator._next_node_for_resume 是 staticmethod 无 workflows_root → 空串（SPEC §4 例外）。

    此路径仅用于 route 求值（不渲染 agent.md），route.when 不引 {{ subagents_root }}——
    SPEC「orchestrator 上下文可达则算绝对路径，否则空串」裁决下空串合规。
    """
    wf = _make_wf()
    nxt = type("_StubOrch", (), {})  # 仅取 staticmethod，不需 instance
    from orca.run.orchestrator import Orchestrator
    out = Orchestrator._next_node_for_resume(wf, None, {})
    assert out == "n1"  # entry 返回，未渲染；route-only 路径


def test_step_build_ctx_populates_subagents_root(tmp_path):
    """step._build_ctx（in-session 路径）在 yaml_path 透传时 populate（SPEC §4 #5 of 6）。"""
    workflows_root = tmp_path
    (workflows_root / "subagents" / "nas-supernet").mkdir(parents=True)
    yaml_path = workflows_root / "nas-supernet.yaml"
    yaml_path.write_text("name: nas-supernet", encoding="utf-8")
    wr = _workflows_root_from_yaml(str(yaml_path))
    assert wr == workflows_root.resolve()
    ctx = _build_ctx(_make_wf(), {}, {}, "r1", workflows_root=wr)
    assert ctx.subagents_root == str((workflows_root / "subagents" / "nas-supernet").resolve())


def test_step_build_ctx_no_workflows_root_empty():
    """step._build_ctx 无 workflows_root → 空串（in-session 旧 caller 向后兼容）。"""
    ctx = _build_ctx(_make_wf(), {}, {}, "r1")  # workflows_root 默认 None
    assert ctx.subagents_root == ""


def test_step_workflows_root_from_yaml_none():
    """yaml_path None / 空串 → None（向后兼容）。"""
    assert _workflows_root_from_yaml(None) is None
    assert _workflows_root_from_yaml("") is None


# ── 集成：advance_step / _recover_step_result（point-to-file 透传 e2e）────────


def test_advance_step_first_arm_populates_subagents_root(tmp_path, monkeypatch):
    """advance_step 首次 arm（pending → entry 节点）的 ctx.subagents_root 非空。

    SPEC §4 #5 of 6 集成验证：yaml_path 透传 → workflows_root → subagents_root 在
    StepResult.prompt（或 prompt_file）渲染产物里 inline 为绝对路径。
    """
    from orca.events.tape import Tape
    from orca.run.step import advance_step

    workflows_root = tmp_path
    (workflows_root / "subagents" / "demo-wf").mkdir(parents=True)
    yaml_path = workflows_root / "demo-wf.yaml"
    yaml_path.write_text(
        "name: demo-wf\ndescription: d\nentry: n1\n"
        "nodes:\n  - name: n1\n    kind: agent\n    executor: claude\n"
        "    prompt: 'Read {{ subagents_root }}/helper.md then act.'\n",
        encoding="utf-8",
    )
    from orca.compile.parser import load_workflow
    wf = load_workflow(yaml_path)
    tape = Tape(tmp_path / "tape.jsonl", run_id="r1", resume=True)

    monkeypatch.chdir(tmp_path)
    result = advance_step(
        tape, wf, inputs={}, run_id="r1", yaml_path=str(yaml_path), prompts_dir=None,
    )
    assert not result.done
    assert "{{ subagents_root }}" not in result.prompt  # 已 inline
    assert str(workflows_root / "subagents" / "demo-wf") in result.prompt


def test_recover_step_result_re_arm_preserves_subagents_root(tmp_path, monkeypatch):
    """recoverable 失败 → 重 arm 同节点 → 重渲染 prompt 仍含 inline 的 subagents_root。

    SPEC §4 #5 of 6 集成验证：_recover_step_result 内部 _build_ctx 透传 workflows_root
    （tests/run/step.py:604-605），重渲染的 prompt 不丢 subagents_root。这是 reviewer
    新风险 #2 的端到端覆盖——若 _recover_step_result 忘记透传，重 arm 的 prompt 会
    渲染成空串 → render fail loud 抛 ExecError（防漏点兜底）。
    """
    import asyncio

    from orca.events.bus import EventBus
    from orca.events.tape import Tape
    from orca.iface.in_session._step_io import apply_step_result
    from orca.run.step import advance_step

    workflows_root = tmp_path
    (workflows_root / "subagents" / "demo-wf").mkdir(parents=True)
    yaml_path = workflows_root / "demo-wf.yaml"
    yaml_path.write_text(
        "name: demo-wf\ndescription: d\nentry: n1\n"
        "nodes:\n  - name: n1\n    kind: agent\n    executor: claude\n"
        "    prompt: 'Read {{ subagents_root }}/helper.md then act.'\n",
        encoding="utf-8",
    )
    from orca.compile.parser import load_workflow
    wf = load_workflow(yaml_path)
    tape = Tape(tmp_path / "tape.jsonl", run_id="r1", resume=True)
    bus = EventBus(tape)
    monkeypatch.chdir(tmp_path)

    # 1) 首次 arm（pending → entry），把 emits 落 tape
    r1 = advance_step(
        tape, wf, inputs={}, run_id="r1", yaml_path=str(yaml_path), prompts_dir=None,
    )
    asyncio.run(apply_step_result(bus, r1, wf=wf, run_id="r1"))
    assert str(workflows_root / "subagents" / "demo-wf") in r1.prompt

    # 2) 提交失败哨兵（触发 recoverable）→ _recover_step_result 重 arm
    bad_output = (
        '{"_sentinel": "orca_node_failed_v1", "blocked_on": "test", '
        '"tried": [], "reason": "simulated"}'
    )
    r2 = advance_step(
        tape, wf, output=bad_output, run_id="r1", yaml_path=str(yaml_path),
        prompts_dir=None,
    )
    # 重 arm 仍存活（recoverable），prompt 含 inline subagents_root（未丢）
    assert r2.recoverable is True, "失败哨兵应触发 recoverable 重 arm"
    assert str(workflows_root / "subagents" / "demo-wf") in r2.prompt, (
        "重 arm 后的 prompt 应仍含 inline subagents_root（_recover_step_result 透传）"
    )


# ── 加载期绑定：load_workflow 把 workflows_root 写进 wf（单一真源）──────────────


def test_load_workflow_binds_workflows_root(tmp_path):
    """``load_workflow`` 加载期绑定 ``wf.workflows_root = yaml 目录（resolve 绝对）``。

    point-to-file 协议（SPEC §3.2/§4）：确定性路径只在加载期解析一次，运行期所有
    RunContext 构造点从 ``wf.workflows_root`` 推导 subagents_root，零参数透传。
    """
    yaml_path = tmp_path / "demo-wf.yaml"
    yaml_path.write_text(
        "name: demo-wf\ndescription: d\nentry: n1\n"
        "nodes:\n  - name: n1\n    kind: agent\n    executor: claude\n"
        "    prompt: 'hello'\n",
        encoding="utf-8",
    )
    from orca.compile.parser import load_workflow
    wf = load_workflow(yaml_path)
    assert wf.workflows_root == tmp_path.resolve()


def test_advance_step_second_arm_without_yaml_path_uses_wf_workflows_root(
    tmp_path, monkeypatch,
):
    """历史 bug 复现路径：``orca next`` 不传 yaml_path，下一节点渲染仍拿到 subagents_root。

    根因：``_build_ctx`` 只从 ``yaml_path`` 推导 workflows_root，``cli.py next`` /
    daemon 漏传 → workflows_root=None → subagents_root="" → render fail loud。
    修复：``load_workflow`` 把 workflows_root 绑到 wf，``_build_ctx`` 在形参为 None
    时回退 ``wf.workflows_root``——本测试两次 ``advance_step`` 均**不传 yaml_path**，
    断言第二节点 prompt 已 inline 绝对路径。
    """
    import asyncio

    from orca.events.bus import EventBus
    from orca.events.tape import Tape
    from orca.iface.in_session._step_io import apply_step_result
    from orca.run.step import advance_step

    workflows_root = tmp_path
    (workflows_root / "subagents" / "demo-wf").mkdir(parents=True)
    yaml_path = workflows_root / "demo-wf.yaml"
    yaml_path.write_text(
        "name: demo-wf\ndescription: d\nentry: n1\n"
        "nodes:\n"
        "  - name: n1\n    kind: agent\n    executor: claude\n"
        "    prompt: 'first'\n    routes:\n      - to: n2\n"
        "  - name: n2\n    kind: agent\n    executor: claude\n"
        "    prompt: 'Read {{ subagents_root }}/helper.md then act.'\n",
        encoding="utf-8",
    )
    from orca.compile.parser import load_workflow
    wf = load_workflow(yaml_path)
    assert wf.workflows_root == workflows_root.resolve()

    tape = Tape(tmp_path / "tape.jsonl", run_id="r1", resume=True)
    bus = EventBus(tape)
    monkeypatch.chdir(tmp_path)

    # 1) bootstrap（不传 yaml_path）：entry 渲染
    r1 = advance_step(tape, wf, inputs={}, run_id="r1", prompts_dir=None)
    assert not r1.done
    asyncio.run(apply_step_result(bus, r1, wf=wf, run_id="r1"))

    # 2) 完成 entry → advance 分支渲染下一节点（模拟 orca next --output，不传 yaml_path）
    r2 = advance_step(tape, wf, output="ok", run_id="r1", prompts_dir=None)
    assert not r2.done
    assert r2.node == "n2"
    assert "{{ subagents_root }}" not in r2.prompt, "next 渲染应已 inline subagents_root"
    assert str(workflows_root / "subagents" / "demo-wf") in r2.prompt, (
        "不传 yaml_path 时 next 分支应回退 wf.workflows_root（历史 bug 回归防线）"
    )
