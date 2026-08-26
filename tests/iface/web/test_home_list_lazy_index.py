"""test_home_list_lazy_index.py —— SPEC ``docs/specs/2026-08-10-home-list-lazy-index.md``。

覆盖意图（AC3 / AC5 + §3.3 批量写回 + §3.4 scandir 直构 + §3.1 守卫，非仅行为）：
  - **AC3**：``_summary_from_tape`` 派生的 RunSummary 字段（workflow_name / status / progress /
    elapsed / started_at / event_count / cost）与 tape 已知值全等——验证 §3.1 单遍 capture +
    §3.2 ``_summary_from_overview`` 派生正确（等价于旧 ``_topology_workflow_name_from_tape`` +
    ``_scan_tape_timebounds`` 的口径）。
  - **AC5**：持久缓存 version gate——构造 v1/v2 cache 文件 → ``_persistent_cache_loaded`` 返空
    （version=3）+ warn。
  - **§3.3 批量写回**：``discover_runs`` 期间 defer，尾部 per-runs_dir 单次 flush，落盘
    ``version=3`` + entries 完整；二次 discovery 走缓存命中（直构，零 recompute）。
  - **§3.4 scandir 直构**：缓存命中 → ``_summary_from_overview`` 直构（不调 ``_scan_meta_overview``）。
  - **§3.1 守卫（BLOCKER I-1/I-2）**：非数值 timestamp 不炸 overview（守卫缺失会让外层 except
    吞掉整个 overview，agents/cost/status 一同丢失）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orca.iface.web.run_manager import RunManager, _scan_meta_overview
from orca.runtime import register_project

from tests.iface.web.conftest import make_manager


# ── helpers ──────────────────────────────────────────────────────────────


def _event(seq: int, etype: str, timestamp, data: dict, node=None) -> dict:
    return {
        "seq": seq,
        "type": etype,
        "timestamp": timestamp,
        "node": node,
        "session_id": None,
        "data": data,
    }


def _write_tape(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e, ensure_ascii=False) for e in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── AC3：RunSummary 字段全等（§3.1 capture + §3.2 派生）────────────────


def test_summary_from_tape_fields_match_tape_values(tmp_path):
    """AC3：``_summary_from_tape`` 从单遍 capture 的 overview 派生 RunSummary，字段值与
    tape 已知值全等（workflow_name / status / progress / elapsed / started_at / cost /
    event_count / chart_count）。等价于旧 ``_topology_workflow_name_from_tape`` +
    ``_scan_tape_timebounds`` 口径（同源 capture，验证 §3.1 等价性论证）。

    SPEC 2026-08-10-card-event-log-align §3.3：``event_count`` 语义改全量 → **log 行数**
    （对齐前端 ``classifyLogLevel`` 非 null 且非 route_taken）。fixture 含 6 全量事件，
    其中 ``agent_usage`` 不属 log 白名单 → log_event_count == 5（排除 agent_usage）。
    """
    tape_path = tmp_path / "runs" / "run-abc.jsonl"
    _write_tape(
        tape_path,
        [
            _event(
                1, "workflow_started", 1000.0,
                {
                    "inputs": {}, "node_count": 2, "entry": "n1",
                    "workflow_name": "my_workflow",
                    "topology": {"nodes": [{"name": "n1"}, {"name": "n2"}]},
                },
            ),
            _event(2, "node_started", 1001.0, {}, node="n1"),
            _event(3, "node_completed", 1002.0, {"elapsed": 1.0, "output": {}}, node="n1"),
            _event(4, "node_started", 1003.0, {}, node="n2"),
            _event(5, "agent_usage", 1004.0, {"cost_usd": 0.5}),
            _event(6, "workflow_completed", 1010.0, {"elapsed": 10.0, "outputs": {}}),
        ],
    )
    manager = RunManager()
    summary = manager._summary_from_tape(
        tape_path, project_id="p1", project_name="Proj", source="attached",
    )
    assert summary is not None
    assert summary.run_id == "run-abc"
    assert summary.workflow_name == "my_workflow"
    assert summary.status == "completed"
    assert summary.progress == "1/2"  # n1 done / n2 pending
    assert summary.elapsed == pytest.approx(10.0)  # 1010.0 - 1000.0
    assert summary.started_at == pytest.approx(1000.0)
    # SPEC §3.3：log 行数 = 5（排除 agent_usage；含 workflow_started/node_started×2/
    # node_completed/workflow_completed）。全量 count = 6 由 meta event_count 字段持有（见 AC7）。
    assert summary.event_count == 5
    assert summary.chart_count == 0
    assert summary.cost == pytest.approx(0.5)
    assert summary.source == "attached"


def test_summary_from_tape_workflow_name_fallback_to_stem(tmp_path):
    """AC3 边界：tape 无 workflow_started（或 ws 无 workflow_name）→ workflow_name fallback
    到 tape_path.stem（与旧 ``_topology_workflow_name_from_tape ... or stem`` 等价）。"""
    tape_path = tmp_path / "runs" / "fallback-id.jsonl"
    _write_tape(
        tape_path,
        [
            _event(
                1, "workflow_started", 100.0,
                {"inputs": {}, "node_count": 1, "entry": "n1",
                 "topology": {"nodes": [{"name": "n1"}]}},  # 无 workflow_name
            ),
            _event(2, "workflow_completed", 105.0, {"elapsed": 5.0, "outputs": {}}),
        ],
    )
    manager = RunManager()
    summary = manager._summary_from_tape(
        tape_path, project_id=None, project_name=None, source="attached",
    )
    assert summary is not None
    assert summary.workflow_name == "fallback-id"  # fallback stem
    assert summary.status == "completed"


def test_summary_from_tape_failed_status_and_elapsed(tmp_path):
    """AC3：failed 终态 + elapsed 从 started_ts/ended_ts 派生（无终态 → 0.0）。"""
    tape_path = tmp_path / "runs" / "fail-run.jsonl"
    _write_tape(
        tape_path,
        [
            _event(
                1, "workflow_started", 200.0,
                {"inputs": {}, "node_count": 1, "entry": "n1",
                 "workflow_name": "wf_fail",
                 "topology": {"nodes": [{"name": "n1"}]}},
            ),
            _event(2, "workflow_failed", 250.0, {"kind": "exec", "message": "x"}),
        ],
    )
    manager = RunManager()
    summary = manager._summary_from_tape(
        tape_path, project_id="p", project_name="P", source="attached",
    )
    assert summary is not None
    assert summary.status == "failed"
    assert summary.elapsed == pytest.approx(50.0)  # 250 - 200
    assert summary.started_at == pytest.approx(200.0)


# ── §3.1 守卫（BLOCKER I-1/I-2）：corrupt timestamp 不炸 overview ─────────


def test_scan_meta_overview_nonnumeric_timestamp_does_not_crash(tmp_path):
    """§3.1 守卫（BLOCKER I-1/I-2）：workflow_started.timestamp 为字符串（corrupt）时，
    isinstance 守卫阻止 float() 抛异常 → overview 正常返回（agents/run_status 不丢失）。
    守卫缺失会让外层 except 吞掉整个 overview。"""
    tape_path = tmp_path / "bad_ts.jsonl"
    _write_tape(
        tape_path,
        [
            _event(
                1, "workflow_started", "not-a-number",  # 非数值 timestamp
                {"inputs": {}, "node_count": 1, "entry": "n1",
                 "workflow_name": "guard_wf",
                 "topology": {"nodes": [{"name": "n1"}]}},
            ),
            _event(2, "workflow_completed", "also-bad", {"elapsed": 1.0, "outputs": {}}),
        ],
    )
    count, _, _, overview_data = _scan_meta_overview(tape_path)
    assert count == 2
    # overview 不为 None（守卫保住 agents / run_status / workflow_name）
    assert overview_data is not None
    overview = overview_data["overview"]
    assert overview["run_status"] == "completed"
    assert overview["workflow_name"] == "guard_wf"
    assert overview["agents"] == [{"name": "n1", "status": "pending"}]
    # 非数值 timestamp 守卫拦下 → started_ts / ended_ts 留 None
    assert overview["started_ts"] is None
    assert overview["ended_ts"] is None


def test_scan_meta_overview_capture_three_fields(tmp_path):
    """§3.1：单遍 capture workflow_name / started_ts / ended_ts 入 overview dict。"""
    tape_path = tmp_path / "capture.jsonl"
    _write_tape(
        tape_path,
        [
            _event(
                1, "workflow_started", 300.0,
                {"inputs": {}, "node_count": 1, "entry": "n1",
                 "workflow_name": "capture_wf",
                 "topology": {"nodes": [{"name": "n1"}]}},
            ),
            _event(2, "workflow_cancelled", 320.0, {"reason": "user"}),
        ],
    )
    _, _, _, overview_data = _scan_meta_overview(tape_path)
    assert overview_data is not None
    ov = overview_data["overview"]
    assert ov["workflow_name"] == "capture_wf"
    assert ov["started_ts"] == pytest.approx(300.0)
    assert ov["ended_ts"] == pytest.approx(320.0)


def test_scan_meta_overview_workflow_name_non_str_guarded(tmp_path):
    """§3.1 守卫：``workflow_started.data.workflow_name`` 为非 str（corrupt）时，
    ``isinstance(name, str) and name`` 守卫过滤 → overview workflow_name 留 None
    （下游 ``_summary_from_overview`` fallback 到 stem）。守卫不崩 overview。"""
    tape_path = tmp_path / "non_str_name.jsonl"
    _write_tape(
        tape_path,
        [
            _event(
                1, "workflow_started", 10.0,
                {"inputs": {}, "node_count": 1, "entry": "n1",
                 "workflow_name": 123,  # 非 str（corrupt）
                 "topology": {"nodes": [{"name": "n1"}]}},
            ),
            _event(2, "workflow_completed", 12.0, {"elapsed": 2.0, "outputs": {}}),
        ],
    )
    _, _, _, overview_data = _scan_meta_overview(tape_path)
    assert overview_data is not None
    assert overview_data["overview"]["workflow_name"] is None
    # 下游 fallback 到 stem
    manager = RunManager()
    summary = manager._summary_from_tape(
        tape_path, project_id=None, project_name=None, source="attached",
    )
    assert summary is not None
    assert summary.workflow_name == "non_str_name"  # fallback stem


# ── AC5：persistent cache version gate ──────────────────────────────────


def test_persistent_cache_v1_rejected_and_warns(tmp_path, caplog):
    """AC5：构造 v1 cache 文件 → ``_persistent_cache_loaded`` 返空（version=3）+ warn。

    SPEC 2026-08-10-card-event-log-align §3.5：cache version v2→v3（v2 缺 log_event_count /
    chart_count 字段）。v1 同样被拒（v1 缺 workflow_name / started_ts / ended_ts）。
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True)
    cache_path = runs_dir / ".orca-meta-cache.json"
    # v1 结构（无 version=3，无新五字段）
    cache_path.write_text(
        json.dumps(
            {"version": 1, "entries": {"old.jsonl": {"mtime": 1.0, "size": 10,
                                                    "count": 5, "oldest": 0,
                                                    "newest": 4, "overview": {}}}}
        ),
        encoding="utf-8",
    )
    manager = RunManager()
    with caplog.at_level(logging.WARNING, logger="orca.iface.web.run_manager"):
        data = manager._persistent_cache_loaded(runs_dir)
    # version gate 触发 → 空重建（version=3 + 无 entries）
    assert data["version"] == 3
    assert data["entries"] == {}
    # warn 记录（含文件路径，便于稳定 grep）
    assert any(
        "version 不符" in r.message and str(cache_path) in r.message
        for r in caplog.records
    )


