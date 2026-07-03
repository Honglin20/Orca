"""tests/e2e_phase13/test_e2e_2_multi_run_parallel.py —— E2E-2 多 run 并行真跑。

SPEC §8.3 验收点：
  - 起 3 个 run 并行（同一 RunManager.start_run 调 3 次）
  - 每 run 调 chart_parallel.py（label 嵌 ORCA_RUN_ID 标识）
  - **A run 的 tape 只含 A 标识的 chart，B/C 同理**（env 不串）

驱动：``RunManager.start_run`` × 3，gather 等 3 个都终态，逐 run replay tape 断言隔离。
"""

from __future__ import annotations

import asyncio

import pytest

from tests.e2e_phase13._workflows import parallel_chart_wf


def test_e2e_2_multi_run_parallel_no_env_cross_talk(
    short_runs_dir, tmp_path, chart_parallel_script, artifacts_dir
):
    """E2E-2：3 run 并行 → 各 tape 只含自己 run_id 标识的 chart。"""
    from orca.iface.web.run_manager import RunManager

    yaml_path = parallel_chart_wf(tmp_path, chart_parallel_script)

    async def go():
        # max_concurrent=3 让 3 个 run 真并行（sem 内 asyncio 自然并发）
        manager = RunManager(runs_dir=short_runs_dir, max_concurrent=3)
        run_ids = []
        for _ in range(3):
            rid = await manager.start_run(str(yaml_path), {}, None, None)
            run_ids.append(rid)
        # 等 3 个都到终态
        await asyncio.gather(*[manager.wait_done(rid, timeout=60) for rid in run_ids])
        per_run_events = {rid: manager.get_run_events(rid) for rid in run_ids}
        await manager.shutdown()
        return run_ids, per_run_events

    run_ids, per_run_events = asyncio.run(go())

    assert len(run_ids) == 3
    assert len(set(run_ids)) == 3, f"3 个 run_id 应唯一；got {run_ids}"

    # 每 run 都 completed
    for rid in run_ids:
        events = per_run_events[rid]
        assert any(e.type == "workflow_completed" for e in events), (
            f"run {rid} 未 completed；events={[e.type for e in events]}"
        )

    # 每 run tape 恰好 1 个 chart，且 label 含该 run_id 标识
    for rid in run_ids:
        events = per_run_events[rid]
        chart_events = [
            e for e in events
            if e.type == "custom" and e.data.get("kind") == "chart"
        ]
        assert len(chart_events) == 1, (
            f"run {rid} 应只有 1 个 chart；got {len(chart_events)}"
        )
        ev = chart_events[0]
        chart = ev.data["chart"]
        # chart_parallel.py 用 ORCA_RUN_ID 派生 tag（取 split('-')[-1][:8]）
        expected_tag = rid.split("-")[-1][:8] if "-" in rid else rid[:8]
        assert chart["label"] == f"parallel-{expected_tag}", (
            f"run {rid} chart label 应嵌 run_id 标识；"
            f"got {chart['label']!r}，期望 parallel-{expected_tag}"
        )
        assert ev.node == "worker"

    # 隔离断言：A run 的 chart label 不会出现在 B/C 的 tape 里
    labels_per_run = {
        rid: next(
            e.data["chart"]["label"]
            for e in per_run_events[rid]
            if e.type == "custom" and e.data.get("kind") == "chart"
        )
        for rid in run_ids
    }
    assert len(set(labels_per_run.values())) == 3, (
        f"3 个 run 的 chart label 应唯一；got {labels_per_run}"
    )
