"""tests/iface/in_session/test_resolve_artifacts_dir.py —— SPEC 2026-08-06 §2.1 / §4 守门测试。

覆盖 ``_resolve_artifacts_dir`` 三条分支（Rule 9：测意图）：
  - workflow inputs 含**绝对** ``project_root`` + wf_name → project-scoped
    ``<proj>/artifacts/<wf>/``（跨 run 复用 + 多 wf 隔离）。
  - ``project_root`` 给了但**非绝对** → raise（fail loud，防跨 run 解析漂移）。
  - 无 ``project_root`` / 无 wf_name / tape 无 ws → per-run 回落（``runs/<run_id>/artifacts/``）。

辅助 ``_read_workflow_inputs`` 镜像 ``_read_workflow_name`` 的 tape 头扫描（同一
``workflow_started`` 事件），无 ws / 损坏 / 非 dict → ``{}``。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca.chart._paths import artifacts_dir_for_run
from orca.iface.in_session.cli import (
    _read_workflow_inputs,
    _read_workflow_name,
    _resolve_artifacts_dir,
    _TAPE_HEAD_SCAN_LIMIT,
)


# ── helpers ─────────────────────────────────────────────────────────────────


def _write_ws(tape_path: Path, wf_name: str | None, inputs: dict | None) -> None:
    """Raw-write a ``workflow_started`` first line + ensure clean file."""
    data: dict = {}
    if wf_name is not None:
        data["workflow_name"] = wf_name
    if inputs is not None:
        data["inputs"] = inputs
    payload = {
        "seq": 1,
        "type": "workflow_started",
        "timestamp": 1.0,
        "node": None,
        "session_id": None,
        "data": data,
    }
    tape_path.parent.mkdir(parents=True, exist_ok=True)
    tape_path.write_text(
        json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# ── _read_workflow_inputs ───────────────────────────────────────────────────


def test_read_workflow_inputs_returns_inputs_dict(tmp_path: Path):
    tape = tmp_path / "r.jsonl"
    _write_ws(tape, wf_name="nas-supernet", inputs={"project_root": "/abs/proj"})
    assert _read_workflow_inputs(tape) == {"project_root": "/abs/proj"}


def test_read_workflow_inputs_empty_when_no_inputs_key(tmp_path: Path):
    tape = tmp_path / "r.jsonl"
    _write_ws(tape, wf_name="nas-supernet", inputs=None)
    assert _read_workflow_inputs(tape) == {}


def test_read_workflow_inputs_empty_when_no_tape(tmp_path: Path):
    assert _read_workflow_inputs(tmp_path / "missing.jsonl") == {}


def test_read_workflow_inputs_empty_when_corrupt_head(tmp_path: Path):
    tape = tmp_path / "r.jsonl"
    tape.write_text("{not valid json\n", encoding="utf-8")
    assert _read_workflow_inputs(tape) == {}


def test_read_workflow_inputs_empty_when_no_workflow_started(tmp_path: Path):
    """扫满 ``_TAPE_HEAD_SCAN_LIMIT`` 行仍无 ws → {}（不读整个大文件）。"""
    tape = tmp_path / "r.jsonl"
    filler = [
        json.dumps({"seq": i, "type": "agent_message", "data": {}})
        for i in range(1, _TAPE_HEAD_SCAN_LIMIT + 2)
    ]
    tape.write_text("\n".join(filler) + "\n", encoding="utf-8")
    assert _read_workflow_inputs(tape) == {}


def test_read_workflow_inputs_returns_empty_when_inputs_not_dict(tmp_path: Path):
    """``data.inputs`` 非 dict（坏 schema）→ {}（保守回落，不崩）。"""
    tape = tmp_path / "r.jsonl"
    payload = {
        "seq": 1, "type": "workflow_started", "node": None, "session_id": None,
        "timestamp": 1.0,
        "data": {"workflow_name": "w", "inputs": ["not", "a", "dict"]},
    }
    tape.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    assert _read_workflow_inputs(tape) == {}


def test_read_workflow_inputs_consistent_with_read_workflow_name(tmp_path: Path):
    """同一 ws 事件两 helper 读到同一来源（spec §2.1 单一真相源）。"""
    tape = tmp_path / "r.jsonl"
    _write_ws(tape, wf_name="nas-supernet", inputs={"project_root": "/abs/proj", "seed": 0})
    assert _read_workflow_name(tape) == "nas-supernet"
    assert _read_workflow_inputs(tape) == {"project_root": "/abs/proj", "seed": 0}


# ── _resolve_artifacts_dir ──────────────────────────────────────────────────


def test_resolve_artifacts_dir_project_scoped_absolute(tmp_path: Path):
    """有 wf_name + 绝对 project_root → ``<proj>/artifacts/<wf>/``。"""
    tape = tmp_path / "runs" / "r.jsonl"
    _write_ws(tape, wf_name="nas-supernet", inputs={"project_root": "/abs/proj"})
    path, is_project_scoped = _resolve_artifacts_dir(tape, run_id="r-aaa")
    assert is_project_scoped is True
    assert path == (Path("/abs/proj") / "artifacts" / "nas-supernet").resolve()
    # 与 per-run runs/<run_id>/artifacts/ 完全不同位置
    assert path != artifacts_dir_for_run(tape.parent, "r-aaa").resolve()


def test_resolve_artifacts_dir_per_run_when_no_project_root(tmp_path: Path):
    """无 project_root → 既有 per-run ``runs/<run_id>/artifacts/``（向后兼容）。"""
    tape = tmp_path / "runs" / "r.jsonl"
    _write_ws(tape, wf_name="nas-supernet", inputs={"seed": 0})
    path, is_project_scoped = _resolve_artifacts_dir(tape, run_id="r-aaa")
    assert is_project_scoped is False
    assert path == artifacts_dir_for_run(tape.parent, "r-aaa").resolve()


def test_resolve_artifacts_dir_per_run_when_empty_project_root(tmp_path: Path):
    """project_root 空串 → per-run 回落（防 ``""`` 被当真值 ``proj and wf_name``）。"""
    tape = tmp_path / "runs" / "r.jsonl"
    _write_ws(tape, wf_name="nas-supernet", inputs={"project_root": ""})
    path, is_project_scoped = _resolve_artifacts_dir(tape, run_id="r-aaa")
    assert is_project_scoped is False
    assert path == artifacts_dir_for_run(tape.parent, "r-aaa").resolve()


def test_resolve_artifacts_dir_per_run_when_no_workflow_name(tmp_path: Path):
    """无 wf_name（tape 无 ws 或损坏）→ per-run 回落（即使有 project_root 也不 project-scoped）。"""
    tape = tmp_path / "runs" / "r.jsonl"
    _write_ws(tape, wf_name=None, inputs={"project_root": "/abs/proj"})
    path, is_project_scoped = _resolve_artifacts_dir(tape, run_id="r-aaa")
    assert is_project_scoped is False
    assert path == artifacts_dir_for_run(tape.parent, "r-aaa").resolve()


def test_resolve_artifacts_dir_per_run_when_no_inputs(tmp_path: Path):
    """inputs 缺（旧 schema 兼容）→ per-run 回落。"""
    tape = tmp_path / "runs" / "r.jsonl"
    _write_ws(tape, wf_name="quant-train", inputs=None)
    path, is_project_scoped = _resolve_artifacts_dir(tape, run_id="r-aaa")
    assert is_project_scoped is False
    assert path == artifacts_dir_for_run(tape.parent, "r-aaa").resolve()


def test_resolve_artifacts_dir_raises_on_relative_project_root(tmp_path: Path):
    """project_root 非绝对 → raise（fail loud，防相对路径跨 run 解析漂移）。"""
    tape = tmp_path / "runs" / "r.jsonl"
    _write_ws(tape, wf_name="nas-supernet", inputs={"project_root": "relative/proj"})
    with pytest.raises(ValueError, match=r"project_root 必须绝对路径"):
        _resolve_artifacts_dir(tape, run_id="r-aaa")


def test_resolve_artifacts_dir_isolates_workflows(tmp_path: Path):
    """同 project_root + 不同 wf_name → 不同 wf 子目录（多 wf 隔离契约）。"""
    proj = "/abs/proj"
    tape_a = tmp_path / "runs" / "a.jsonl"
    tape_b = tmp_path / "runs" / "b.jsonl"
    _write_ws(tape_a, wf_name="nas-supernet", inputs={"project_root": proj})
    _write_ws(tape_b, wf_name="puzzle", inputs={"project_root": proj})
    path_a, scoped_a = _resolve_artifacts_dir(tape_a, run_id="a-aaa")
    path_b, scoped_b = _resolve_artifacts_dir(tape_b, run_id="b-aaa")
    assert scoped_a and scoped_b
    assert path_a == (Path(proj) / "artifacts" / "nas-supernet").resolve()
    assert path_b == (Path(proj) / "artifacts" / "puzzle").resolve()
    assert path_a != path_b


def test_resolve_artifacts_dir_per_run_when_no_tape(tmp_path: Path):
    """tape 文件不存在（next 路径异常场景）→ per-run 回落，不 raise（调用方按无 ws 处理）。"""
    tape = tmp_path / "runs" / "missing.jsonl"
    path, is_project_scoped = _resolve_artifacts_dir(tape, run_id="r-aaa")
    assert is_project_scoped is False
    assert path == artifacts_dir_for_run(tape.parent, "r-aaa").resolve()