def test_persistent_cache_v2_also_rejected_for_v3_upgrade(tmp_path, caplog):
    """SPEC 2026-08-10-card-event-log-align §3.5：v2 cache 也要被拒（v2 缺 log_event_count /
    chart_count）——v2→v3 升级 gate 守门。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True)
    cache_path = runs_dir / ".orca-meta-cache.json"
    # v2 结构（缺 log_event_count / chart_count）
    entry = {
        "mtime": 1.0, "size": 10, "count": 2, "oldest": 1, "newest": 2,
        "overview": {"overview": {"agents": [], "charts": [], "cost_usd": 0.0,
                                  "run_status": "completed",
                                  "workflow_name": "wf", "started_ts": 1.0,
                                  "ended_ts": 2.0}},
    }
    cache_path.write_text(
        json.dumps({"version": 2, "entries": {"old.jsonl": entry}}),
        encoding="utf-8",
    )
    manager = RunManager()
    with caplog.at_level(logging.WARNING, logger="orca.iface.web.run_manager"):
        data = manager._persistent_cache_loaded(runs_dir)
    assert data["version"] == 3  # gate 拒 v2 → 空 v3 重建
    assert data["entries"] == {}


def test_persistent_cache_v3_accepted(tmp_path):
    """AC5 配套：v3 cache 正常加载（不被 version gate 拒）。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True)
    cache_path = runs_dir / ".orca-meta-cache.json"
    entry = {
        "mtime": 1.0, "size": 10, "count": 2, "oldest": 1, "newest": 2,
        "overview": {"overview": {"agents": [], "charts": [], "cost_usd": 0.0,
                                  "run_status": "completed",
                                  "workflow_name": "wf", "started_ts": 1.0,
                                  "ended_ts": 2.0,
                                  # SPEC §3.5 v3 新增两字段
                                  "log_event_count": 2, "chart_count": 0}},
    }
    cache_path.write_text(
        json.dumps({"version": 3, "entries": {"ok.jsonl": entry}}),
        encoding="utf-8",
    )
    manager = RunManager()
    data = manager._persistent_cache_loaded(runs_dir)
    assert data["version"] == 3
    assert "ok.jsonl" in data["entries"]


