"""Standalone: drive opencode TUI run + inject charts + save SVG/PNG snapshot.

Run: python tests/e2e_phase12/capture_snapshot.py
Not a pytest test — just produces the visual artifact for the report.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orca.compile import load_workflow
from orca.iface.cli.app import OrcaApp
from orca.iface.cli.widgets.dag_graph import DagGraph
from orca.iface.cli.widgets.node_detail import NodeDetail

ART = Path(__file__).parent / "_artifacts"
ART.mkdir(exist_ok=True)


def _payload(label, title, ctype="line"):
    return {
        "chart_type": ctype, "label": label, "title": title,
        "data": [{"x": i, "y": v} for i, v in enumerate([5, 3, 4, 2, 1], start=1)],
        "x": "x", "y": "y",
    }


async def main():
    wf = load_workflow(Path(__file__).parent / "opencode_tui_workflow.yaml")
    tape = ART / "tape.jsonl"
    app = OrcaApp(wf=wf, tape_path=tape)

    async def inject():
        for _ in range(300):
            try:
                if app.query_one(DagGraph).status_of_node("analyst") == "running":
                    break
            except Exception:
                pass
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.4)
        try:
            app.query_one(DagGraph).select("reporter")
        except Exception:
            pass
        for p, n in [
            (_payload("training", "loss"), "reporter"),
            (_payload("training", "acc"), "reporter"),
            (_payload("eval", "f1", "bar"), "reporter"),
            (_payload("eval", "precision", "bar"), "reporter"),
            (_payload("wf_summary", "elapsed"), None),
        ]:
            await app.bus.emit("custom", {"kind": "chart", "chart": p}, node=n)

    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause(0.3)
        inj = asyncio.ensure_future(inject())
        for _ in range(750):
            if app.terminal_state is not None:
                break
            await pilot.pause(0.2)
        try:
            await asyncio.wait_for(inj, timeout=5)
        except asyncio.TimeoutError:
            pass
        await pilot.pause(0.5)

        # Focus reporter + charts tab for the screenshot.
        app.query_one(DagGraph).select("reporter")
        await pilot.pause(0.1)
        cp = app.query_one(NodeDetail)._chart_panel
        cp._focus = ("reporter", "training", "loss")
        cp._rerender()
        app.action_focus_charts()
        await pilot.pause(0.4)

        svg = ART / "phase12_opencode_e2e.svg"
        try:
            path = app.save_screenshot(filename=str(svg))
            print(f"SVG saved: {path}")
        except Exception as e:
            print(f"save_screenshot(svg) failed: {e!r}")
        try:
            rendered = app.export_screenshot(title="phase12 opencode e2e")
            png = ART / "phase12_opencode_e2e.svg.png"
            png.write_text(rendered if isinstance(rendered, str) else str(rendered))
            print(f"export_screenshot produced {len(rendered) if isinstance(rendered,str) else 'n/a'} chars")
        except Exception as e:
            print(f"export_screenshot failed: {e!r}")
        print("terminal_state:", app.terminal_state.status if app.terminal_state else None)
        # Chart count summary
        charts = dict(app.query_one(NodeDetail).all_charts())
        print("all_charts buckets:", list(charts.keys()))
        print("reporter charts:", {k: len(v) for k, v in app.query_one(NodeDetail)._chart_panel.charts_for("reporter").items()})


asyncio.run(main())
