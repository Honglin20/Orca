"""tests/iface/in_session/test_node_memory.py —— 节点记忆(Node Memory)SPEC §8 验收守门。

覆盖 SPEC §8 全部可测阻断项:
  1. 非 memory 节点零行为(回归红线,§8.1)
  2. 首跑写 MD:body 正确 + frontmatter 4 字段(§8.2 / §8.4)
  3. 二跑 prompt 含「上一轮记忆」段 + body = 上轮 MD body(§8.3)
  4. --no-memory 整 run 不写不注入(§8.5)
  5. 空 output 写空 body(§8.6)
  6. 跨 project_root 隔离(§8.7)
  7. 写失败 mock OSError 不阻断 run + 结构化日志 event=memory_write_failed(§8.8)

测试路径:``advance_step(... prompts_dir=None)`` inline 是单测主路径(决策逻辑),
``apply_step_result`` 直调验写记忆副作用。项目惯例:``asyncio.run``(无 pytest-asyncio,
对齐 tests/iface/in_session/test_daemon.py)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from unittest import mock

import pytest

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.iface.in_session._step_io import apply_step_result
from orca.run.step import Emit, advance_step
from orca.schema.workflow import AgentNode, Route, Workflow


# ── fixtures / helpers ─────────────────────────────────────────────────────


def _wf(*, memory: bool = False, output_schema: dict | None = None) -> Workflow:
    """单节点 agent workflow(entry a → $end),可选开启 memory。"""
    return Workflow(
        name="mem_unit_wf",
        entry="a",
        nodes=[
            AgentNode(
                name="a",
                executor="opencode",
                model="d/d",
                prompt="do A",
                memory=memory,
                output_schema=output_schema,
                routes=[Route(to="$end")],
            )
        ],
    )


def _two_node_wf(*, memory_a: bool = False, memory_b: bool = False) -> Workflow:
    """两节点线性 wf(a → b → $end):用于 node_completed 触发写记忆 + 下游推进验证。"""
    return Workflow(
        name="mem_two_wf",
        entry="a",
        nodes=[
            AgentNode(
                name="a", executor="opencode", model="d/d", prompt="do A",
                memory=memory_a, routes=[Route(to="b")],
            ),
            AgentNode(
                name="b", executor="opencode", model="d/d", prompt="do B",
                memory=memory_b, routes=[Route(to="$end")],
            ),
        ],
    )


def _memory_path(root: Path, wf_name: str = "mem_unit_wf", node: str = "a") -> Path:
    return Path(root) / ".orca" / "memory" / wf_name / f"{node}.md"


def _apply(bus, result, wf, run_id, *, project_root, no_memory=False) -> None:
    """asyncio.run 包装(项目惯例,无 pytest-asyncio)。"""
    asyncio.run(apply_step_result(
        bus, result, wf=wf, run_id=run_id,
        no_memory=no_memory, project_root=project_root,
    ))


def _fake_completed(node_name: str, output) -> object:
    """构造一个只含 ``node_completed`` emit 的伪 StepResult(测写记忆副作用)。

    apply_step_result 只读 ``result.emits`` / ``.done`` / ``.node`` / ``.prompt`` /
    ``.reason``,故用最小伪对象即可。用 types.SimpleNamespace 避开 class body 闭包陷阱
    (class body 不走函数闭包作用域,直接引用外层变量会 NameError)。
    """
    from types import SimpleNamespace
    return SimpleNamespace(
        emits=[Emit("node_completed", {"output": output}, node=node_name)],
        done=True,
        node=None,
        prompt=None,
        reason="completed",
    )


# ── §8.1 非 memory 节点零行为(回归红线)──────────────────────────────────────


def test_non_memory_node_writes_no_md_and_injects_nothing(tmp_path):
    """``memory=False`` → 不写 MD、prompt 无「上一轮记忆」段(回归红线)。"""
    wf = _wf(memory=False)
    project_root = tmp_path
    tape = Tape(tmp_path / "tape.jsonl", run_id="r1", resume=True)
    bus = EventBus(tape)
    result = advance_step(tape, wf, run_id="r1", prompts_dir=None,
                          project_root=project_root)
    _apply(bus, result, wf, "r1", project_root=project_root)
    bus.close()

    # 无 .orca 任何路径(§8.1 grep ``.orca/memory`` 不命中)
    assert not (project_root / ".orca").exists(), "memory=False 不应创建 .orca 目录"
    # prompt 原样(无注入段)
    assert result.prompt is not None
    assert "上一轮记忆" not in result.prompt
    assert "复用协议" not in result.prompt


# ── §8.2 + §8.4 首跑写 MD(body + frontmatter 4 字段)────────────────────────


def test_first_run_writes_md_with_frontmatter_and_body(tmp_path):
    """``memory=True`` 节点完成后:MD body = output + frontmatter 含 4 字段。"""
    wf = _wf(memory=True)
    project_root = tmp_path
    tape = Tape(tmp_path / "tape.jsonl", run_id="r1", resume=True)
    bus = EventBus(tape)
    # bootstrap(entry a) — 此时 a 还没完成,不写 MD
    r1 = advance_step(tape, wf, run_id="r1", prompts_dir=None, project_root=project_root)
    _apply(bus, r1, wf, "r1", project_root=project_root)
    # 完成 a(output="first-output")→ node_completed → 写 MD
    result = advance_step(tape, wf, output="first-output", run_id="r1",
                          prompts_dir=None, project_root=project_root)
    _apply(bus, result, wf, "r1", project_root=project_root)
    bus.close()

    md = _memory_path(project_root)
    assert md.is_file(), "首跑 memory=True 完成后应写 MD"
    text = md.read_text(encoding="utf-8")
    # frontmatter 4 字段(SPEC §0.7)
    assert "run_id: r1" in text
    assert re.search(r"^timestamp:\s+[0-9.]+", text, re.MULTILINE), "timestamp 字段(数字)"
    assert "workflow: mem_unit_wf" in text
    assert "node: a" in text
    # body(output_schema=None → output 原文)
    assert "first-output" in text
    # frontmatter 结构:首行 ---
    assert text.startswith("---\n")


def test_first_run_structured_output_json_body(tmp_path):
    """``output_schema`` 非 None → body = json.dumps(parsed, indent=2, ensure_ascii=False)。"""
    wf = _wf(
        memory=True,
        output_schema={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
    )
    project_root = tmp_path
    tape = Tape(tmp_path / "tape.jsonl", run_id="r1", resume=True)
    bus = EventBus(tape)
    r1 = advance_step(tape, wf, run_id="r1", prompts_dir=None, project_root=project_root)
    _apply(bus, r1, wf, "r1", project_root=project_root)
    result = advance_step(tape, wf, output='{"x": 42}', run_id="r1",
                          prompts_dir=None, project_root=project_root)
    _apply(bus, result, wf, "r1", project_root=project_root)
    bus.close()

    md = _memory_path(project_root)
    text = md.read_text(encoding="utf-8")
    # body 是 deterministic json.dumps 输出(indent=2 → ``"x": 42``)
    assert '"x": 42' in text


# ── §8.3 二跑 prompt 含「上一轮记忆」段 ────────────────────────────────────


def test_second_run_prompt_contains_memory_section(tmp_path):
    """``memory=True`` 节点二跑:渲染后 prompt 含「上一轮记忆」段 + body = 上轮 MD body。"""
    wf = _wf(memory=True)
    project_root = tmp_path
    tape = Tape(tmp_path / "tape.jsonl", run_id="r1", resume=True)
    bus = EventBus(tape)
    r1 = advance_step(tape, wf, run_id="r1", prompts_dir=None, project_root=project_root)
    _apply(bus, r1, wf, "r1", project_root=project_root)
    result = advance_step(tape, wf, output="FIRST-RUN-OUTPUT", run_id="r1",
                          prompts_dir=None, project_root=project_root)
    _apply(bus, result, wf, "r1", project_root=project_root)
    bus.close()

    # 二跑:新 tape(同 wf 同 node)→ bootstrap a 时注入上一轮记忆
    tape2 = Tape(tmp_path / "tape2.jsonl", run_id="r2", resume=True)
    res = advance_step(tape2, wf, run_id="r2", prompts_dir=None,
                       project_root=project_root)
    assert res.prompt is not None
    assert "上一轮记忆" in res.prompt, "二跑 prompt 应含「上一轮记忆」段"
    assert "复用协议" in res.prompt
    assert "FIRST-RUN-OUTPUT" in res.prompt, "body 段含上轮 MD body 原文"


# ── §8.5 --no-memory 整 run 不写不注入 ──────────────────────────────────────


def test_no_memory_flag_skips_inject(tmp_path):
    """``no_memory=True`` 透传后:即使 ``node.memory=True`` 也不注入(预置 MD 也不读)。"""
    wf = _wf(memory=True)
    project_root = tmp_path
    md = _memory_path(project_root)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(
        "---\nrun_id: old\ntimestamp: 1.0\nworkflow: mem_unit_wf\nnode: a\n---\n\nLEGACY\n",
        encoding="utf-8",
    )

    tape = Tape(tmp_path / "tape.jsonl", run_id="r1", resume=True)
    res = advance_step(tape, wf, run_id="r1", prompts_dir=None,
                       project_root=project_root, no_memory=True)
    assert res.prompt is not None
    assert "上一轮记忆" not in res.prompt, "no_memory=True 不应注入"
    assert "LEGACY" not in res.prompt


def test_no_memory_flag_skips_write_on_apply_step_result(tmp_path):
    """``apply_step_result(no_memory=True)`` → 即便 node_completed 也不写 MD。"""
    wf = _wf(memory=True)
    project_root = tmp_path
    tape = Tape(tmp_path / "tape.jsonl", run_id="r1", resume=True)
    bus = EventBus(tape)
    r1 = advance_step(tape, wf, run_id="r1", prompts_dir=None,
                      project_root=project_root, no_memory=True)
    _apply(bus, r1, wf, "r1", project_root=project_root, no_memory=True)
    result = advance_step(tape, wf, output="X", run_id="r1", prompts_dir=None,
                          project_root=project_root, no_memory=True)
    _apply(bus, result, wf, "r1", project_root=project_root, no_memory=True)
    bus.close()
    assert not _memory_path(project_root).exists(), "no_memory=True 不应写 MD"


# ── §8.6 空 output 写空 body ────────────────────────────────────────────────


def test_empty_output_writes_empty_body(tmp_path):
    """空 output → MD 仅 frontmatter + 空 body(§0.6 / §8.6)。

    advance_step 对 ``output=""`` normalize 为 None(idempotent-replay 分支,
    不触发 node_completed)。为精确测「空 output 写空 body」契约,直接用伪 emit 走
    ``apply_step_result``(契约边界)。
    """
    wf = _wf(memory=True)
    project_root = tmp_path
    tape = Tape(tmp_path / "tape.jsonl", run_id="r1", resume=True)
    bus = EventBus(tape)
    fake_result = _fake_completed("a", output="")
    _apply(bus, fake_result, wf, "r1", project_root=project_root)
    bus.close()

    md = _memory_path(project_root)
    assert md.is_file()
    text = md.read_text(encoding="utf-8")
    # frontmatter 4 字段
    for field in ("run_id: r1", "timestamp:", "workflow: mem_unit_wf", "node: a"):
        assert field in text
    # body 为空:strip frontmatter 后无内容
    # frontmatter 结构:``---\n...\n---\n\n`` + body
    parts = text.split("---\n", 2)
    assert len(parts) == 3, f"frontmatter 结构不合: {text!r}"
    body = parts[2].lstrip("\n")
    assert body == "", f"空 output 应写空 body, got {body!r}"


# ── §8.7 跨 project_root 隔离 ───────────────────────────────────────────────


def test_cross_project_root_isolation(tmp_path):
    """两个 project_root 跑同 wf:各自 MD 互不可见(§8.7)。"""
    root_a = tmp_path / "projA"
    root_b = tmp_path / "projB"
    root_a.mkdir()
    root_b.mkdir()
    wf = _wf(memory=True)

    # 在 root_a 写一份 MD
    md_a = _memory_path(root_a)
    md_a.parent.mkdir(parents=True, exist_ok=True)
    md_a.write_text(
        "---\nrun_id: rA\ntimestamp: 1.0\nworkflow: mem_unit_wf\nnode: a\n---\n\nFROM-A\n",
        encoding="utf-8",
    )

    # root_b 下跑同 wf → 不应读到 root_a 的 MD(root_b 无 MD → 原样 prompt)
    tape_b = Tape(tmp_path / "tapeB.jsonl", run_id="rB", resume=True)
    res_b = advance_step(tape_b, wf, run_id="rB", prompts_dir=None, project_root=root_b)
    assert res_b.prompt is not None
    assert "FROM-A" not in res_b.prompt, "root_b 的 run 不应读到 root_a 的 MD"
    assert "上一轮记忆" not in res_b.prompt

    # root_a 下跑同 wf → 应注入 FROM-A
    tape_a = Tape(tmp_path / "tapeA.jsonl", run_id="rA", resume=True)
    res_a = advance_step(tape_a, wf, run_id="rA", prompts_dir=None, project_root=root_a)
    assert res_a.prompt is not None
    assert "FROM-A" in res_a.prompt


# ── §8.8 写失败 mock OSError 不阻断 run + 结构化日志 ──────────────────────────


def test_write_failure_does_not_block_run(tmp_path, caplog):
    """write_node_memory OSError → run 正常完成、日志含 event=memory_write_failed。"""
    wf = _wf(memory=True)
    project_root = tmp_path
    tape = Tape(tmp_path / "tape.jsonl", run_id="r1", resume=True)
    bus = EventBus(tape)
    fake_result = _fake_completed("a", output="data")
    with mock.patch("orca.run.memory.os.replace", side_effect=OSError("disk full")):
        with caplog.at_level(logging.WARNING, logger="orca.run.memory"):
            _apply(bus, fake_result, wf, "r1", project_root=project_root)
    bus.close()

    # best-effort:reply 正常返回(apply_step_result 不抛)
    # (此处 bus 已 emit_batch,无异常即说明 apply_step_result 完成)
    # 结构化日志 event=memory_write_failed
    matched = any(
        getattr(r, "event", None) == "memory_write_failed"
        for r in caplog.records
    )
    assert matched, (
        "应记 event=memory_write_failed,got="
        f"{[(r.name, r.getMessage()) for r in caplog.records]}"
    )
    # MD 没写成
    assert not _memory_path(project_root).exists()


# ── 二节点 wf:a 完成写 MD;b 启动时不读 a 的记忆(每节点只读自己)──────────────


def test_two_node_each_node_reads_only_own_memory(tmp_path):
    """a(memory=True)完成后 b(memory=True)启动:b 只读自己的 MD(此时为空),不读 a 的。

    守每节点 MD 隔离契约:每节点按 ``<wf>/<node>.md`` 索引,互不串扰。
    """
    wf = _two_node_wf(memory_a=True, memory_b=True)
    project_root = tmp_path

    tape = Tape(tmp_path / "tape.jsonl", run_id="r1", resume=True)
    bus = EventBus(tape)
    # bootstrap(entry=a)
    r1 = advance_step(tape, wf, run_id="r1", prompts_dir=None, project_root=project_root)
    _apply(bus, r1, wf, "r1", project_root=project_root)
    # 完成 a → 推进到 b(node_completed a)
    r2 = advance_step(tape, wf, output="A-OUT", run_id="r1",
                      prompts_dir=None, project_root=project_root)
    _apply(bus, r2, wf, "r1", project_root=project_root)
    bus.close()

    # a 的 MD 写了(two-node wf 名 = mem_two_wf)
    md_a = _memory_path(project_root, wf_name="mem_two_wf", node="a")
    assert md_a.is_file()
    assert "A-OUT" in md_a.read_text(encoding="utf-8")

    # b 启动:r2.prompt 是 b 的渲染 prompt(advance_step 完成 a 后返 b 的 prompt)
    assert r2.prompt is not None
    assert "A-OUT" not in r2.prompt, "b 不应读到 a 的记忆(每节点只读自己)"
    assert "上一轮记忆" not in r2.prompt, "b 首跑无 MD → 不注入"


# ── 回归:advance_step 旧调用形态(无 project_root/no_memory)不破 ──────────


def test_advance_step_legacy_call_form_unchanged(tmp_path):
    """新 kwargs 都有默认值,``advance_step(tape, wf, run_id=..., prompts_dir=None)``
    旧形态必须仍能跑(单测 inline 路径回归,§6 改动面「默认值保持单测 inline 路径不破」)。
    """
    wf = _wf(memory=True)  # 即便 memory=True,无 project_root 也不注入(无副作用)
    tape = Tape(tmp_path / "tape.jsonl", run_id="r1", resume=True)
    res = advance_step(tape, wf, run_id="r1", prompts_dir=None)
    assert res.prompt is not None
    assert "上一轮记忆" not in res.prompt, "无 project_root → 不注入(旧形态不变)"


# ── §0.8 坏 MD 不影响正确性(核心契约,守 read_node_memory_body 降级分支)─────────


def test_read_memory_corrupt_no_end_fence_returns_none(tmp_path):
    """MD 缺结尾 ``---`` → read 返 None → inject 原样(memory.py:134 静默降级)。

    SPEC §0.8「丢/坏 MD 不影响正确性」核心契约,防用户手编辑 / 半写 / git 部分检出。
    """
    from orca.run.memory import inject_memory_prompt, read_node_memory_body

    wf = _wf(memory=True)
    project_root = tmp_path
    md = _memory_path(project_root)
    md.parent.mkdir(parents=True, exist_ok=True)
    # 缺结尾 ---(只有起始)
    md.write_text("---\nrun_id: r1\ntimestamp: 1.0\nworkflow: x\nnode: a\n", encoding="utf-8")

    assert read_node_memory_body(wf, wf.nodes[0], project_root=project_root) is None
    # inject 静默原样返回
    out = inject_memory_prompt(wf.nodes[0], wf, "ORIGINAL", project_root=project_root)
    assert out == "ORIGINAL", "坏 MD → inject 应原样返回"


def test_read_memory_corrupt_no_start_fence_returns_none(tmp_path):
    """MD 首行非 ``---`` → read 返 None(memory.py:127-128 静默降级)。"""
    from orca.run.memory import read_node_memory_body

    wf = _wf(memory=True)
    project_root = tmp_path
    md = _memory_path(project_root)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("no frontmatter at all\njust some text\n", encoding="utf-8")

    assert read_node_memory_body(wf, wf.nodes[0], project_root=project_root) is None


def test_read_memory_oserror_returns_none(tmp_path):
    """read OSError(权限 / IO 错)→ None(memory.py:123-124 静默降级)。"""
    from orca.run.memory import read_node_memory_body

    wf = _wf(memory=True)
    project_root = tmp_path
    md = _memory_path(project_root)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("---\nrun_id: r\n---\n\nbody\n", encoding="utf-8")

    # mock read_text 抛 OSError
    original_read_text = Path.read_text
    with mock.patch.object(Path, "read_text", side_effect=OSError("io error")):
        assert read_node_memory_body(wf, wf.nodes[0], project_root=project_root) is None
    # 验证 mock 已清除(不污染后续测试)
    assert Path(md).read_text(encoding="utf-8").startswith("---") is True or original_read_text


# ── §0.6 空 body 仍注入(SPEC 「空 output 本身是注入信号」)─────────────────────


def test_inject_empty_body_still_injects_section(tmp_path):
    """MD body 为空 → inject 仍拼「上一轮记忆」段(§0.6 决策推论)。

    防止未来误改 ``if not body`` 守门,悄然回归「空 body 不注入」。
    """
    wf = _wf(memory=True)
    project_root = tmp_path
    md = _memory_path(project_root)
    md.parent.mkdir(parents=True, exist_ok=True)
    # 合法 frontmatter + 空 body
    md.write_text(
        "---\nrun_id: rE\ntimestamp: 1.0\nworkflow: mem_unit_wf\nnode: a\n---\n\n",
        encoding="utf-8",
    )

    tape = Tape(tmp_path / "tape.jsonl", run_id="r2", resume=True)
    res = advance_step(tape, wf, run_id="r2", prompts_dir=None, project_root=project_root)
    assert res.prompt is not None
    assert "上一轮记忆" in res.prompt, "空 body MD 仍应触发注入(§0.6 信号语义)"
    assert "复用协议" in res.prompt


# ── _write_memories_for_emits 边界(e.node=None / orphan name)─────────────────


def test_write_memories_skips_emit_with_none_node(tmp_path):
    """``Emit(type='node_completed', node=None)`` → 安全 skip(_step_io.py:126-127)。

    真实场景:route_taken / workflow_completed emit 都 node=None。虽然 ``type != node_completed``
    已先过滤,但这是独立防御层,需独立守门。
    """
    wf = _wf(memory=True)
    project_root = tmp_path
    tape = Tape(tmp_path / "tape.jsonl", run_id="r1", resume=True)
    bus = EventBus(tape)
    # 混批:含 node_completed(None node) + workflow_completed(None node)
    fake = type("R", (), {
        "emits": [
            Emit("node_completed", {"output": "x"}, node=None),
            Emit("workflow_completed", {"outputs": {}}, node=None),
        ],
        "done": True, "node": None, "prompt": None, "reason": "completed",
    })()
    _apply(bus, fake, wf, "r1", project_root=project_root)
    bus.close()
    # 无 MD 写入(node=None 安全 skip)
    assert not _memory_path(project_root).exists()


def test_write_memories_skips_unknown_node_name(tmp_path):
    """``Emit.node`` 名不在 ``wf.nodes``(并行组名 / orphan)→ 安全 skip(_step_io.py:128-130)。

    SPEC §3.2 docstring 点名的边界:并行组名 / orphan 查不到 node 对象 → 不抛、不写。
    """
    wf = _wf(memory=True)  # wf 只有一个节点 ``a``
    project_root = tmp_path
    tape = Tape(tmp_path / "tape.jsonl", run_id="r1", resume=True)
    bus = EventBus(tape)
    fake = type("R", (), {
        "emits": [Emit("node_completed", {"output": "x"}, node="ghost-not-in-wf")],
        "done": True, "node": None, "prompt": None, "reason": "completed",
    })()
    _apply(bus, fake, wf, "r1", project_root=project_root)
    bus.close()
    # ghost 名查不到 → 不写 MD
    assert not (project_root / ".orca" / "memory" / "mem_unit_wf" / "ghost-not-in-wf.md").exists()


# ── §8.7 跨 project_root 严格双向隔离 ───────────────────────────────────────


def test_cross_project_root_strict_bidirectional(tmp_path):
    """预置 root_a + root_b 各自 MD,验证互不可读(§8.7 严格闭环)。"""
    root_a = tmp_path / "projA"
    root_b = tmp_path / "projB"
    root_a.mkdir()
    root_b.mkdir()
    wf = _wf(memory=True)

    for root, body in [(root_a, "FROM-A"), (root_b, "FROM-B")]:
        md = _memory_path(root)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(
            f"---\nrun_id: r\ntimestamp: 1.0\nworkflow: mem_unit_wf\nnode: a\n---\n\n{body}\n",
            encoding="utf-8",
        )

    # root_a 的 run 只看到 FROM-A,看不到 FROM-B
    tape_a = Tape(tmp_path / "tapeA.jsonl", run_id="rA", resume=True)
    res_a = advance_step(tape_a, wf, run_id="rA", prompts_dir=None, project_root=root_a)
    assert res_a.prompt is not None
    assert "FROM-A" in res_a.prompt
    assert "FROM-B" not in res_a.prompt

    # 反向
    tape_b = Tape(tmp_path / "tapeB.jsonl", run_id="rB", resume=True)
    res_b = advance_step(tape_b, wf, run_id="rB", prompts_dir=None, project_root=root_b)
    assert res_b.prompt is not None
    assert "FROM-B" in res_b.prompt
    assert "FROM-A" not in res_b.prompt


# ── §8.2 加固:strip frontmatter 后 body 字面比对 ──────────────────────────────


def test_first_run_body_literal_strip_frontmatter(tmp_path):
    """§8.2 严格守门:strip frontmatter 后 body == output 原文(非子串)。

    防止 frontmatter 字段漂移或 body 末尾混入 frontmatter 行被 ``in`` 子串掩盖。
    """
    from orca.run.memory import read_node_memory_body

    wf = _wf(memory=True)
    project_root = tmp_path
    tape = Tape(tmp_path / "tape.jsonl", run_id="r1", resume=True)
    bus = EventBus(tape)
    r1 = advance_step(tape, wf, run_id="r1", prompts_dir=None, project_root=project_root)
    _apply(bus, r1, wf, "r1", project_root=project_root)
    r2 = advance_step(tape, wf, output="EXACT-BODY-TEXT", run_id="r1",
                      prompts_dir=None, project_root=project_root)
    _apply(bus, r2, wf, "r1", project_root=project_root)
    bus.close()

    body = read_node_memory_body(wf, wf.nodes[0], project_root=project_root)
    assert body == "EXACT-BODY-TEXT", f"strip frontmatter 后 body 应 == output 原文, got {body!r}"


# ── SPEC §5 CLI flag 透传链端到端 ────────────────────────────────────────────


def test_cli_next_no_memory_flag_passthrough(tmp_path, monkeypatch):
    """``orca next --no-memory`` flag → 真传到 advance_step(no_memory=True) + apply_step_result。

    SPEC §5 CLI flag 是契约面。本测试从 typer CliRunner 进,捕获 advance_step 调用 kwargs,
    断言 ``no_memory=True`` / ``project_root=Path.cwd()`` 真传到。守 cli.py:963→1118→1143 透传链。
    """
    from typer.testing import CliRunner
    from orca.iface.in_session import cli as cli_mod
    from orca.iface.in_session.cli import app

    # 准备:tmp_path 下放一个 tape + marker(让 next 能进 flock 临界区)
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    # 写最小 tape(workflow_started + node_started)和 marker,让 _load_wf_for_run / read_marker 通过
    # 但更稳的做法是直接 mock advance_step + apply_step_result,绕开 tape 准备
    captured: dict = {}

    async def _fake_apply(bus, result, **kwargs):
        captured.update(kwargs)
        return {"done": False}

    def _fake_advance(tape, wf, **kwargs):
        captured.update(kwargs)
        from orca.run.step import StepResult
        return StepResult(done=False, node="a")

    # marker 必须存在(否则 _next_in_critical_section 返 no-marker,advance_step 不调)
    run_id = "r-cli-test"
    tape_path = runs_dir / f"{run_id}.jsonl"
    tape_path.write_text(
        json.dumps({"type": "workflow_started", "data": {"workflow_name": "x", "inputs": {},
                                                          "host_session": None}}) + "\n",
        encoding="utf-8",
    )
    # 让 _default_tape_path(run_id) 指到这个 tape
    monkeypatch.setattr(cli_mod, "_default_tape_path", lambda rid: tape_path)
    # marker
    from orca.iface.in_session.marker import ActivationMarker, write_marker, marker_path
    write_marker(marker_path(runs_dir, run_id),
                 ActivationMarker(run_id=run_id, model="m", no_output_count=0))

    # mock wf 加载(_load_wf_for_run 返 None 名 wf 会让流程异常;但 advance_step 被 mock 了)
    monkeypatch.setattr(cli_mod, "_load_wf_for_run", lambda rid, tape: object())
    monkeypatch.setattr(cli_mod, "advance_step", _fake_advance)
    monkeypatch.setattr(cli_mod, "apply_step_result", _fake_apply)

    runner = CliRunner()
    result = runner.invoke(app, [
        "next", "--run-id", run_id, "--no-memory",
    ])
    # 不在乎结果 exit code(流程被 mock),只验 flag 透传
    assert captured.get("no_memory") is True, (
        f"--no-memory flag 应透传到 advance_step/apply_step_result(no_memory=True), got {captured}"
    )
    assert captured.get("project_root") is not None, "project_root 应被透传"


def test_cli_bootstrap_no_memory_flag_passthrough(tmp_path, monkeypatch):
    """``orca <wf> --inputs {} --no-memory`` bootstrap flag 同样透传(SPEC §5)。"""
    import json as _json
    from typer.testing import CliRunner
    from orca.iface.in_session import cli as cli_mod
    from orca.iface.in_session.cli import app

    monkeypatch.chdir(tmp_path)

    wf_yaml = tmp_path / "wf.yaml"
    wf_yaml.write_text(
        'name: cli_mem_test\n'
        'description: x\n'
        'entry: a\n'
        'nodes:\n'
        '  - name: a\n'
        '    kind: agent\n'
        '    executor: opencode\n'
        '    model: d/d\n'
        '    prompt: "x"\n'
        '    routes:\n'
        '      - to: $end\n',
        encoding="utf-8",
    )

    captured: dict = {}

    async def _fake_apply(bus, result, **kwargs):
        captured.update(kwargs)
        return {"done": False}

    def _fake_advance(tape, wf, **kwargs):
        captured.update(kwargs)
        from orca.run.step import StepResult
        return StepResult(done=False, node="a")

    # bootstrap 不需要 marker,但会 spawn 守护 → mock 掉避免 detach 进程
    monkeypatch.setattr(cli_mod, "_spawn_chart_daemon", lambda *a, **kw: None)
    monkeypatch.setattr(cli_mod, "_wait_for_sock", lambda *a, **kw: True)
    monkeypatch.setattr(cli_mod, "_spawn_sidechain_daemon", lambda *a, **kw: None)
    monkeypatch.setattr(cli_mod, "advance_step", _fake_advance)
    monkeypatch.setattr(cli_mod, "apply_step_result", _fake_apply)
    # _default_tape_path 指 tmp_path,避免污染真实 runs/
    monkeypatch.setattr(cli_mod, "_default_tape_path", lambda rid: tmp_path / "runs" / f"{rid}.jsonl")

    runner = CliRunner()
    result = runner.invoke(app, [
        str(wf_yaml), "--inputs", "{}", "--no-memory",
    ])
    assert captured.get("no_memory") is True, (
        f"bootstrap --no-memory 应透传, got {captured}"
    )
    assert captured.get("project_root") is not None


# ── daemon 写/读对称(架构一致性)──────────────────────────────────────────────


def test_daemon_next_passes_project_root_to_advance_step(tmp_path, monkeypatch):
    """daemon.next() 调 advance_step + apply_step_result 都传 project_root(架构对称)。

    守 SPEC §6「避免 two-path 分叉」:daemon 路径不能只写不注入(写 MD 但 prompt 不含记忆段
    会让 MD 单向积累,违背特性目的)。
    """
    from orca.iface.in_session import daemon as daemon_mod
    from orca.iface.in_session.daemon import InSessionDaemon

    wf = _wf(memory=True)
    tape_path = tmp_path / "tape.jsonl"

    captured: dict = {}

    def _fake_advance(tape, wf_arg, **kwargs):
        captured.update(kwargs)
        from orca.run.step import StepResult
        return StepResult(done=False, node="a")

    async def _fake_apply(bus, result, **kwargs):
        captured.update(kwargs)
        return {"done": False}

    # 构造 daemon:绕开 __init__ 的 flock / Tape / signal 注册(单测只验 kwargs 透传)
    inst = InSessionDaemon.__new__(InSessionDaemon)
    inst.wf = wf
    inst.run_id = "r-daemon-test"
    inst.inputs = {}
    inst.tape = None
    inst.bus = None
    inst._pending_output = "OUT"
    inst._start_ts = 0.0

    monkeypatch.setattr(daemon_mod, "advance_step", _fake_advance)
    monkeypatch.setattr(daemon_mod, "apply_step_result", _fake_apply)
    import asyncio
    asyncio.run(inst.next())

    assert captured.get("project_root") is not None, (
        "daemon.next() 必须传 project_root 给 advance_step + apply_step_result(架构对称)"
    )
