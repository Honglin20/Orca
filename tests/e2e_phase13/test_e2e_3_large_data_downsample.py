"""tests/e2e_phase13/test_e2e_3_large_data_downsample.py —— E2E-3 大数据降采样真跑。

SPEC §8.3 验收点：
  - 100k 行输入 + max_points=2000（默认）
  - client lib 端降采样到 ≤ 2000 行
  - 整条消息 ≤ MAX_MESSAGE_BYTES（2MB）
  - tape 含 custom(chart) 事件，data 行数 ≤ 2000

驱动：``RunManager.start_run``（rows=100000 max_points=2000），等终态，replay tape 断言。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from orca.chart._limits import MAX_MESSAGE_BYTES
from tests.e2e_phase13._workflows import large_chart_wf


def test_e2e_3_large_data_downsample_within_limits(
    short_runs_dir, tmp_path, chart_large_script, artifacts_dir
):
    """E2E-3：100k 行 → 降采样到 ≤2000 + 消息 ≤2MB → tape 落 chart。"""
    from orca.iface.web.run_manager import RunManager

    yaml_path = large_chart_wf(tmp_path, chart_large_script, rows=100_000, max_points=2000)

    async def go():
        manager = RunManager(runs_dir=short_runs_dir, max_concurrent=1)
        run_id = await manager.start_run(str(yaml_path), {}, None, None)
        await manager.wait_done(run_id, timeout=90)
        events = manager.get_run_events(run_id)
        await manager.shutdown()
        return run_id, events

    run_id, events = asyncio.run(go())

    # 编排正常 completed
    completed = [e for e in events if e.type == "workflow_completed"]
    assert completed, f"workflow 未 completed；events tail: {[e.type for e in events[-5:]]}"

    # script 自身 exit 0（降采样成功，未触发超限）
    node_completed = [
        e for e in events
        if e.type == "node_completed" and e.node == "worker"
    ]
    assert node_completed, "worker 未 completed"
    assert node_completed[0].data["output"]["exit_code"] == 0, (
        f"script 应 exit 0（降采样通过）；got {node_completed[0].data['output']}"
    )

    # tape 含 1 个 chart 事件
    chart_events = [
        e for e in events
        if e.type == "custom" and e.data.get("kind") == "chart"
    ]
    assert len(chart_events) == 1, f"应只有 1 个 chart；got {len(chart_events)}"

    # 降采样后 data 行数 ≤ 2000
    chart = chart_events[0].data["chart"]
    assert len(chart["data"]) <= 2000, (
        f"降采样后行数应 ≤ 2000；got {len(chart['data'])}"
    )
    # 同时不能太稀（至少 1000，否则降采样策略可疑）
    assert len(chart["data"]) >= 1000, (
        f"降采样后行数 {len(chart['data'])} 异常低（<1000）—— 策略可疑"
    )

    # 整条 chart 事件 JSON 编码 < MAX_MESSAGE_BYTES（2MB）
    encoded = json.dumps({
        "node": chart_events[0].node,
        "session_id": chart_events[0].session_id,
        "payload": chart,
    }).encode("utf-8")
    assert len(encoded) < MAX_MESSAGE_BYTES, (
        f"chart payload 编码 {len(encoded)} > {MAX_MESSAGE_BYTES}（消息体过大）"
    )
