"""tests/run/test_route_output.py —— phase-14 Route.output 终点输出变换（SPEC §5 / §8.2 E2E-4/5）。

set node 驱动路由（确定性，不 spawn opencode），验证：
  - 命中 $end 的 route 带 output → final output 用 route.output（覆盖 wf.outputs）
  - 命中 $end 的 route 无 output → fallback wf.outputs
  - 不同 set 值 → 不同 route 命中 → 不同 final output（分类器语义）
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orca.compile.parser import load_workflow
from orca.events.tape import Tape
from orca.run import run_workflow


def _run(wf):
    return asyncio.run(run_workflow(wf, None))


def _write_wf(tmp_path: Path, yaml: str):
    p = tmp_path / "wf.yaml"
    p.write_text(yaml, encoding="utf-8")
    return load_workflow(p)


def _completed_outputs(state) -> dict:
    """从 tape 取 workflow_completed.data.outputs（与 test_demo_integration 同模式）。"""
    tape = Tape(Path("runs") / f"{state.run_id}.jsonl", run_id=state.run_id)
    for ev in tape.replay():
        if ev.type == "workflow_completed":
            return ev.data.get("outputs", {})
    return {}


def test_route_output_used_at_end_high(tmp_path: Path, monkeypatch):
    """flag=high → 第一条 route（带 output）命中 $end → final output = route.output。"""
    monkeypatch.chdir(tmp_path)
    wf = _write_wf(
        tmp_path,
        """
name: rt
entry: decide
nodes:
  - name: decide
    kind: set
    values:
      flag: "high"
    routes:
      - when: "decide.output.flag == 'high'"
        to: $end
        output:
          level: high
          src: "{{ decide.output.flag }}"
      - to: $end
        output:
          level: low
          src: "{{ decide.output.flag }}"
""",
    )
    state = _run(wf)
    assert state.status == "completed"
    assert _completed_outputs(state) == {"level": "high", "src": "high"}


def test_route_output_used_at_end_low(tmp_path: Path, monkeypatch):
    """flag=low → 兜底 route（带不同 output）命中 $end → final output = 兜底 route.output。"""
    monkeypatch.chdir(tmp_path)
    wf = _write_wf(
        tmp_path,
        """
name: rt
entry: decide
nodes:
  - name: decide
    kind: set
    values:
      flag: "low"
    routes:
      - when: "decide.output.flag == 'high'"
        to: $end
        output:
          level: high
      - to: $end
        output:
          level: low
""",
    )
    state = _run(wf)
    assert state.status == "completed"
    assert _completed_outputs(state) == {"level": "low"}


def test_route_output_fallback_to_wf_outputs(tmp_path: Path, monkeypatch):
    """命中 $end 的 route 无 output → final output 走 wf.outputs fallback（SPEC §0.1 #5）。"""
    monkeypatch.chdir(tmp_path)
    wf = _write_wf(
        tmp_path,
        """
name: rt
entry: decide
nodes:
  - name: decide
    kind: set
    values:
      flag: "x"
    routes:
      - to: $end
outputs:
  result: "{{ decide.output.flag }}"
""",
    )
    state = _run(wf)
    assert state.status == "completed"
    assert _completed_outputs(state) == {"result": "x"}


def test_route_output_dead_code_warn(tmp_path: Path, monkeypatch):
    """route.to 非 $end 且带 output → validate_workflow warnings 含死代码提示（SPEC §7.3）。

    注：``ResultWarning`` 走 ``ValidationResult.add_warning``（与孤立 node 等既有 warn 同通道），
    展示靠广义 warnings 通道修复（C1 余项，phase-14 scope 外）；本测试直接调
    ``validate_workflow`` 取 warnings list，验证 validator 收集逻辑。
    """
    from orca.compile.validator import validate_workflow

    monkeypatch.chdir(tmp_path)
    wf = _write_wf(
        tmp_path,
        """
name: rt
entry: a
nodes:
  - name: a
    kind: set
    values:
      x: "1"
    routes:
      - to: b
        output:
          dead: "code"
  - name: b
    kind: set
    values:
      y: "2"
    routes:
      - to: $end
""",
    )
    wf_warnings = validate_workflow(wf)  # list[str]（无 error 时返回 warnings）
    assert any("死代码" in w or "output" in w.lower() for w in wf_warnings), wf_warnings
