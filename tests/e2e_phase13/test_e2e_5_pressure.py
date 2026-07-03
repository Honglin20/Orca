"""tests/e2e_phase13/test_e2e_5_pressure.py —— E2E-5 压测：3 run × 10 chart 真跑。

SPEC §8.4 验收点（用户重点）：
  - 3 个 run 并行 × 每 run 1 script 推 10 chart = 30 张图
  - 各 tape 各 10 chart 事件（无丢失）
  - 各 tape 仅含自己 run_id 标识的 chart（无串扰）
  - chart_type 5 种轮转（line/bar/area/scatter/table）

驱动：``RunManager.start_run`` × 3（max_concurrent=3 真并行），等 3 终态，逐 run 断言。
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from tests.e2e_phase13._workflows import pressure_chart_wf


def test_e2e_5_pressure_3_runs_x_10_charts_no_loss_no_crosstalk(
    short_runs_dir, tmp_path, chart_pressure_script, artifacts_dir
):
    """E2E-5：3 run × 10 chart → 每 tape 各 10 chart，无丢失 / 无串扰。"""
    from orca.iface.web.run_manager import RunManager

    yaml_path = pressure_chart_wf(tmp_path, chart_pressure_script)

    async def go():
        manager = RunManager(runs_dir=short_runs_dir, max_concurrent=3)
        run_ids = []
        for _ in range(3):
            rid = await manager.start_run(str(yaml_path), {}, None, None)
            run_ids.append(rid)
        # 压测：3 run 同时跑，每 run 10 chart，最长 ~60s
        await asyncio.gather(*[manager.wait_done(rid, timeout=120) for rid in run_ids])
        per_run_events = {rid: manager.get_run_events(rid) for rid in run_ids}
        await manager.shutdown()
        return run_ids, per_run_events

    run_ids, per_run_events = asyncio.run(go())

    # 每 run 都 completed
    for rid in run_ids:
        events = per_run_events[rid]
        assert any(e.type == "workflow_completed" for e in events), (
            f"run {rid} 未 completed；events tail: {[e.type for e in events[-5:]]}"
        )

    # 每 run tape 恰好 10 chart 事件（无丢失）
    for rid in run_ids:
        events = per_run_events[rid]
        chart_events = [
            e for e in events
            if e.type == "custom" and e.data.get("kind") == "chart"
        ]
        assert len(chart_events) == 10, (
            f"run {rid} 应有 10 chart（无丢失）；got {len(chart_events)}"
        )

    # 每 run 的 10 chart label 全一致（嵌 ORCA_RUN_ID 标识），且只属于该 run
    expected_tags = {rid: rid.split("-")[-1][:8] if "-" in rid else rid[:8] for rid in run_ids}
    for rid in run_ids:
        events = per_run_events[rid]
        chart_events = [
            e for e in events
            if e.type == "custom" and e.data.get("kind") == "chart"
        ]
        labels = {e.data["chart"]["label"] for e in chart_events}
        assert labels == {f"pressure-{expected_tags[rid]}"}, (
            f"run {rid} 的 chart label 应只有 pressure-{expected_tags[rid]}；"
            f"got {labels}（串扰？）"
        )

    # 每 run 的 10 chart title 0-9 全有
    for rid in run_ids:
        events = per_run_events[rid]
        chart_events = [
            e for e in events
            if e.type == "custom" and e.data.get("kind") == "chart"
        ]
        titles = {e.data["chart"]["title"] for e in chart_events}
        assert titles == {f"chart-{i}" for i in range(10)}, (
            f"run {rid} chart titles 应为 chart-0..chart-9；got {sorted(titles)}"
        )

    # chart_type 5 种轮转（line/bar/area/scatter/table）—— 每 run 至少出现 4 种
    for rid in run_ids:
        events = per_run_events[rid]
        chart_events = [
            e for e in events
            if e.type == "custom" and e.data.get("kind") == "chart"
        ]
        types_counter = Counter(e.data["chart"]["chart_type"] for e in chart_events)
        assert set(types_counter.keys()) >= {"line", "bar", "area", "scatter", "table"}, (
            f"run {rid} chart_type 应覆盖 5 种；got {dict(types_counter)}"
        )

    # 隔离断言：3 run 的 label 集合互不相交（无串扰）
    all_label_sets = [
        {e.data["chart"]["label"]
         for e in per_run_events[rid]
         if e.type == "custom" and e.data.get("kind") == "chart"}
        for rid in run_ids
    ]
    for i in range(3):
        for j in range(i + 1, 3):
            assert all_label_sets[i].isdisjoint(all_label_sets[j]), (
                f"run {run_ids[i]} 和 run {run_ids[j]} 的 label 集合相交："
                f"{all_label_sets[i]} ∩ {all_label_sets[j]}"
            )