# ── §3.3 批量写回 + §3.4 scandir 直构（discover_runs 端到端）──────────────


def test_discover_runs_writes_v3_cache_and_direct_constructs_on_hit(
    tmp_path, monkeypatch
):
    """§3.3 + §3.4：``discover_runs`` 落盘 version=3 cache + entries 完整；二次 discovery
    缓存命中走 ``_summary_from_overview`` 直构（不调 ``_scan_meta_overview`` recompute）。

    验证意图（非仅行为）：直构路径 zero-fold——patch ``_scan_meta_overview`` 为 raising stub，
    二次 discovery 若走 recompute 会 raise → 测试失败。"""
    monkeypatch.setenv("ORCA_HOME", str(tmp_path / ".orca_home"))
    proj = tmp_path / "proj"
    (proj / "workflows").mkdir(parents=True)  # marker
    runs_dir = proj / "runs"
    register_project(proj, require_marker=False)

    def _make_tape(name: str, wf: str, started: float, ended: float) -> None:
        _write_tape(
            runs_dir / f"{name}.jsonl",
            [
                _event(
                    1, "workflow_started", started,
                    {"inputs": {}, "node_count": 1, "entry": "n1",
                     "workflow_name": wf,
                     "topology": {"nodes": [{"name": "n1"}]}},
                ),
                _event(2, "workflow_completed", ended, {"elapsed": 1.0, "outputs": {}}),
            ],
        )

    _make_tape("run-1", "wf_one", 100.0, 110.0)
    _make_tape("run-2", "wf_two", 200.0, 205.0)

    manager = make_manager(tmp_path)

    # 首次 discovery：miss → recompute + defer 写回（批量）
    summaries = manager.discover_runs()
    by_id = {s.run_id: s for s in summaries}
    assert by_id["run-1"].workflow_name == "wf_one"
    assert by_id["run-1"].elapsed == pytest.approx(10.0)
    assert by_id["run-2"].workflow_name == "wf_two"
    assert by_id["run-2"].elapsed == pytest.approx(5.0)

    # 落盘 cache（version=3 + entries 完整 + defer 已 flush）
    cache_path = runs_dir / ".orca-meta-cache.json"
    assert cache_path.is_file(), "discover_runs 未落盘持久 cache"
    disk = json.loads(cache_path.read_text(encoding="utf-8"))
    assert disk["version"] == 3
    assert set(disk["entries"].keys()) == {"run-1.jsonl", "run-2.jsonl"}
    # entries 含新三字段（capture 进 overview）
    ov1 = disk["entries"]["run-1.jsonl"]["overview"]["overview"]
    assert ov1["workflow_name"] == "wf_one"
    assert ov1["started_ts"] == pytest.approx(100.0)
    assert ov1["ended_ts"] == pytest.approx(110.0)
    # defer 机制：discover_runs 后 dirty 集合已清空
    assert manager._dirty_runs_dirs == set()

    # 二次 discovery：patch _scan_meta_overview 为 raising stub
    # （缓存命中走直构，不应调 recompute；若误调会 raise → 测试失败）
    import orca.iface.web.run_manager as rm_mod

    def _boom(_path):
        raise AssertionError(
            "二次 discovery 缓存命中不应调 _scan_meta_overview recompute"
        )

    monkeypatch.setattr(rm_mod, "_scan_meta_overview", _boom)
    summaries2 = manager.discover_runs()
    by_id2 = {s.run_id: s for s in summaries2}
    # 直构结果与首次全等（AC3 snapshot 等价）
    assert by_id2["run-1"].workflow_name == "wf_one"
    assert by_id2["run-1"].elapsed == pytest.approx(10.0)
    assert by_id2["run-1"].status == "completed"
    assert by_id2["run-1"].event_count == by_id["run-1"].event_count
    assert by_id2["run-2"].workflow_name == "wf_two"


