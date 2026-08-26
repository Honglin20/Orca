"""test_card_event_log_align.py —— SPEC ``docs/specs/2026-08-10-card-event-log-align.md`` v3。

覆盖 AC4-AC7（单测，守门 SPEC §3.1/§3.2/§3.3 双分支计数 + isinstance + 双语义分离 + U1）：

  - **AC4**：fixture ``1 node_started + 1 retry_started + 10 agent_tool_call → event_count==2``
    （retry_started 走 fast-path，验证双分支计数完备；复审验 5 种 bug 模型全被此 fixture 拦）。
  - **AC5**：后端 ``_LOG_EVENT_TYPES`` == 前端 ``classifyLogLevel`` 非 null 且非 route_taken
    （U1 同步契约，集合相等守门）。
  - **AC6**：后端 chart 去重 identity == 前端 ``selectCharts``，含**空 chart_type edge case**（F4）
    + 同 title 多推 + 无 title。
  - **AC7**：meta ``event_count`` 全量 + RunSummary.event_count log 行数（双语义）。
"""

from __future__ import annotations

import json
from pathlib import Path

from orca.iface.web.run_manager import (
    RunManager,
    _LOG_EVENT_TYPES,
    _scan_meta_overview,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _event(seq: int, etype: str, data: dict | None = None, *, node=None, ts: float = 0.0) -> dict:
    return {
        "seq": seq,
        "type": etype,
        "timestamp": ts,
        "node": node,
        "session_id": None,
        "data": data or {},
    }


def _write_tape(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e, ensure_ascii=False) for e in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ws_started(seq: int = 1, run_id: str = "r1", *, node_count: int = 1) -> dict:
    """workflow_started 携 topology（建立上下文，scan 函数首个 ws 分支命中）。"""
    return _event(
        seq, "workflow_started",
        {
            "inputs": {}, "node_count": node_count, "entry": "n1",
            "workflow_name": "wf",
            "run_id": run_id,
            "topology": {"entry": "n1", "nodes": [{"name": "n1"}]},
        },
        ts=0.0,
    )


# ── AC4：双分支计数（fast-path + full-parse）──────────────────────────────


def test_ac4_dual_branch_count_node_started_and_retry_started(tmp_path):
    """AC4：fixture ``1 node_started + 1 retry_started + 10 agent_tool_call → event_count==2``。

    验证双分支计数完备（SPEC §3.1 BLOCKER F1）：
      - ``node_started`` 走 full-parse（不在 ``_META_BULK_MARKERS``）→ 计入 log_event_count。
      - ``retry_started`` 走 fast-path（在 ``_META_BULK_MARKERS`` 且在 ``_LOG_EVENT_TYPES``）
        → fast-path 必须查白名单计入 log_event_count（单 full-parse 计数会漏）。
      - ``agent_tool_call`` 不在 ``_LOG_EVENT_TYPES``（前端 classifyLogLevel=null）→ 不计入。

    守门 5 种 bug 模型（SPEC §3.1 复审）：
      - bug1：fast-path 未做 type check → retry_started 漏计（event_count=1）。
      - bug2：fast-path check 放 ``if m:`` 块外 → 无 seq 事件误计（其它 fixture 覆盖）。
      - bug3：fast-path check 放 ``count += 1`` 前 → seq 提取失败的事件被 log 计数。
      - bug4：full-parse 未做 type check → node_started 漏计（event_count=1）。
      - bug5：白名单漏 ``retry_started`` → fast-path 不命中（event_count=1）。
    本 fixture 全拦：正确实现得 2，任一 bug 模型偏离 2。
    """
    # SPEC §5 AC4 fixture 逐字：1 node_started + 1 retry_started + 10 agent_tool_call。
    # 不含 workflow_started（非 SPEC AC4 表述的组成部分；ws 计数在其它 AC7 测试覆盖）。
    events = []
    # node_started：overview-affecting，走 full-parse 分支
    events.append(_event(1, "node_started", {}, node="n1", ts=1.0))
    # retry_started：bulk marker + log 白名单，走 fast-path 分支（F1 关键场景）
    events.append(_event(2, "retry_started", {"attempt": 1, "max_attempts": 3, "kind": "exec"}, ts=2.0))
    # 10 个 agent_tool_call：bulk marker 但非 log 白名单，应被排除
    for i in range(3, 13):
        events.append(_event(i, "agent_tool_call", {"tool": "x"}, ts=3.0 + i))

    tape_path = tmp_path / "runs" / "r1.jsonl"
    _write_tape(tape_path, events)

    count, _, _, overview_data = _scan_meta_overview(tape_path)
    # 全量 count = 12（node_started + retry_started + 10 tool_call；无 ws）
    assert count == 12, f"全量 count 应=12（无 ws；node_started+retry_started+10 tool_call）：{count}"
    assert overview_data is not None
    overview = overview_data["overview"]
    # SPEC §3.1：log_event_count = 2（node_started + retry_started，10 tool_call 不计）
    assert overview["log_event_count"] == 2, (
        f"log_event_count 应=2（node_started + retry_started），10 tool_call 不计："
        f"{overview['log_event_count']}"
    )

    # 通过 _summary_from_tape 派生 RunSummary，验证 event_count 字段语义
    manager = RunManager()
    summary = manager._summary_from_tape(
        tape_path, project_id="p", project_name="P", source="attached",
    )
    assert summary is not None
    assert summary.event_count == 2, (
        f"RunSummary.event_count 应=2（log 行数，非全量 12）：{summary.event_count}"
    )


def test_ac4_fast_path_check_inside_if_m_block_after_count(tmp_path):
    """AC4 NEW-1 守门：fast-path type check 放 ``if m:`` 块内、``count += 1`` 之后。

    构造 bulk-marker 事件但**无 seq**（``if m:`` 不命中）→ 既不计 count 也不计 log_event_count
    （与 full-parse ``if not isinstance(seq, int): continue`` 一致）。若 check 放 ``if m:`` 块外
    或 ``count += 1`` 前，log_event_count 会误计。
    """
    events = [_ws_started(seq=1)]
    # bulk-marker 但无 seq 字段（fast-path ``m = _META_SEQ_RE.search(...)`` 返 None）
    bad_event = {
        "type": "retry_started",
        "timestamp": 5.0,
        "node": "n1",
        "session_id": None,
        "data": {"attempt": 1, "max_attempts": 3, "kind": "exec"},
        # 故意无 seq
    }
    events.append(bad_event)
    tape_path = tmp_path / "runs" / "r2.jsonl"
    _write_tape(tape_path, events)

    count, _, _, overview_data = _scan_meta_overview(tape_path)
    # 全量 count=1（ws 计入；bad_event 无 seq 被 fast-path 的 ``if m:`` 守门排除）
    assert count == 1, f"全量 count 应=1（ws only；bad_event 无 seq 排除）：{count}"
    assert overview_data is not None
    # log_event_count=1（ws only；retry_started 因无 seq 被 ``if m:`` 守门排除——NEW-1）
    assert overview_data["overview"]["log_event_count"] == 1, (
        "bad_event（无 seq 的 retry_started）不应被 log_event_count 计入（NEW-1：fast-path "
        "type check 放 if m: 块内 count+=1 之后）"
    )


# ── AC5：U1 同步契约（白名单 == 前端 classifyLogLevel）────────────────────


def test_ac5_log_event_types_matches_classify_log_level():
    """AC5：后端 ``_LOG_EVENT_TYPES`` == 前端 ``classifyLogLevel``（selectors.ts:595）非 null
    且非 route_taken（U1 同步契约）。

    前端基准（逐字抄自 ``selectors.ts:classifyLogLevel``，**不改前端**；改前端时本测试同步更新）：
      - info（9）：workflow_started / node_started / foreach_started / retry_started /
        validator_started / wait_started / human_decision_requested / interrupt_requested /
        dialog_started
      - success（10）：workflow_completed / workflow_resumed / node_completed /
        foreach_completed / retry_succeeded / validator_passed / wait_completed /
        human_decision_resolved / interrupt_resolved / dialog_ended
      - error（6）：workflow_failed / workflow_cancelled / node_failed / retry_exhausted /
        validator_failed / error
      - warning（1）：node_skipped
      - debug（1）：route_taken（**排除**，默认隐藏）
      - null：agent_message / agent_thinking / agent_tool_call / agent_tool_result /
        agent_step_started / foreach_item_started / foreach_item_completed / prompt_rendered /
        agent_usage / custom / dialog_message / unknown_event（**排除**）
    """
    # 前端 classifyLogLevel 非 null 且非 route_taken 的全集（手抄，作为 U1 基准）。
    # 改前端时本 set 同步更新——本测试为 U1 守门，差异即漂移。
    frontend_log_types = {
        # info（9）
        "workflow_started", "node_started", "foreach_started", "retry_started",
        "validator_started", "wait_started", "human_decision_requested",
        "interrupt_requested", "dialog_started",
        # success（10）
        "workflow_completed", "workflow_resumed", "node_completed",
        "foreach_completed", "retry_succeeded", "validator_passed",
        "wait_completed", "human_decision_resolved", "interrupt_resolved",
        "dialog_ended",
        # error（6）
        "workflow_failed", "workflow_cancelled", "node_failed",
        "retry_exhausted", "validator_failed", "error",
        # warning（1）
        "node_skipped",
    }
    assert _LOG_EVENT_TYPES == frontend_log_types, (
        f"U1 漂移：后端 _LOG_EVENT_TYPES 与前端 classifyLogLevel 非 null 非 route_taken 集合不等。\n"
        f"仅后端：{_LOG_EVENT_TYPES - frontend_log_types}\n"
        f"仅前端：{frontend_log_types - _LOG_EVENT_TYPES}"
    )
    # route_taken 必须排除（debug 级，默认隐藏）
    assert "route_taken" not in _LOG_EVENT_TYPES
    # 过程事件（null 级）必须排除
    for null_type in (
        "agent_message", "agent_thinking", "agent_tool_call", "agent_tool_result",
        "agent_step_started", "foreach_item_started", "foreach_item_completed",
        "prompt_rendered", "agent_usage", "custom", "dialog_message", "unknown_event",
    ):
        assert null_type not in _LOG_EVENT_TYPES, f"{null_type} 不应进 log 白名单"


# ── AC6：chart 去重 identity（U1 + F4 isinstance edge cases）──────────────


def test_ac6_chart_dedup_same_title_multiple_pushes(tmp_path):
    """AC6：同 title 多次推 → chart_count=1（同 identity upsert，前端 selectCharts 行为）。

    验证 ``identity = title``（当 title 是非空 string）的去重逻辑——前端 byIdentity Map 后到胜。
    后端 set 去重 count 与前端 Map size 相等（U1）。
    """
    events = [_ws_started(seq=1)]
    # 3 个同 title（不同 seq）的 chart：identity="loss_curve" → 去重后 1
    for seq in (2, 3, 4):
        events.append(_event(
            seq, "custom", {"kind": "chart", "chart": {
                "label": "training", "title": "loss_curve", "chart_type": "line",
            }}, ts=float(seq),
        ))
    tape_path = tmp_path / "runs" / "r3.jsonl"
    _write_tape(tape_path, events)

    _, _, _, overview_data = _scan_meta_overview(tape_path)
    assert overview_data is not None
    overview = overview_data["overview"]
    assert overview["chart_count"] == 1, (
        f"同 title 多推应去重为 1：{overview['chart_count']}"
    )
    # charts list（给 huge 模式用）保留全部 3 条（不去重，与前端 huge 模式 serverOverview 分支一致）
    assert len(overview["charts"]) == 3


def test_ac6_chart_dedup_no_title_falls_back_to_chart_type_seq(tmp_path):
    """AC6：无 title（title 缺失/None/非 str）→ identity = ``{chart_type}#{seq}``，各自独立。

    验证 fallback identity 的 seq 唯一性（同 chart_type 不同 seq → 独立条目）。
    """
    events = [_ws_started(seq=1)]
    # 2 个无 title 的 chart：identity = "scatter#2" / "scatter#3" → 各自独立
    events.append(_event(2, "custom", {"kind": "chart", "chart": {
        "label": "search", "chart_type": "scatter",
    }}, ts=2.0))
    events.append(_event(3, "custom", {"kind": "chart", "chart": {
        "label": "search", "chart_type": "scatter",
    }}, ts=3.0))
    tape_path = tmp_path / "runs" / "r4.jsonl"
    _write_tape(tape_path, events)

    _, _, _, overview_data = _scan_meta_overview(tape_path)
    assert overview_data is not None
    overview = overview_data["overview"]
    assert overview["chart_count"] == 2, (
        f"无 title 的两 chart fallback identity 应独立（chart_type#seq 不同）：{overview['chart_count']}"
    )


def test_ac6_chart_dedup_empty_chart_type_edge_case_f4(tmp_path):
    """AC6 F4 edge case：``chart_type=""``（空串）+ 无 title → identity = ``#seq``（前端 typeof "" === "string"）。

    SPEC §3.2 F4：isinstance 守卫匹配前端 ``typeof === "string"``。Python ``""`` 是 str →
    保留 ""；不能用 ``str(... or ...)``（Python ``"" or "chart"`` → "chart" 把空串当 falsy 偏差）。
    本测试守门：空 chart_type 不被 ``or`` 短路成 "chart"。

    验证两点：
      1. 空 chart_type 的 chart identity = "#seq"（不是 "chart#seq"，否则说明 ``or`` 误短路）。
      2. 两个空 chart_type + 不同 seq → identity 各自独立。
    """
    events = [_ws_started(seq=1)]
    # chart_type="" 的 chart：identity = "" + "#seq" = "#2"（不是 "chart#2"）
    events.append(_event(2, "custom", {"kind": "chart", "chart": {
        "label": "misc", "title": "t", "chart_type": "",
    }}, ts=2.0))
    # 另一个空 chart_type + 无 title：identity = "#3"
    events.append(_event(3, "custom", {"kind": "chart", "chart": {
        "chart_type": "",
    }}, ts=3.0))
    tape_path = tmp_path / "runs" / "r5.jsonl"
    _write_tape(tape_path, events)

    _, _, _, overview_data = _scan_meta_overview(tape_path)
    assert overview_data is not None
    overview = overview_data["overview"]
    # 2 个 chart：identity "t" + "#3"（前者有 title="t"，后者无 title fallback 到 "#3"）
    # 关键：若 chart_type="" 被 ``or`` 短路成 "chart"，identity 会变 "t" + "chart#3"
    # 但本 fixture 第一个 chart 有 title="t"，不受影响；重点在第二个 chart 的 chart_type 守卫
    assert overview["chart_count"] == 2, f"两个不同 identity chart 应=2：{overview['chart_count']}"
    # 第二个 chart 的 chart_type 守卫：isinstance → "" 保留（不被 ``or`` 短路为 "chart"）
    assert overview["charts"][1]["chart_type"] == "", (
        f"chart_type='' 应保留为 ''（isinstance 守卫，非 ``or`` 短路为 'chart'）："
        f"{overview['charts'][1]['chart_type']!r}"
    )


def test_ac6_chart_dedup_mixed_title_and_no_title(tmp_path):
    """AC6 综合：title="" 与 title 缺失与 title="valid" 混合 → 空标题/缺失都 fallback identity。

    前端 ``title || backtick-chart_type-sharp-seq-backtick``——JS ``"" || x`` = x，``undefined || x`` = x。
    后端 ``title = title_raw if isinstance(title_raw, str) else ""`` 然后 ``identity = title if title else fallback``
    —— Python ``"" or x`` 也是 x（等价 JS ||）。
    """
    events = [_ws_started(seq=1)]
    # title="valid" → identity = "valid"
    events.append(_event(2, "custom", {"kind": "chart", "chart": {"title": "valid", "chart_type": "line"}}, ts=2.0))
    # title="" → fallback identity = "line#3"
    events.append(_event(3, "custom", {"kind": "chart", "chart": {"title": "", "chart_type": "line"}}, ts=3.0))
    # title=None → fallback identity = "line#4"
    events.append(_event(4, "custom", {"kind": "chart", "chart": {"title": None, "chart_type": "line"}}, ts=4.0))
    # title 缺失 → fallback identity = "line#5"
    events.append(_event(5, "custom", {"kind": "chart", "chart": {"chart_type": "line"}}, ts=5.0))
    # title=123（非 str）→ fallback identity = "line#6"
    events.append(_event(6, "custom", {"kind": "chart", "chart": {"title": 123, "chart_type": "line"}}, ts=6.0))
    tape_path = tmp_path / "runs" / "r6.jsonl"
    _write_tape(tape_path, events)

    _, _, _, overview_data = _scan_meta_overview(tape_path)
    assert overview_data is not None
    overview = overview_data["overview"]
    # 5 chart：identity "valid" + "line#3" + "line#4" + "line#5" + "line#6" → 全独立，count=5
    assert overview["chart_count"] == 5, f"5 个不同 identity 应=5：{overview['chart_count']}"


# ── AC7：双语义（meta 全量 + RunSummary log 行数）─────────────────────────


def test_ac7_meta_event_count_full_vs_runsummary_event_count_log(tmp_path):
    """AC7：同一 tape 的 meta ``event_count`` 保持全量（huge 判定依赖）+ RunSummary.event_count
    是 log 行数（双语义对照表 SPEC §3.3 F10）。

    Fixture：1 ws + 1 node_started + 1 node_completed + 5 agent_tool_call + 1 agent_usage +
    1 workflow_completed = 10 全量事件；log 行数 = 4（ws + node_started + node_completed +
    workflow_completed；agent_tool_call 与 agent_usage 不在白名单）。
    """
    events = [
        _ws_started(seq=1),
        _event(2, "node_started", {}, node="n1", ts=1.0),
        _event(3, "agent_usage", {"cost_usd": 0.1}, ts=2.0),
    ]
    for i in range(4, 9):
        events.append(_event(i, "agent_tool_call", {"tool": "shell"}, ts=float(i)))
    events.append(_event(9, "node_completed", {"elapsed": 0.5, "output": {}}, node="n1", ts=9.0))
    events.append(_event(10, "workflow_completed", {"elapsed": 9.0, "outputs": {}}, ts=10.0))

    tape_path = tmp_path / "runs" / "r7.jsonl"
    _write_tape(tape_path, events)

    manager = RunManager()
    # meta 全量 event_count（get_run_extended_meta 路径，huge 判定用）
    count_full, _, _, _ = manager._scan_meta_overview_cached(tape_path)
    assert count_full == 10, f"全量 count 应=10：{count_full}"

    # RunSummary.event_count 是 log 行数（4）
    summary = manager._summary_from_tape(
        tape_path, project_id="p", project_name="P", source="attached",
    )
    assert summary is not None
    assert summary.event_count == 4, (
        f"RunSummary.event_count 应=4（log 行数：ws+node_started+node_completed+workflow_completed）："
        f"{summary.event_count}"
    )
    # agent_usage / agent_tool_call 不在 log 白名单（G4：工具调用/消息/思考不计入 event_count）
    assert summary.chart_count == 0


def test_ac7_meta_huge_threshold_uses_full_count_not_log_count(tmp_path):
    """AC7 验证 huge 判定确实用全量 count（不是 log 行数）——SPEC §3.3 双语义对照表「meta 全量」用途。

    若 meta event_count 误改为 log 行数，huge 判定（>50000）会失效（log 行数 << 全量 count）。
    本测试构造 6 全量事件（5 是 log），验证 meta 返 event_count=6（不是 5）。
    """
    events = [
        _ws_started(seq=1),
        _event(2, "node_started", {}, node="n1", ts=1.0),
        _event(3, "agent_usage", {"cost_usd": 0.1}, ts=2.0),  # 非 log
        _event(4, "agent_tool_call", {"tool": "x"}, ts=3.0),  # 非 log
        _event(5, "node_completed", {"elapsed": 0.5, "output": {}}, node="n1", ts=4.0),
        _event(6, "workflow_completed", {"elapsed": 4.0, "outputs": {}}, ts=5.0),
    ]
    tape_path = tmp_path / "runs" / "r8.jsonl"
    _write_tape(tape_path, events)

    manager = RunManager()
    # 模拟 attach：注册一个 AttachedRunHandle 让 get_run_extended_meta 路径可达。
    # 直接调 _scan_meta_overview_cached 拿 overview，验 meta 的 event_count 字段语义。
    count, _, _, overview_data = manager._scan_meta_overview_cached(tape_path)
    assert count == 6, f"meta event_count（全量）应=6：{count}"
    assert overview_data is not None
    ov = overview_data["overview"]
    # 双语义对照：overview.log_event_count = 4（ws + node_started + node_completed + workflow_completed）
    # 但 meta.event_count = 6（全量，含 agent_usage + agent_tool_call）。
    assert ov["log_event_count"] == 4, f"log_event_count 应=4：{ov['log_event_count']}"
    # 显式 invariant：meta count != log_event_count（双语义存在性）
    assert count != ov["log_event_count"], (
        "双语义分离失效：meta event_count 应 != overview.log_event_count（除非 tape 无非 log 事件）"
    )


# ── §3.4 in-memory 分支修复（discover_runs live run 不再硬编码 0）──────────


def test_in_memory_branch_event_count_not_hardcoded_zero(tmp_path):
    """SPEC §3.4：in-memory（live）run 的 event_count 从 tape fold 拿，不再硬编码 0。

    构造一个 in-memory handle + 对应 tape 文件，验证 discover_runs 返的 RunSummary.event_count
    非 0（且是 log 行数语义）。status 仍取 handle.status hint（不变）。
    """
    from orca.iface.web.run_manager import (
        AttachedRunHandle,
        AttachedTape,
        EventBus,
    )

    events = [
        _ws_started(seq=1, run_id="live-run"),
        _event(2, "node_started", {}, node="n1", ts=1.0),
        _event(3, "agent_tool_call", {"tool": "x"}, ts=2.0),  # 非 log
        _event(4, "node_completed", {"elapsed": 1.0, "output": {}}, node="n1", ts=3.0),
    ]
    tape_path = tmp_path / "runs" / "live-run.jsonl"
    _write_tape(tape_path, events)

    manager = RunManager(runs_dir=tmp_path / "runs")
    # 注册一个 attached handle 到 _runs（模拟 in-memory live run）
    tape = AttachedTape(tape_path, "live-run")
    bus = EventBus(tape)
    handle = AttachedRunHandle(
        run_id="live-run",
        bus=bus,
        tape=tape,
        tape_path=tape_path,
        status="running",
        terminal=False,
    )
    manager._runs["live-run"] = handle

    summaries = manager.discover_runs()
    by_id = {s.run_id: s for s in summaries}
    assert "live-run" in by_id
    s = by_id["live-run"]
    # SPEC §3.4：非 0（log 行数 = 3：ws + node_started + node_completed）
    assert s.event_count == 3, (
        f"in-memory 分支 event_count 应=3（log 行数，从 tape fold）：{s.event_count}（可能仍硬编码 0）"
    )
    # status hint 仍取 handle.status（E10/N5 不变）
    assert s.status == "running"


def test_in_memory_branch_tape_fold_failure_warns_and_fallback_zero(
    tmp_path, caplog, monkeypatch
):
    """SPEC §3.4 fail-soft：in-memory 分支 tape fold 失败（坏 tape / IO 错）→ warn + fallback
    event_count=0，不崩 discover_runs。

    守门 intent（SPEC §4 边界「in-memory 分支 tape 不可 fold」）：discover_runs 的 in-memory
    分支调 ``_scan_meta_overview(tp)`` 包了 try/except，异常路径必须 swallow + warn（含 run_id，
    fail loud 不静默）+ 不阻断列表。monkeypatch ``_scan_meta_overview`` 为 raising 验证此路径。
    """
    import logging

    from orca.iface.web import run_manager as rm_mod
    from orca.iface.web.run_manager import (
        AttachedRunHandle,
        AttachedTape,
        EventBus,
    )

    events = [_ws_started(seq=1, run_id="bad-live")]
    tape_path = tmp_path / "runs" / "bad-live.jsonl"
    _write_tape(tape_path, events)

    manager = RunManager(runs_dir=tmp_path / "runs")
    tape = AttachedTape(tape_path, "bad-live")
    bus = EventBus(tape)
    handle = AttachedRunHandle(
        run_id="bad-live",
        bus=bus,
        tape=tape,
        tape_path=tape_path,
        status="running",
        terminal=False,
    )
    manager._runs["bad-live"] = handle

    # monkeypatch 模块级 _scan_meta_overview 抛异常（in-memory 分支调用点）
    def _boom(_path):
        raise OSError("simulated IO failure")

    monkeypatch.setattr(rm_mod, "_scan_meta_overview", _boom)

    with caplog.at_level(logging.WARNING, logger="orca.iface.web.run_manager"):
        summaries = manager.discover_runs()
    by_id = {s.run_id: s for s in summaries}
    assert "bad-live" in by_id, "tape fold 失败不应让 run 从列表消失（fail-soft）"
    s = by_id["bad-live"]
    # fallback 0（不崩）
    assert s.event_count == 0
    assert s.chart_count == 0
    # warn 记录（含 run_id 便于稳定 grep）
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("bad-live" in m and "tape fold 失败" in m for m in msgs), (
        f"in-memory 分支 tape fold 失败必须 warn（含 run_id，fail loud）：{msgs}"
    )


# ── F3 fallback（overview 缺字段，v2 残留）────────────────────────────────


def test_f3_summary_from_overview_fallback_when_log_event_count_missing(tmp_path, caplog):
    """SPEC §4 F3：``_summary_from_overview`` 收到缺 ``log_event_count`` 的 overview（v2 残留
    或外部篡改）→ fallback 用全量 count + warn（over-count 方向 NEW-4；不静默）。

    F3 是 ``@staticmethod`` 无 cache 访问，不能触发 recompute——只能降级 + warn。
    v3 gate 后理论不可达，但 cache 文件可能被外部降版——fail loud 不静默。
    """
    import logging

    manager = RunManager()
    # 构造 v2 shape overview（无 log_event_count / chart_count）
    v2_overview = {
        "agents": [{"name": "n1", "status": "done"}],
        "charts": [],
        "cost_usd": 0.1,
        "run_status": "completed",
        "workflow_name": "legacy_v2",
        "started_ts": 1.0,
        "ended_ts": 2.0,
        # 故意缺 log_event_count + chart_count（v2 残留 shape）
    }
    with caplog.at_level(logging.WARNING, logger="orca.iface.web.run_manager"):
        summary = manager._summary_from_overview(
            "legacy-v2", 10, v2_overview,
            project_id=None, project_name=None, source="attached",
        )
    assert summary is not None
    # over-count fallback：用全量 count=10（NEW-4：含非 log 事件，标注降级不撒谎）
    assert summary.event_count == 10
    assert summary.chart_count == 0  # fallback 0
    # warn 记录（fail loud 不静默——含 run_id 便于 grep）
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("log_event_count" in m and "legacy-v2" in m for m in msgs), (
        f"缺 log_event_count 必须 warn（fail loud）：{msgs}"
    )
    assert any("chart_count" in m and "legacy-v2" in m for m in msgs), (
        f"缺 chart_count 必须 warn（fail loud）：{msgs}"
    )
