"""test_scan_meta_overview_contract.py —— AC14 contract test（SPEC §13.4 M-17）。

**契约**：``_scan_meta_overview`` 必须显式声明它对每个 EventType 的处理策略：
  - **overview-affecting**（``OVERVIEW_AFFECTING_EVENT_TYPES``）：进 full json.loads + fold，
    影响 ``agents/charts/cost_usd/run_status`` 之一。
  - **bulk**（``BULK_EVENT_TYPES``）：只计 seq/count，不 fold overview。

**守门意图**（SPEC §13 reviewer I-9 / AC14）：新增 ``EventType`` 必须显式归入其中一档，
否则本测试失败——防"新增事件类型忘了同步 scan 函数"的漏。

**自动派生**：``status-affecting subset = EventType 全集 − BULK_EVENT_TYPES``，
本测试断言它**等于** ``OVERVIEW_AFFECTING_EVENT_TYPES``（即"白名单之外都算 status-affecting"）。

同时断言两档不相交（同一 type 不能既是 overview-affecting 又是 bulk——明示意图）。
"""

from __future__ import annotations

from typing import get_args

import pytest

from orca.iface.web.run_manager import (
    BULK_EVENT_TYPES,
    OVERVIEW_AFFECTING_EVENT_TYPES,
    _scan_meta_overview,
)
from orca.schema.event import EventType


def _all_event_types() -> set[str]:
    """``EventType`` Literal 全集 → set[str]。"""
    return set(get_args(EventType))


def test_event_type_union_covers_all_status_affecting_subset():
    """AC14：每个 EventType 必须显式归入 overview-affecting 或 bulk 档（完备划分）。"""
    all_types = _all_event_types()
    declared = OVERVIEW_AFFECTING_EVENT_TYPES | BULK_EVENT_TYPES
    missing = all_types - declared
    assert not missing, (
        f"新增 EventType 未被 _scan_meta_overview 声明处理策略：{sorted(missing)}。"
        "请把它加入 ``OVERVIEW_AFFECTING_EVENT_TYPES``（若影响 agents/charts/cost_usd/"
        "run_status 之一）或 ``BULK_EVENT_TYPES``（仅计 count/seq），并在 _scan_meta_overview"
        " 的 if/elif 分支同步处理（前者）。"
    )


def test_bulk_and_overview_sets_disjoint():
    """两档不相交（明示意图：一个 type 不能同时是 overview-affecting 和 bulk）。"""
    overlap = OVERVIEW_AFFECTING_EVENT_TYPES & BULK_EVENT_TYPES
    assert not overlap, (
        f"EventType 同时归入 overview-affecting 与 bulk（意图不清）：{sorted(overlap)}"
    )


def test_status_affecting_derived_from_bulk_whitelist():
    """**自动派生**（SPEC §13.4 M-17 白名单语义）：

    ``status-affecting subset`` = ``EventType 全集`` − ``BULK_EVENT_TYPES``，
    必须**等于** ``OVERVIEW_AFFECTING_EVENT_TYPES``。
    即：白名单（bulk）之外的全部 EventType 都算 status-affecting，且 scan 函数显式 fold 之。
    """
    all_types = _all_event_types()
    derived_status_affecting = all_types - BULK_EVENT_TYPES
    assert derived_status_affecting == OVERVIEW_AFFECTING_EVENT_TYPES


def test_no_unknown_event_type_in_either_set():
    """两档中不能出现 EventType union 之外的幽灵 type（防 typo / 已删 EventType 残留）。"""
    all_types = _all_event_types()
    for t in OVERVIEW_AFFECTING_EVENT_TYPES | BULK_EVENT_TYPES:
        assert t in all_types, (
            f"声明的 type {t!r} 不在 orca.schema.EventType union 内（typo 或已删残留）"
        )


@pytest.mark.parametrize("event_type", sorted(OVERVIEW_AFFECTING_EVENT_TYPES))
def test_scan_meta_overview_handles_each_status_affecting_type(
    event_type, tmp_path
):
    """**覆盖性**（AC14 第二层）：每个 overview-affecting EventType 喂进 scan 函数，
    要么改变 overview 派生（agents/charts/cost_usd/run_status），要么至少进入 full-parse
    计数（不出 KeyError / 不被静默跳过）。

    用最小合法 JSON 行喂：workflow_started 携带 topology（建立 topo 上下文）；其它 type
    用最小 data。扫描后断言 count >= 预期 + overview 字段存在。
    """
    import json

    tape = tmp_path / "tape.jsonl"
    lines: list[str] = []

    # workflow_started 总是首行（建立 topology + wf_status=running）。
    lines.append(
        json.dumps(
            {
                "seq": 1,
                "type": "workflow_started",
                "node": None,
                "session_id": None,
                "timestamp": 0.0,
                "data": {
                    "inputs": {},
                    "node_count": 1,
                    "entry": "n1",
                    "workflow_name": "wf_contract",
                    "topology": {"nodes": [{"name": "n1"}]},
                },
            }
        )
    )

    if event_type != "workflow_started":
        payload = {
            "seq": 2,
            "type": event_type,
            "node": "n1" if event_type.startswith("node_") else None,
            "session_id": "s1" if event_type.startswith("agent_") else None,
            "timestamp": 1.0,
            "data": {},
        }
        if event_type == "node_completed":
            payload["data"] = {"elapsed": 0.1, "output": {}}
        elif event_type == "node_failed":
            payload["data"] = {"kind": "exec", "message": "x"}
        elif event_type == "node_skipped":
            payload["data"] = {"reason": "r"}
        elif event_type == "agent_usage":
            payload["data"] = {"cost_usd": 0.01}
        elif event_type == "custom":
            payload["data"] = {"kind": "chart", "chart": {"title": "c"}}
        elif event_type == "workflow_completed":
            payload["data"] = {"elapsed": 1.0, "outputs": {}}
        elif event_type == "workflow_failed":
            payload["data"] = {"kind": "exec", "message": "x"}
        elif event_type == "workflow_cancelled":
            payload["data"] = {"reason": "user"}
        lines.append(json.dumps(payload))

    tape.write_text("\n".join(lines) + "\n", encoding="utf-8")
    count, _, _, overview_data = _scan_meta_overview(tape)
    assert count >= 1
    assert overview_data is not None
    overview = overview_data["overview"]
    # 每个 overview-affecting type 至少要能算出四字段之一（不为默认空）。
    expected_nonempty = event_type != "workflow_started"  # wf_started 单独测下面
    if event_type == "workflow_completed":
        assert overview["run_status"] == "completed"
    elif event_type == "workflow_failed":
        assert overview["run_status"] == "failed"
    elif event_type == "workflow_cancelled":
        assert overview["run_status"] == "cancelled"
    elif event_type == "workflow_started":
        assert overview["run_status"] == "running"
        assert overview["agents"] == [{"name": "n1", "status": "pending"}]
    elif event_type == "node_completed":
        assert overview["agents"] == [{"name": "n1", "status": "done"}]
    elif event_type == "node_failed":
        assert overview["agents"] == [{"name": "n1", "status": "failed"}]
    elif event_type == "node_skipped":
        assert overview["agents"] == [{"name": "n1", "status": "skipped"}]
    elif event_type == "node_started":
        assert overview["agents"] == [{"name": "n1", "status": "running"}]
    elif event_type == "agent_usage":
        assert overview["cost_usd"] == pytest.approx(0.01)
    elif event_type == "custom":
        assert len(overview["charts"]) == 1
        if expected_nonempty:
            assert overview["charts"][0]["title"] == "c"