def test_discover_runs_corrupt_tape_skip_does_not_crash(tmp_path, monkeypatch):
    """§4 fail-soft：坏 tape（partial JSON）→ skip + warn，列表不崩，其余 tape 正常入列表。"""
    monkeypatch.setenv("ORCA_HOME", str(tmp_path / ".orca_home"))
    proj = tmp_path / "proj"
    (proj / "workflows").mkdir(parents=True)
    runs_dir = proj / "runs"
    register_project(proj, require_marker=False)

    # 好tape
    _write_tape(
        runs_dir / "good.jsonl",
        [
            _event(
                1, "workflow_started", 10.0,
                {"inputs": {}, "node_count": 1, "entry": "n1",
                 "workflow_name": "good_wf",
                 "topology": {"nodes": [{"name": "n1"}]}},
            ),
            _event(2, "workflow_completed", 12.0, {"elapsed": 2.0, "outputs": {}}),
        ],
    )
    # 坏tape（非 JSON 行触发 json.JSONDecodeError → break；count==0 → skip）
    (runs_dir / "bad.jsonl").write_text("{not valid json\n", encoding="utf-8")

    manager = make_manager(tmp_path)
    summaries = manager.discover_runs()
    ids = [s.run_id for s in summaries]
    assert "good" in ids
    assert "bad" not in ids


