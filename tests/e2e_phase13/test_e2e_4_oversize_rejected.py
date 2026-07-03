"""tests/e2e_phase13/test_e2e_4_oversize_rejected.py —— E2E-4 超限拒绝真跑。

SPEC §8.3 验收点：
  - 500k 行 + max_points=200_000
  - client lib 降采样后 200k 行（远超 2MB）→ client ``_render`` raise ValueError
  - script 非零退出（chart_large.py exit 2）
  - tape 无对应 chart 事件（SPEC §5.2 核心契约）

驱动：``RunManager.start_run``（rows=500000 max_points=200000），等终态，断言 tape 无 chart。
"""

from __future__ import annotations

import asyncio

import pytest

from tests.e2e_phase13._workflows import large_chart_wf


def test_e2e_4_oversize_rejected_no_chart_in_tape(
    short_runs_dir, tmp_path, chart_large_script, artifacts_dir
):
    """E2E-4：500k 行 + max_points=200k → client raise + script exit 2 + tape 无 chart。"""
    from orca.iface.web.run_manager import RunManager

    yaml_path = large_chart_wf(
        tmp_path, chart_large_script, rows=500_000, max_points=200_000
    )

    async def go():
        manager = RunManager(runs_dir=short_runs_dir, max_concurrent=1)
        run_id = await manager.start_run(str(yaml_path), {}, None, None)
        await manager.wait_done(run_id, timeout=120)
        events = manager.get_run_events(run_id)
        await manager.shutdown()
        return run_id, events

    run_id, events = asyncio.run(go())

    # script 非零退出（chart_large.py exit 2 = REJECTED）—— 注意 SPEC §4.6：
    # 非零退出码不 fail loud（业务结果），所以 node_completed 而非 node_failed。
    node_completed = [
        e for e in events
        if e.type == "node_completed" and e.node == "worker"
    ]
    assert node_completed, (
        f"worker 应 node_completed（非零业务退出码不 fail loud）；"
        f"events: {[e.type for e in events]}"
    )
    output = node_completed[0].data["output"]
    assert output["exit_code"] == 2, (
        f"script 应 exit 2（REJECTED 路径）；got exit_code={output['exit_code']}"
    )
    assert "REJECTED" in output["stdout"], (
        f"stdout 应含 REJECTED 标记；got {output['stdout']!r}"
    )

    # tape 无 chart 事件（client raise → 未发 socket 消息 → ingestor 未 emit）
    chart_events = [
        e for e in events
        if e.type == "custom" and e.data.get("kind") == "chart"
    ]
    assert chart_events == [], (
        f"超限 payload 不应写入 tape（SPEC §5.2）；got {len(chart_events)} chart 事件"
    )
