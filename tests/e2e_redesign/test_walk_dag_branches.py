"""test_walk_dag_branches.py —— walk_dag 控制流分支单测（Rule 9）。

walk_dag 的真 workflow E2E（test_tars_harness_walk.py）只覆盖 happy path（单节点 done:true）+
多节点首跳。本文件用注入的 ``FakeOrcaCLI`` 钉死四个控制流分支，防回归：

1. ``reached_done=True``（done:true 无 error_kind）
2. ``error_kind`` 路径（done:true 但带 error_kind → reached_done 留 False、error 记录）
3. ``WalkLimitExceeded``（超 max_steps → error 记录、不 raise）
4. ``DAGStallError``（done=False 且无 next node → fail loud 冒出，不吞）

无需真 orca 引擎（纯控制流），快、确定性。
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.e2e_redesign.tars_harness import (
    DAGStallError,
    WalkLimitExceeded,
    walk_dag,
)
from tests.spike_ask_user.orca_cli import BootstrapResult, NextResult
from tests.spike_ask_user.orca_cli import OrcaCLIError


class FakeOrcaCLI:
    """脚本化的假 orca CLI：按预设的 next 响应序列推进，记录 stop 调用。

    - ``next_responses``：每次 ``next_step`` 返的 ``NextResult``（按序消费）。
    - ``next_side_effect``：若设，``next_step`` raise 之（模拟引擎非零退出）。
    - ``stopped``：记录 stop 被调的 run_id 列表（断言 cleanup 用）。
    """

    def __init__(
        self,
        *,
        next_responses: list[NextResult] | None = None,
        next_side_effect: Exception | None = None,
    ) -> None:
        self._next_responses = list(next_responses or [])
        self._next_side_effect = next_side_effect
        self.stopped: list[str] = []
        self._boot_run_id = "fake-run-id"

    def bootstrap(self, wf: str, inputs: dict[str, Any] | None) -> BootstrapResult:
        return BootstrapResult(
            run_id=self._boot_run_id, tape="", node="setup",
            prompt="【Orca 节点执行】fake", prompt_file="",
            done=False, raw={},
        )

    def next_step(self, run_id: str, output: str) -> NextResult:
        if self._next_side_effect is not None:
            raise self._next_side_effect
        if not self._next_responses:
            raise AssertionError("FakeOrcaCLI next_responses 用尽")
        return self._next_responses.pop(0)

    def stop(self, run_id: str) -> dict[str, Any]:
        self.stopped.append(run_id)
        return {"ok": True}


def _next(done: bool, *, node: str = "", error_kind: str = "",
          reason: str = "") -> NextResult:
    return NextResult(
        done=done, prompt="", node=node, busy=False, retry_after_ms=0,
        raw={"error_kind": error_kind, "reason": reason},
    )


def test_walk_dag_clean_done(monkeypatch) -> None:
    """分支 1：done:true 无 error_kind → reached_done=True、final.done=True。"""
    # walk_dag 会 load_parsed(workflow) 查 node schema——注入真 workflow（quant-ptq-sweep
    # 单节点）让 faker 合成合法 output。FakeOrcaCLI 控制引擎响应。
    cli = FakeOrcaCLI(next_responses=[_next(done=True)])
    result = walk_dag("quant-ptq-sweep", orca_cli=cli)
    assert result.reached_done is True
    assert result.final is not None and result.final.done is True
    assert result.error == ""
    assert cli.stopped == ["fake-run-id"], "cleanup stop 应被调"


def test_walk_dag_done_with_error_kind_not_reached_done(monkeypatch) -> None:
    """分支 2：done:true 但带 error_kind → reached_done 留 False、error 记录 error_kind。"""
    cli = FakeOrcaCLI(next_responses=[
        _next(done=True, error_kind="render_error", reason="渲染崩"),
    ])
    result = walk_dag("quant-ptq-sweep", orca_cli=cli)
    assert result.reached_done is False, "error_kind 时不应 reached_done"
    assert "render_error" in result.error
    assert result.final is not None and result.final.done is True  # 仍返 final（done=true）


def test_walk_dag_walk_limit_exceeded(monkeypatch) -> None:
    """分支 3：超 max_steps → error 记录、不 raise（多节点循环预期）。

    用 agent-struct-exploration（6 节点 + 循环）。FakeOrcaCLI 每次返「未 done、去下一节点」，
    max_steps=2 → 第 3 次 raise WalkLimitExceeded → catch → error 记录。
    """
    # 每次返 done=False, node=engineer（持续推进，永不 done）
    responses = [_next(done=False, node="engineer") for _ in range(10)]
    cli = FakeOrcaCLI(next_responses=responses)
    result = walk_dag("agent-struct-exploration", orca_cli=cli, max_steps=2)
    assert result.reached_done is False
    assert "max_steps" in result.error
    assert len(result.steps) == 2, "应走满 max_steps=2 步"


def test_walk_dag_dag_stall_error_raises_fail_loud(monkeypatch) -> None:
    """分支 4：done=False 且无 next node → DAGStallError 冒出（引擎不变式违反，不吞）。"""
    cli = FakeOrcaCLI(next_responses=[_next(done=False, node="")])  # 无 next node
    with pytest.raises(DAGStallError, match="引擎状态机 bug"):
        walk_dag("quant-ptq-sweep", orca_cli=cli)


def test_walk_dag_orca_cli_error_recorded_not_raised(monkeypatch) -> None:
    """分支 5：next_step raise OrcaCLIError（路由依赖真数据）→ error 记录、不 raise、cleanup 仍跑。"""
    cli = FakeOrcaCLI(next_side_effect=OrcaCLIError("route eval 失败"))
    result = walk_dag("quant-ptq-sweep", orca_cli=cli)
    assert result.reached_done is False
    assert "OrcaCLIError" in result.error
    # cleanup stop 仍应被调（finally 块）
    assert cli.stopped == ["fake-run-id"]


def test_walk_dag_cleanup_runs_even_on_stall(monkeypatch) -> None:
    """DAGStallError 冒出前，finally 仍清 marker（防残留）。"""
    cli = FakeOrcaCLI(next_responses=[_next(done=False, node="")])
    with pytest.raises(DAGStallError):
        walk_dag("quant-ptq-sweep", orca_cli=cli)
    assert cli.stopped == ["fake-run-id"], "DAGStallError 冒出前 finally 应清 marker"
