"""TUI v2 截屏 replay：重放 mxint tape，截 v2 三块布局 SVG screenshot（spec v2 §9.3）。

非测试文件——是开发期截屏脚本（pytest 跑会生成 SVG，无断言）。

**v2 三块布局验证**（spec §2.1）：
  - 左 30% AgentsList：拓扑序纵向 + 每行 icon/elapsed/tok
  - 右上 70% AgentHistory：选中 agent 的 events + last message 默认展开
  - 右下 30% LogStream：高层节点事件（node_started/completed/...）

用法（开发期）::

    python3 tests/iface/cli/_tui_replay_shot.py [tape.jsonl]

默认重放 ``runs/mxint_analysis-20260704-105608-90fd22.jsonl``，输出 SVG 到
``tests/iface/cli/_artifacts/tui_v2_replay.svg``。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from orca.iface.cli.app import OrcaApp
from orca.schema.event import Event
from orca.schema.workflow import Workflow

DEFAULT_TAPE = Path("runs/mxint_analysis-20260704-105608-90fd22.jsonl")
ARTIFACTS = Path("tests/iface/cli/_artifacts")


def load_workflow_from_tape(tape_path: Path) -> Workflow:
    """从 tape 第一行 workflow_started.data 拿 topology 反构造 Workflow（简化版）。"""
    with tape_path.open() as f:
        first_line = f.readline()
    data = json.loads(first_line)["data"]
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


async def replay_and_screenshot(tape_path: Path) -> Path | None:
    """重放 tape + 截 v2 三块布局 SVG，返回 SVG path（失败返 None）。"""
    wf = load_workflow_from_tape(tape_path)
    app = OrcaApp(wf=wf, tape_path=ARTIFACTS / "_replay.jsonl")
    # patch kickoff 不真跑 orchestrator（避免 spawn opencode）
    app.kickoff = lambda: None  # type: ignore[assignment]
    async with app.run_test(size=(140, 44)) as pilot:
        # 重放 tape 全部事件
        with tape_path.open() as f:
            for line in f:
                ev_dict = json.loads(line)
                try:
                    ev = Event(**ev_dict)
                    app._dispatch_to_widgets(ev)
                except Exception as e:
                    print(f"skip event {ev_dict.get('seq')}: {e}")
        await pilot.pause()
        await pilot.pause()

        # v2 widget snapshot（验证点：三块布局都 mount）
        from orca.iface.cli.widgets import AgentsList, AgentHistory, LogStream
        lst = app.query_one(AgentsList)
        history = app.query_one(AgentHistory)
        log_stream = app.query_one(LogStream)
        # 打印拓扑 + 选中 agent + last entry
        if lst._node_names:
            print(f"AgentsList nodes: {lst._node_names}")
            print(f"AgentsList selected: {lst._selected}")
        print(f"AgentHistory node: {history._node_name}")
        print(f"AgentHistory entries: {len(history._entries)}")
        print(f"AgentHistory expanded_seqs: {history._expanded_seqs}")
        print(f"LogStream lines: {len(log_stream.lines)}")

        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        svg = ARTIFACTS / "tui_v2_replay.svg"
        try:
            app.save_screenshot(filename=str(svg))
            print(f"SVG saved: {svg}")
            return svg
        except Exception as e:
            print(f"screenshot failed: {e}")
            return None


def main() -> None:
    tape = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TAPE
    if not tape.exists():
        print(f"tape not found: {tape}")
        sys.exit(1)
    asyncio.run(replay_and_screenshot(tape))


if __name__ == "__main__":
    main()
