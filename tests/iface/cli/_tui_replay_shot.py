"""TUI 截屏 replay：重放 mxint tape，截 SVG screenshot（spec v1.1 §9.3）。

非测试文件——是开发期截屏脚本（pytest 跑会生成 SVG，无断言）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from orca.iface.cli.app import OrcaApp
from orca.schema.event import Event
from orca.schema.workflow import Workflow

TAPE = Path("runs/mxint_analysis-20260704-105608-90fd22.jsonl")
ARTIFACTS = Path("tests/iface/cli/_artifacts")


def load_workflow_from_tape(tape_path: Path) -> Workflow:
    """从 tape 第一行 workflow_started.data 拿 topology 反构造 Workflow（简化版）。"""
    with tape_path.open() as f:
        first_line = f.readline()
    data = json.loads(first_line)["data"]
    # 拓扑信息足以构造 DagGraph；用 minimal Workflow
    from orca.schema.workflow import AgentNode, Route, Workflow
    nodes = []
    routes_dict: dict[str, list[str]] = {}
    for n in data["topology"]["nodes"]:
        nodes.append(AgentNode(name=n["name"], executor="opencode"))
    for r in data["topology"]["routes"]:
        if r["to"] != "$end":
            routes_dict.setdefault(r["from"], []).append(r["to"])
    # 给每个 node 添加 routes
    for n in nodes:
        if n.name in routes_dict:
            n.routes = [Route(to=t) for t in routes_dict[n.name]]
        else:
            n.routes = [Route(to="$end")]
    return Workflow(
        name=data["workflow_name"],
        entry=data["entry"],
        nodes=nodes,
        parallel=[],
    )


async def replay_and_screenshot():
    wf = load_workflow_from_tape(TAPE)
    app = OrcaApp(wf=wf, tape_path=ARTIFACTS / "_replay.jsonl")
    # patch kickoff 不真跑 orchestrator（避免 spawn opencode）
    app.kickoff = lambda: None  # type: ignore
    async with app.run_test() as pilot:
        # 重放 tape 全部事件
        with TAPE.open() as f:
            for line in f:
                ev_dict = json.loads(line)
                try:
                    ev = Event(**ev_dict)
                    app._dispatch_to_widgets(ev)
                except Exception as e:
                    print(f"skip event {ev_dict.get('seq')}: {e}")
        await pilot.pause()
        await pilot.pause()
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        # 截屏：DAG 视图 + Activity Stream + Header
        svg = ARTIFACTS / "tui_v1_replay.svg"
        try:
            app.save_screenshot(str(svg))
            print(f"SVG saved: {svg}")
        except Exception as e:
            print(f"screenshot failed: {e}")
        # Activity Stream snapshot
        from orca.iface.cli.widgets.activity_stream import ActivityStream
        activity = app.query_one(ActivityStream)
        print(f"Activity Stream entries: {len(activity.entries)}")
        # 取头 5 条 entry summary
        for e in activity.entries[:5]:
            print(f"  seq={e.seq} type={e.event_type} node={e.node} summary={e.summary_line[:50]!r}")


if __name__ == "__main__":
    asyncio.run(replay_and_screenshot())
