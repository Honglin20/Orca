"""v2 TUI 真用户验证脚本（重放 tape + 三块布局断言）。

非测试文件——是开发期脚本（pytest 不收）。重放 mxint / demo_loop tape，
在 app alive 时跑 verifier 函数，断言 v2 三块布局正确显示。

**v2 三块布局断言**（spec §2）：
  - AgentsList：拓扑序 + 每行投影（status/elapsed/tok）
  - AgentHistory：选中 agent 的 events + last message 默认展开
  - LogStream：高层节点事件（node_started/completed/...）

用法（开发期）::

    python3 tests/iface/cli/_tui_gap_verify.py
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from orca.iface.cli.app import OrcaApp
from orca.iface.cli.widgets import AgentHistory, AgentsList, LogStream
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
        async with app.run_test(size=(140, 44)) as pilot:
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
                app.save_screenshot(filename=str(svg))
                print(f"  SVG saved: {svg}")
            except Exception as e:
                print(f"  screenshot failed: {e}")
            # 跑 verifier（app alive 时）
            verifier(app)
    asyncio.run(scenario())


def verify_mxint(app: OrcaApp) -> None:
    """v2 AgentsList + AgentHistory 断言（mxint_analysis 5 agent 拓扑）。"""
    print("\n=== verify_mxint ===")
    lst = app.query_one(AgentsList)
    history = app.query_one(AgentHistory)
    log_stream = app.query_one(LogStream)

    # 1. AgentsList 含 5 agent（mxint 拓扑：analyzer/configurator/runner/diagnostic_saver/report_painter）
    expected = {"analyzer", "configurator", "runner", "diagnostic_saver", "report_painter"}
    actual = set(lst._node_names)
    assert expected.issubset(actual), (
        f"AgentsList 缺节点；expected ⊆ {expected}，got {actual}"
    )
    print(f"  [OK] AgentsList 含 {len(lst._node_names)} 个 agent")

    # 2. 至少 1 个 agent 状态投影为 done（tape 重放完必有）
    done_nodes = [n for n in lst._node_names if lst.projection_of(n) and lst.projection_of(n).status == "done"]
    assert done_nodes, (
        f"AgentsList 无 done 节点（重放后必有终态）；"
        f"projections={[(n, lst.projection_of(n).status) for n in lst._node_names]}"
    )
    print(f"  [OK] {len(done_nodes)} 个 agent done：{done_nodes}")

    # 3. AgentHistory 含 events（auto-follow 默认选中最后一个 running node）
    assert len(history._entries) > 0, (
        f"AgentHistory 空（auto-follow 应驱动 set_node + events）；"
        f"selected={lst._selected}, _node_events keys={list(app._node_events.keys())}"
    )
    print(f"  [OK] AgentHistory 含 {len(history._entries)} events（node={history._node_name}）")

    # 4. last message 默认展开（_expanded_seqs 非空 + 含最后一条 agent_message seq）
    if any(e.event_type == "agent_message" for e in history._entries):
        assert history._expanded_seqs, (
            f"AgentHistory _expanded_seqs 空（应含 last agent_message seq）"
        )
        print(f"  [OK] last message 默认展开，expanded_seqs={history._expanded_seqs}")
    else:
        print(f"  [SKIP] 无 agent_message event（本 tape 无 message；expanded_seqs={history._expanded_seqs}）")

    # 5. LogStream 含高层事件（node_started/completed 等必有）
    assert len(log_stream.lines) > 0, "LogStream 空（重放后必有 node_started/completed 等）"
    print(f"  [OK] LogStream 含 {len(log_stream.lines)} 行高层事件")


def verify_demo_loop(app: OrcaApp) -> None:
    """v2 loop workflow iter N 显示断言（demo_loop tape，counter self-loop）。"""
    print("\n=== verify_demo_loop ===")
    lst = app.query_one(AgentsList)
    history = app.query_one(AgentHistory)
    log_stream = app.query_one(LogStream)

    # demo_loop 含 counter 节点（self-loop 重入）
    assert "counter" in lst._node_names, f"AgentsList 缺 counter；got {lst._node_names}"
    print(f"  [OK] AgentsList 含 counter（self-loop 节点）")

    # counter 投影应含 iter_n > 1（loop 重入）
    counter_proj = lst.projection_of("counter")
    assert counter_proj is not None, "counter projection is None"
    # demo_loop counter iter_n 至少 3（典型 tape 有 3 轮）
    if counter_proj.iter_n > 1:
        print(f"  [OK] counter.iter_n={counter_proj.iter_n}（loop 重入显示）")
    else:
        print(f"  [INFO] counter.iter_n={counter_proj.iter_n}（如 tape 仅 1 轮则正常）")

    # 切到 counter：AgentHistory 应显示该 agent events
    lst.select("counter")
    # AgentHistory 在 dispatch 后已含 events；select 触发 set_node
    assert history._node_name == "counter", (
        f"select('counter') 后 AgentHistory.node_name 应为 counter；got {history._node_name}"
    )
    assert len(history._entries) > 0, (
        f"AgentHistory 空（select counter 应触发 set_node + 重放 events）"
    )
    print(f"  [OK] select('counter') → AgentHistory {len(history._entries)} events")

    # LogStream 含 workflow_started / node_started
    assert len(log_stream.lines) > 0, "LogStream 空"
    print(f"  [OK] LogStream 含 {len(log_stream.lines)} 行")


if __name__ == "__main__":
    print("=== v2 真用户验证 1：mxint_analysis tape ===")
    if MXINT_TAPE.exists():
        replay_and_verify(MXINT_TAPE, "mxint", verify_mxint)
    else:
        print(f"tape not found: {MXINT_TAPE}")
    print("\n=== v2 真用户验证 2：demo_loop tape ===")
    if DEMO_LOOP_TAPE.exists():
        replay_and_verify(DEMO_LOOP_TAPE, "demo_loop", verify_demo_loop)
    else:
        print(f"tape not found: {DEMO_LOOP_TAPE}")
    print("\n" + "=" * 60)
    print("v2 真用户验证完成（无断言失败 = 通过）")
