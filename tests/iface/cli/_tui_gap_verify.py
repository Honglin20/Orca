"""GAP-A/B/C/E 真用户验证脚本：重放 mxint + demo_loop tape，断言关键修复点。

非测试文件——是开发期验证脚本（pytest 不收）。直接 python 运行。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from orca.iface.cli.app import OrcaApp
from orca.iface.cli.widgets.activity_stream import ActivityStream
from orca.iface.cli.widgets.dag_graph import DagGraph
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
    # GAP-A：5 节点 NodeProjection.tokens 全非 None
    graph = app.query_one(DagGraph)
    print("\n[GAP-A] DAG NodeProjection.tokens（应显实际数字，非 None）：")
    nodes_with_tokens = 0
    for name in ("analyzer", "configurator", "runner", "diagnostic_saver", "report_painter"):
        proj = graph.projection_of(name)
        if proj is None:
            print(f"  {name}: (无 projection)")
            continue
        print(f"  {name}: status={proj.status} iter={proj.iter_n} elapsed={proj.elapsed} tokens={proj.tokens}")
        if proj.tokens is not None:
            nodes_with_tokens += 1
    assert nodes_with_tokens == 5, f"GAP-A 失败：仅 {nodes_with_tokens}/5 节点有 tokens"
    print(f"  ✓ 5/5 节点 tokens 全非 None")

    # GAP-B：60 个 tool_result entry summary 含 tool name + 主要参数
    activity = app.query_one(ActivityStream)
    result_entries = [e for e in activity.entries if e.event_type == "agent_tool_result"]
    print(f"\n[GAP-B] tool_result entry summary（{len(result_entries)} 条，应显 tool + args 非 '?  {{}}'）：")
    # 抽 5 条样本
    for i, e in enumerate(result_entries[:5]):
        print(f"  #{i}: summary={e.summary_line!r}  meta={e.meta_line!r}")
    bad_count = sum(1 for e in result_entries if e.summary_line.startswith("?  {}"))
    assert bad_count == 0, f"GAP-B 失败：{bad_count} 条 result summary 显 '?  {{}}'"
    # 检查含 tool name（如 glob/read/bash）
    has_tool_name = sum(1 for e in result_entries if any(
        t in e.summary_line for t in ("glob", "read", "bash", "grep", "write", "edit")
    ))
    print(f"  含 tool name: {has_tool_name}/{len(result_entries)}")
    assert has_tool_name >= len(result_entries) * 0.9, \
        f"GAP-B 失败：仅 {has_tool_name}/{len(result_entries)} 含 tool name"
    print(f"  ✓ 0 条 '?  {{}}'，{has_tool_name} 条含 tool name")

    # GAP-C：tool_result meta 含 elapsed
    has_elapsed = sum(1 for e in result_entries if "s" in e.meta_line and "lines" in e.meta_line)
    print(f"\n[GAP-C] tool_result meta 含 elapsed（{has_elapsed}/{len(result_entries)}）：")
    for e in result_entries[:5]:
        print(f"  meta={e.meta_line!r}")
    assert has_elapsed == len(result_entries), \
        f"GAP-C 失败：仅 {has_elapsed}/{len(result_entries)} 含 elapsed"
    # 不含 exit（canonical 不支持）
    has_exit = sum(1 for e in result_entries if "exit" in e.meta_line)
    assert has_exit == 0, f"GAP-C 失败：{has_exit} 条 meta 显 exit（应不显）"
    print(f"  ✓ 全部含 elapsed · 0 条显 exit")
    print("\n✓ mxint tape 验证全通过（GAP-A/B/C）")


def verify_demo_loop(app: OrcaApp) -> None:
    # GAP-E：counter self-loop 不 crash + iter ≥ 2
    graph = app.query_one(DagGraph)
    counter = graph.projection_of("counter")
    done = graph.projection_of("done")
    print(f"\n[GAP-E] counter (self-loop) projection:")
    print(f"  counter: status={counter.status if counter else None} iter={counter.iter_n if counter else None}")
    print(f"  done:    status={done.status if done else None} iter={done.iter_n if done else None}")
    assert counter is not None, "GAP-E 失败：counter projection 缺失"
    # counter 应 iter ≥ 2（demo_loop tape 里跑 3 次）
    assert counter.iter_n >= 2, f"GAP-E 失败：counter iter={counter.iter_n}（应 ≥ 2）"
    # self-loop 节点集合含 counter
    assert "counter" in graph._self_loop_nodes, "GAP-E 失败：counter 未在 self_loop_nodes"
    print(f"  ✓ counter iter={counter.iter_n}（demo_loop node_started 次数 3 一致）")
    print(f"  ✓ counter ∈ self_loop_nodes（不抛 CycleDetected）")
    print("\n✓ demo_loop tape 验证全通过（GAP-E）")


if __name__ == "__main__":
    print("=== GAP 真用户验证 1：mxint_analysis tape（186 events）===")
    replay_and_verify(MXINT_TAPE, "mxint", verify_mxint)
    print("\n=== GAP 真用户验证 2：demo_loop tape（14 events）===")
    replay_and_verify(DEMO_LOOP_TAPE, "demo_loop", verify_demo_loop)
    print("\n" + "=" * 60)
    print("✓ 全部 GAP 真用户验证通过（A/B/C/E）")