def test_persistent_cache_writeback_immediate_when_not_deferred(tmp_path):
    """§3.3：单 tape 路径（``_defer_persist=False``）即时写盘（n=1 无 O(n²)）。

    验证 ``get_run_extended_meta`` 等单 tape 调用链走即时写——``_defer_persist`` 默认 False。
    """
    runs_dir = tmp_path / "runs"
    _write_tape(
        runs_dir / "solo.jsonl",
        [
            _event(
                1, "workflow_started", 1.0,
                {"inputs": {}, "node_count": 1, "entry": "n1",
                 "workflow_name": "solo_wf",
                 "topology": {"nodes": [{"name": "n1"}]}},
            ),
            _event(2, "workflow_completed", 2.0, {"elapsed": 1.0, "outputs": {}}),
        ],
    )
    manager = RunManager()
    assert manager._defer_persist is False  # 默认即时写
    tape_path = runs_dir / "solo.jsonl"
    # _scan_meta_overview_cached 触发即时写回
    manager._scan_meta_overview_cached(tape_path)
    cache_path = runs_dir / ".orca-meta-cache.json"
    assert cache_path.is_file(), "单 tape 路径未即时落盘"
    disk = json.loads(cache_path.read_text(encoding="utf-8"))
    assert disk["version"] == 3
    assert "solo.jsonl" in disk["entries"]
    # defer 未开启 → 不标 dirty
    assert manager._dirty_runs_dirs == set()


# ── §4 两层 scandir fail-soft（I-5）──────────────────────────────────────


def test_iter_runs_dir_tapes_directory_scandir_oserror_falls_back_to_glob(
    tmp_path, monkeypatch, caplog,
):
    """§4 I-5：目录级 ``os.scandir`` OSError（权限 / 非 dir）→ 降级 glob +
    ``_summary_from_tape``（prestat=None）。验证降级路径仍枚举出全部 tape。

    intent：scandir 失败不崩 discovery——降级到 glob（prestat=None，走全量 fold）。
    模拟方式：首次 scandir 调用抛 OSError（``_iter_runs_dir_tapes`` 内），随后恢复真实
    scandir（glob 内部复用，仍可枚举）。
    """
    runs_dir = tmp_path / "runs"
    _write_tape(
        runs_dir / "r1.jsonl",
        [_event(1, "workflow_started", 1.0,
                {"inputs": {}, "node_count": 1, "entry": "n1",
                 "workflow_name": "w1", "topology": {"nodes": [{"name": "n1"}]}}),
         _event(2, "workflow_completed", 2.0, {"elapsed": 1.0, "outputs": {}})],
    )
    _write_tape(
        runs_dir / "r2.jsonl",
        [_event(1, "workflow_started", 3.0,
                {"inputs": {}, "node_count": 1, "entry": "n1",
                 "workflow_name": "w2", "topology": {"nodes": [{"name": "n1"}]}}),
         _event(2, "workflow_completed", 4.0, {"elapsed": 1.0, "outputs": {}})],
    )

    real_scandir = os.scandir
    state = {"raised": False}

    def _scandir_raise_once(directory):
        # 首次调用（_iter_runs_dir_tapes 的 scandir）抛 OSError；后续恢复真实 scandir
        # （让 glob 降级路径内部仍能枚举）。
        if not state["raised"]:
            state["raised"] = True
            raise OSError("permission denied")
        return real_scandir(directory)

    monkeypatch.setattr(os, "scandir", _scandir_raise_once)
    manager = RunManager()
    with caplog.at_level(logging.WARNING, logger="orca.iface.web.run_manager"):
        results = list(manager._iter_runs_dir_tapes(runs_dir))
    # 降级 glob：两个 tape 都枚举到，prestat=None（走全量 fold）
    assert len(results) == 2
    paths = [str(p) for p, _ in results]
    assert all(pre is None for _, pre in results)
    assert any("r1.jsonl" in p for p in paths)
    assert any("r2.jsonl" in p for p in paths)
    # warn 记录（含降级语义）
    assert any("scandir 失败降级" in r.message for r in caplog.records)


