"""GAP 真用户验证脚本（v1.1.1 已 deprecated by v2）。

**TODO Step 6**：v1.1.1 ActivityStream / DagGraph 已删（Step 1a/1b），本脚本
原断言全失效。Step 2-5 v2 widget 填充 + e2e fixture 更新后此脚本重写为：
  - mxint tape 重放 → v2 AgentsList 5 agent 拓扑序 + AgentHistory last message 展开
  - demo_loop tape 重放 → counter iter N 显示 + self-loop 不 crash
  - with_retry tape 重放 → Log Stream retry chain 完整

当前文件仅保留 tape 加载 helper（load_wf_from_tape）+ 主入口；verifier 函数
改为 TODO 占位 print（保留运行入口，pytest 不收本文件）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from orca.iface.cli.app import OrcaApp
from orca.schema.event import Event
from orca.schema.workflow import (
    AgentNode, Route, ScriptNode, SetNode, Workflow,
)

MXINT_TAPE = Path("runs/mxint_analysis-20260704-105608-90fd22.jsonl")
DEMO_LOOP_TAPE = Path("runs/demo_loop-20260704-133900-c20797.jsonl")
ARTIFACTS = Path("tests/iface/cli/_artifacts")


def load_wf_from_tape(tape_path: Path) -> Workflow:
    """从 tape workflow_started 拓扑反构 Workflow。"""
    with tape_path.open() as f:
        first_line = f.readline()
    data = json.loads(first_line)["data"]
    nodes = []
    routes_dict: dict[str, list[str]] = {}
    for n in data["topology"]["nodes"]:
        kind = n.get("kind")
        if kind == "agent":
            nodes.append(AgentNode(name=n["name"], executor="opencode"))
        elif kind == "set":
            nodes.append(SetNode(name=n["name"], values={"placeholder": "0"}))
        elif kind == "script":
            nodes.append(ScriptNode(name=n["name"], command="echo"))
        else:
            nodes.append(AgentNode(name=n["name"], executor="opencode"))
    for r in data["topology"]["routes"]:
        if r["to"] != "$end":
            routes_dict.setdefault(r["from"], []).append(r["to"])
    for n in nodes:
        if n.name in routes_dict:
            n.routes = [Route(to=t) for t in routes_dict[n.name]]
        else:
            n.routes = [Route(to="$end")]
    return Workflow(
        name=data["workflow_name"], entry=data["entry"],
        nodes=nodes, parallel=[],
    )


def replay_and_verify(tape_path: Path, label: str, verifier) -> None:
    """重放 tape 并在 app alive 时跑 verifier(app)。"""
    wf = load_wf_from_tape(tape_path)
    tape_out = ARTIFACTS / f"_gap_verify_{label}.jsonl"
    app = OrcaApp(wf=wf, tape_path=tape_out)
    app.kickoff = lambda: None  # type: ignore[assignment]

    async def scenario():
        async with app.run_test() as pilot:
            with tape_path.open() as f:
                for line in f:
                    ev_dict = json.loads(line)
                    try:
                        ev = Event(**ev_dict)
                        app._dispatch_to_widgets(ev)
                    except Exception as e:
                        print(f"  skip event {ev_dict.get('seq')}: {e}")
            await pilot.pause()
            await pilot.pause()
            # SVG screenshot（验证用，可失败不致命）
            try:
                svg = ARTIFACTS / f"gap_verify_{label}.svg"
                ARTIFACTS.mkdir(parents=True, exist_ok=True)
                app.save_screenshot(str(svg))
                print(f"  SVG saved: {svg}")
            except Exception as e:
                print(f"  screenshot failed: {e}")
            # 跑 verifier（app alive 时）
            verifier(app)
    asyncio.run(scenario())


def verify_mxint(app: OrcaApp) -> None:
    """TODO Step 6：v2 AgentsList + AgentHistory 断言。

    原 GAP-A/B/C（v1.1.1 DagGraph tokens / ActivityStream tool_result summary）
    已删；Step 2 AgentsList 填充后改为：
      - 5 节点 AgentsList.update_node(tokens=...) 投影正确
      - AgentHistory entries 含 tool_result summary（从 _event_summary 派生）
    """
    print("\n[TODO Step 6] verify_mxint: v2 widget 断言待 Step 2/3 填充后补")


def verify_demo_loop(app: OrcaApp) -> None:
    """TODO Step 6：v2 loop workflow iter N 显示断言。

    原 GAP-E（v1.1.1 DagGraph self_loop_nodes + iter_n）已删；Step 2 AgentsList
    填充后改为：
      - counter iter N == node_started 次数（AgentsList 显示 "iter 3"）
      - self-loop 节点不抛错（dispatch 兼容）
    """
    print("\n[TODO Step 6] verify_demo_loop: v2 loop workflow 断言待 Step 2 填充后补")


if __name__ == "__main__":
    print("=== GAP 真用户验证 1：mxint_analysis tape（186 events）===")
    replay_and_verify(MXINT_TAPE, "mxint", verify_mxint)
    print("\n=== GAP 真用户验证 2：demo_loop tape（14 events）===")
    replay_and_verify(DEMO_LOOP_TAPE, "demo_loop", verify_demo_loop)
    print("\n" + "=" * 60)
    print("（TODO Step 6）v2 widget 断言待填充（Step 2/3/5）")
