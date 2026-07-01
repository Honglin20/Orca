"""tests/exec/test_contract.py —— 契约层结构/import/ABC/错误映射/依赖单向。

覆盖 SPEC §7.1 / §7.2 + 开发计划 A.6：
  - public API 可 import（Executor / make_executor / RunContext / ExecError）
  - Executor 是 ABC（不能直接实例化）
  - RunContext frozen（mutation 抛 FrozenInstanceError）
  - ExecError 字段（phase / error_type / message）+ phase_to_error_type 全映射覆盖
  - 依赖单向铁律 1：exec/ 不 import orca.run / orca.compile
  - 依赖单向铁律 2：exec/ 不 import orca.events.bus / Tape（只允许 orca.schema.Event）
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from orca.exec import ExecError, Executor, RunContext, make_executor, phase_to_error_type
from orca.schema import AgentNode, ForeachNode, ScriptNode, SetNode

EXEC_DIR = Path(__file__).resolve().parents[2] / "orca" / "exec"


# ── public API import ────────────────────────────────────────────────────────


def test_public_api_imports():
    """SPEC §7.1：核心符号可从 orca.exec 顶层 import。"""
    from orca.exec import (  # noqa: F401
        ClaudeExecutor,
        ExecError,
        Executor,
        RunContext,
        ScriptExecutor,
        SetExecutor,
        make_executor,
    )
    # 惰性符号也经 __getattr__ 可解析
    assert ClaudeExecutor.__name__ == "ClaudeExecutor"
    assert ScriptExecutor.__name__ == "ScriptExecutor"
    assert SetExecutor.__name__ == "SetExecutor"


# ── Executor ABC ──────────────────────────────────────────────────────────────


def test_executor_is_abc_cannot_instantiate():
    """SPEC §7.2：Executor 是 ABC，直接实例化抛 TypeError。"""
    with pytest.raises(TypeError, match="abstract"):
        Executor()  # type: ignore[abstract]


def test_executor_subclass_must_implement_exec():
    """子类不实现 exec 仍是抽象的。"""

    class _Incomplete(Executor):  # type: ignore[misc]
        pass

    with pytest.raises(TypeError):
        _Incomplete()  # type: ignore[abstract]


# ── RunContext frozen ─────────────────────────────────────────────────────────


def test_run_context_construct():
    ctx = RunContext(inputs={"x": 1}, outputs={}, run_id="r1")
    assert ctx.inputs == {"x": 1}
    assert ctx.outputs == {}
    assert ctx.run_id == "r1"


def test_run_context_is_frozen():
    """frozen dataclass：字段重新赋值抛 FrozenInstanceError（SPEC §4.7）。

    注意：Python ``frozen=True`` 只阻止属性重新绑定，不阻止可变容器内部变异
    （这是语言既定语义，非本层职责）。此处只断言绑定级冻结。
    """
    ctx = RunContext(inputs={"x": 1}, outputs={}, run_id="r1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.run_id = "other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.inputs = {"y": 2}  # type: ignore[misc]


# ── ExecError + phase_to_error_type ──────────────────────────────────────────


def test_exec_error_fields_default_error_type():
    e = ExecError(phase="timeout", message="超时")
    assert e.phase == "timeout"
    assert e.error_type == "ExecTimeout"
    assert e.message == "超时"
    assert "timeout" in str(e)


def test_exec_error_explicit_error_type_override():
    """stream phase 可显式覆盖 error_type（附 api_error_status 等额外诊断）。"""
    e = ExecError(phase="stream", message="claude 报错", error_type="ClaudeApiError")
    assert e.error_type == "ClaudeApiError"


@pytest.mark.parametrize(
    "phase,expected",
    [
        ("timeout", "ExecTimeout"),
        ("spawn", "CliExitNonZero"),
        ("stream", "ClaudeStreamError"),
        ("result_parse", "NoResultEvent"),
        ("schema", "SchemaValidationError"),
        ("render", "RenderError"),
        # phase 11 §9.7.5（Wait Node）：duration 超上限走 config phase
        ("config", "ConfigError"),
    ],
)
def test_phase_to_error_type_all_mappings(phase, expected):
    """SPEC §6：每个已登记 phase 各映射到固定 error_type（新增 phase 同步补此表 + 映射表）。"""
    assert phase_to_error_type(phase) == expected


def test_phase_to_error_type_unknown_raises():
    """未知 phase fail loud（映射表漏补是 bug）。"""
    with pytest.raises(ValueError, match="未知 error phase"):
        phase_to_error_type("bogus")


# ── make_executor 分派（factory 真实现，覆盖 SPEC §7.8 的 fail-loud 边界） ─────


def test_make_executor_foreach_raises_not_implemented():
    """ForeachNode 归 phase 5 编排（SPEC §7.8 / §5 边界）。"""
    node = ForeachNode(name="fe", source="x.body", body=AgentNode(name="b"))
    with pytest.raises(NotImplementedError, match="phase 5"):
        make_executor(node)


def test_make_executor_unknown_kind_raises_typeerror():
    """非 4 种合法 kind（schema 层漏校验的 bug 场景）→ factory fail loud。

    node.kind 是 Literal 联合（4 选 1），pydantic 层已杜绝非法 kind；此处用一个
    非 Node 的对象模拟「上层 bug 透传到 factory」，验证 fallback 兜底 fail loud。
    """
    class _FakeNode:  # 非 Node 子类，不匹配任何 isinstance 分支
        kind = "bogus"

    with pytest.raises(TypeError, match="不支持 node kind"):
        make_executor(_FakeNode())  # type: ignore[arg-type]


# ── 依赖单向铁律（grep 静态判据，SPEC §7.0 铁律 1 / 2）────────────────────────


def _walk_py(root: Path):
    for p in root.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        yield p


def test_dependency_no_run_no_compile():
    """铁律 1：exec/ 不 import orca.run / orca.compile。"""
    banned = ("from orca.run", "import orca.run", "from orca.compile", "import orca.compile")
    hits = []
    for p in _walk_py(EXEC_DIR):
        text = p.read_text(encoding="utf-8")
        for b in banned:
            if b in text:
                hits.append(f"{p.relative_to(EXEC_DIR.parent.parent)}: {b}")
    assert not hits, f"exec/ 反向依赖 run/compile：\n{chr(10).join(hits)}"


def test_dependency_no_events_bus_no_tape():
    """铁律 2：exec/ 不写 tape / 不 import events.bus / Tape（SPEC §7.0）。

    executor 产出 ``AsyncIterator[Event]``，写 tape + bus.emit 归 phase 5 orchestrator。
    允许 ``from orca.schema import Event``（类型）—— 那是消费 Event 数据结构，非写真相源。
    """
    banned = (
        "from orca.events.bus",
        "import orca.events.bus",
        "from orca.events.tape",
        "import orca.events.tape",
        "EventBus",
        "Tape(",
    )
    hits = []
    for p in _walk_py(EXEC_DIR):
        text = p.read_text(encoding="utf-8")
        for b in banned:
            if b in text:
                hits.append(f"{p.relative_to(EXEC_DIR.parent.parent)}: {b}")
    assert not hits, f"exec/ 写 tape / 持 bus（违反铁律 2）：\n{chr(10).join(hits)}"