def test_iter_runs_dir_tapes_per_entry_stat_oserror_skips_only_that_entry(
    tmp_path, monkeypatch, caplog,
):
    """§4 I-5：per-entry ``DirEntry.stat()`` OSError（TOCTOU 删除 / 单文件权限）→
    skip+warn **该 entry**，**不降级整目录**（保持其余进度，避免 1354 重扫断崖）。

    intent：一个坏 entry 不让其余 1353 个白扫——其余仍 yield。
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True)
    # 构造两个 mock DirEntry：good 可 stat，bad stat 抛 OSError
    good = MagicMock()
    good.name = "good.jsonl"
    good.is_file.return_value = True
    good.path = str(runs_dir / "good.jsonl")
    good_stat = MagicMock(st_mtime=1.0, st_size=42)
    good.stat.return_value = good_stat

    bad = MagicMock()
    bad.name = "bad.jsonl"
    bad.is_file.return_value = True
    bad.path = str(runs_dir / "bad.jsonl")
    bad.stat.side_effect = OSError("no permission")

    # scandir 返回 [bad, good]（bad 在前，验证 continue 后 good 仍 yield）
    monkeypatch.setattr(os, "scandir", lambda _d: iter([bad, good]))
    manager = RunManager()
    with caplog.at_level(logging.WARNING, logger="orca.iface.web.run_manager"):
        results = list(manager._iter_runs_dir_tapes(runs_dir))
    # bad 被 skip（stat 失败），good 正常 yield
    assert len(results) == 1
    tape_path, prestat = results[0]
    assert tape_path.name == "good.jsonl"
    assert prestat is good_stat
    # warn 记录该 entry stat 失败
    assert any(
        "entry stat 失败 skip" in r.message and "bad.jsonl" in r.message
        for r in caplog.records
    )


# ── §3.1 ended_ts 后值覆盖 ───────────────────────────────────────────────


def test_scan_meta_overview_ended_ts_last_terminal_overwrites(tmp_path):
    """§3.1：多个终态事件 → ``ended_ts`` 取最末一个（后值覆盖，与旧
    ``_scan_tape_timebounds`` 同源）。验证 ``workflow_completed`` 后又出现
    ``workflow_cancelled`` 时，``ended_ts`` == 后者 timestamp。"""
    tape_path = tmp_path / "multi_terminal.jsonl"
    _write_tape(
        tape_path,
        [
            _event(1, "workflow_started", 100.0,
                   {"inputs": {}, "node_count": 1, "entry": "n1",
                    "workflow_name": "wf_multi",
                    "topology": {"nodes": [{"name": "n1"}]}}),
            _event(2, "workflow_completed", 110.0, {"elapsed": 10.0, "outputs": {}}),
            _event(3, "workflow_cancelled", 120.0, {"reason": "override"}),
        ],
    )
    _, _, _, overview_data = _scan_meta_overview(tape_path)
    assert overview_data is not None
    ov = overview_data["overview"]
    # 最末终态事件（workflow_cancelled, ts=120）覆盖 workflow_completed 的 110
    assert ov["ended_ts"] == pytest.approx(120.0)
    assert ov["started_ts"] == pytest.approx(100.0)
    # wf_status 取最后一个终态（cancelled 覆盖 completed）
    assert ov["run_status"] == "cancelled"

