"""tests/schema/test_state.py —— RunState / UsageSummary / Status 构造。

覆盖 SPEC §7.5：RunState/UsageSummary 构造（含递归 node_breakdown）。
重点验证 Status（node 级，含 done）与 RunState.status（workflow 级，含 completed）的有意区分。
"""

import typing

import pytest
from pydantic import ValidationError

from orca.schema import RunState, Status, UsageSummary


def test_runstate_defaults():
    rs = RunState(run_id="r1", workflow_name="nas")
    assert rs.status == "pending"
    assert rs.current_node is None
    assert rs.node_status == {}
    assert rs.context == {}
    assert rs.usage is None


def test_runstate_full():
    rs = RunState(
        run_id="r1",
        workflow_name="nas",
        status="running",
        current_node="trainer",
        node_status={"trainer": "running", "optimizer": "done"},
        context={"optimizer": {"structure": "cnn"}},
        usage=UsageSummary(input_tokens=100, output_tokens=50, cost_usd=0.01),
    )
    assert rs.status == "running"
    assert rs.node_status["optimizer"] == "done"
    assert rs.usage.cost_usd == pytest.approx(0.01)


def test_usage_summary_defaults():
    u = UsageSummary()
    assert u.input_tokens == 0
    assert u.output_tokens == 0
    assert u.cache_tokens == 0
    assert u.cost_usd == 0.0
    assert u.node_breakdown == {}


def test_usage_summary_recursive():
    """SPEC §9.5：node_breakdown 递归自引用。"""
    inner = UsageSummary(input_tokens=10, output_tokens=5)
    leaf = UsageSummary(input_tokens=1)
    outer = UsageSummary(
        input_tokens=100,
        node_breakdown={"trainer": inner, "evaluator": leaf},
    )
    assert outer.node_breakdown["trainer"].input_tokens == 10
    assert outer.node_breakdown["evaluator"].output_tokens == 0
    # 两层嵌套
    outer.node_breakdown["trainer"].node_breakdown["sub"] = UsageSummary(input_tokens=2)
    assert outer.node_breakdown["trainer"].node_breakdown["sub"].input_tokens == 2


def test_usage_summary_recursive_from_dict():
    """从嵌套 dict 构造，真正走 pydantic 递归校验（非构造后手动 mutate）。"""
    u = UsageSummary(
        node_breakdown={"a": {"node_breakdown": {"b": {"input_tokens": 1}}}}
    )
    assert isinstance(u.node_breakdown["a"], UsageSummary)
    assert isinstance(u.node_breakdown["a"].node_breakdown["b"], UsageSummary)
    assert u.node_breakdown["a"].node_breakdown["b"].input_tokens == 1


def test_usage_summary_extra_forbid():
    with pytest.raises(ValidationError):
        UsageSummary(bogus=1)


def test_status_literal_values():
    """Status 是 node 级状态：含 done（非 completed）。"""
    assert set(typing.get_args(Status)) == {
        "pending",
        "running",
        "done",
        "failed",
        "skipped",
    }


def test_node_status_rejects_invalid():
    with pytest.raises(ValidationError):
        RunState(run_id="r", workflow_name="w", node_status={"x": "not_a_status"})


def test_node_status_rejects_completed():
    """反向覆盖：Status（node 级）用 done，绝不能接受 completed（那是 workflow 级）。

    防止后人误把 completed 加进 Status Literal。
    """
    with pytest.raises(ValidationError):
        RunState(run_id="r", workflow_name="w", node_status={"x": "completed"})


def test_runstate_status_uses_completed_not_done():
    """SPEC §4.2 有意区分：workflow 级 status 用 completed，node 级用 done。"""
    rs = RunState(run_id="r", workflow_name="w", status="completed")
    assert rs.status == "completed"
    with pytest.raises(ValidationError):
        RunState(run_id="r", workflow_name="w", status="done")
