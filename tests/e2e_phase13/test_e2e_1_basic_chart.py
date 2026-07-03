"""tests/e2e_phase13/test_e2e_1_basic_chart.py —— E2E-1 single script chart 真跑。

SPEC §8.3 验收点：
  - 编排跑 1 个 script node（调 chart_demo.py）
  - script 内 ``orca.chart.render_chart`` 推 line chart 到 ingestor
  - tape 含 ``custom(chart)`` 事件，node / session_id 正确
  - chart payload 字段对（chart_type / data / label / title）

驱动：``RunManager.start_run``（不 mock 编排），等终态，replay tape 断言。
"""

from __future__ import annotations

import asyncio

import pytest

from tests.e2e_phase13._workflows import basic_chart_wf


def test_e2e_1_basic_chart_pushes_chart_to_tape(
    short_runs_dir, tmp_path, chart_demo_script, artifacts_dir
):
    """E2E-1：单 script → render_chart → tape 含 custom(chart) 事件。"""
    from orca.iface.web.run_manager import RunManager

    yaml_path = basic_chart_wf(tmp_path, chart_demo_script)

    async def go():
        # 注意：runs_dir 必须传 short_runs_dir（macOS sock path length）
        manager = RunManager(runs_dir=short_runs_dir, max_concurrent=1)
        run_id = await manager.start_run(str(yaml_path), {}, None, None)
        await manager.wait_done(run_id, timeout=60)
        events = manager.get_run_events(run_id)
        await manager.shutdown()
        return run_id, events

    run_id, events = asyncio.run(go())

    # 断言 1：run 终态 completed（验证编排正常完成）
    completed = [e for e in events if e.type == "workflow_completed"]
    assert completed, f"workflow 未 completed；events={[e.type for e in events]}"

    # 断言 2：tape 含 custom(chart) 事件
    chart_events = [
        e for e in events
        if e.type == "custom" and e.data.get("kind") == "chart"
    ]
    assert len(chart_events) == 1, (
        f"应只有 1 个 chart 事件（chart_demo 推 1 张）；"
        f"got {len(chart_events)}: {chart_events}"
    )
    ev = chart_events[0]

    # 断言 3：node / session_id 路由正确（script 子进程经 env 注入 → render_chart 用）
    assert ev.node == "worker", f"chart 事件 node 应为 worker；got {ev.node!r}"
    assert ev.session_id and len(ev.session_id) >= 16, (
        f"chart 事件 session_id 应非空（script ScriptExecutor 生成的 uuid）；got {ev.session_id!r}"
    )

    # 断言 4：chart payload 字段正确
    chart = ev.data["chart"]
    assert chart["chart_type"] == "line"
    assert chart["label"] == "training"
    assert chart["title"] == "loss"
    assert chart["x"] == "step"
    assert chart["y"] == "loss"
    assert len(chart["data"]) == 5  # chart_demo.py 推 5 个数据点

    # 断言 5：script node 自己也 completed（stdout 含 seq）
    node_completed = [
        e for e in events
        if e.type == "node_completed" and e.node == "worker"
    ]
    assert node_completed, "worker node 未 completed"
    stdout = node_completed[0].data["output"]["stdout"]
    assert "pushed chart" in stdout, f"chart_demo stdout 缺成功标记；got {stdout!r}"
