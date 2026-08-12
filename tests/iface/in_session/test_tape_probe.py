"""tests/iface/in_session/test_tape_probe.py —— _tape_probe 纯只读终态扫描单测。

覆盖意图（非仅行为）：
  - scan_terminal：最末终态事件类型判定 + workflow_resumed 归零语义（SPEC 2026-08-11 §2.4）
  - 重复终态检测（同类 warn / 多类 raise）边界不受 resume 归零影响
  - resume 归零后同类重复不误触发 duplicate-terminal warn
"""

from __future__ import annotations

import json
from pathlib import Path

from orca.iface.in_session._tape_probe import scan_terminal


def _write_event(tape_path: Path, etype: str, seq: int, data: dict | None = None) -> None:
    """raw 写一行 JSONL 事件到 tape（测试专用，构造特定 tape 序列）。"""
    with open(tape_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "seq": seq, "type": etype, "timestamp": 0.0,
            "node": None, "session_id": None, "data": data or {},
        }) + "\n")


# ── 基础终态判定 ─────────────────────────────────────────────────────────────


def test_scan_terminal_no_terminal_returns_none(tmp_path):
    """无终态事件 → None（活跃 run）。"""
    tape = tmp_path / "run.jsonl"
    _write_event(tape, "workflow_started", 1)
    _write_event(tape, "node_started", 2)
    assert scan_terminal(tape) is None


def test_scan_terminal_completed_returns_type(tmp_path):
    """workflow_completed → 'workflow_completed'。"""
    tape = tmp_path / "run.jsonl"
    _write_event(tape, "workflow_started", 1)
    _write_event(tape, "workflow_completed", 2)
    assert scan_terminal(tape) == "workflow_completed"


def test_scan_terminal_failed_returns_type(tmp_path):
    """workflow_failed → 'workflow_failed'。"""
    tape = tmp_path / "run.jsonl"
    _write_event(tape, "workflow_started", 1)
    _write_event(tape, "workflow_failed", 2)
    assert scan_terminal(tape) == "workflow_failed"


# ── workflow_resumed 重新激活（SPEC 2026-08-11 §2.4）─────────────────────────


def test_scan_terminal_resumed_returns_none(tmp_path):
    """resumed tape [ws, wf_failed, wf_resumed, ns] → None（resume 重新激活 → 非终态）。

    意图（AC8/AC10 支持）：stop/status 据此判定 resumed run 仍可 stop / 仍活跃。
    若 scan_terminal 仍返 'workflow_failed'，stop 会短路 already-terminal exit 0，
    实际 run 在跑——违背诚实 reply。
    """
    tape = tmp_path / "run.jsonl"
    _write_event(tape, "workflow_started", 1)
    _write_event(tape, "workflow_failed", 2)
    _write_event(tape, "workflow_resumed", 3)
    _write_event(tape, "node_started", 4)
    assert scan_terminal(tape) is None


def test_scan_terminal_resume_then_complete_returns_type(tmp_path):
    """[wf_failed, wf_resumed, wf_completed] → 'workflow_completed'（resume 后真完成）。

    意图：resume 归零后，后续真终态重新计入 → 返 post-resume 的终态类型。
    """
    tape = tmp_path / "run.jsonl"
    _write_event(tape, "workflow_failed", 1)
    _write_event(tape, "workflow_resumed", 2)
    _write_event(tape, "workflow_completed", 3)
    assert scan_terminal(tape) == "workflow_completed"


def test_scan_terminal_resumed_does_not_trigger_duplicate_warn(tmp_path, caplog):
    """[wf_failed, wf_resumed, wf_failed] → 不触发 duplicate-terminal warn。

    意图：resume = 全新开始，历史终态不计。归零 terminal_count/types_seen 后只有 1 条
    post-resume wf_failed → 无 warn。若不归零，terminal_count=2 + 同类 → 误 warn。
    """
    tape = tmp_path / "run.jsonl"
    _write_event(tape, "workflow_failed", 1)
    _write_event(tape, "workflow_resumed", 2)
    _write_event(tape, "workflow_failed", 3)
    with caplog.at_level("WARNING", logger="orca.iface.in_session._tape_probe"):
        result = scan_terminal(tape)
    assert result == "workflow_failed"
    assert not any("duplicate-terminal" in r.getMessage() for r in caplog.records), (
        "resume 后的历史终态不应触发 duplicate-terminal warn"
    )


def test_scan_terminal_resumed_clears_contradiction(tmp_path):
    """[wf_completed, wf_resumed, wf_failed] → 不 raise TapeContradictionError。

    意图：resume 前有多类终态是历史，归零 types_seen 后只剩 post-resume 单类 → 不矛盾。
    若不归零，types_seen={completed, failed} → raise TapeContradictionError → stop 崩。
    """
    tape = tmp_path / "run.jsonl"
    _write_event(tape, "workflow_completed", 1)
    _write_event(tape, "workflow_resumed", 2)
    _write_event(tape, "workflow_failed", 3)
    # 不 raise（resume 归零 types_seen；post-resume 只 1 类）。
    assert scan_terminal(tape) == "workflow_failed"
