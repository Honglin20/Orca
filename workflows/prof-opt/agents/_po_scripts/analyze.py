#!/usr/bin/env python3
"""analyze.py — profile dir -> bottleneck_report.json.

Reads the four contract artifacts (PROFILER_CONTRACT.md), validates them
STRICTLY (unknown key -> fail loud; the four files must also agree with each
other), and produces the bottleneck report consumed by proposal generation:

    makespan_cycles       headline latency
    pipeline_breakdown    per actual taskgraph pipeline value
    critical_path         longest latency path through depends_on
    hot_patterns          critical-path ops clustered by repeated op_type
    cost_table            op_type x shape-class buckets -> cycles

Default output: <profile_dir>/../bottleneck_report.json (the pinned landing
spot next to base/profile/). stdout: single-line JSON summary.

--freeze-origin additionally writes <profile_dir>/../origin_anchor.json
(the immutable dual anchor: baseline makespan + target line + accuracy
budget), write-if-absent: an existing file with field-identical content is a
no-op; any difference exits 2 (the anchor never drifts — changing the line
or budget requires rebuilding the workspace). Without --freeze-origin the
anchor file is never touched.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

SCHEMA_VERSION = 1

TASKGRAPH_KEYS = {"schema_version", "onnx", "operators"}
OPERATOR_KEYS = {"name", "op_type", "task_id", "pipeline", "latency",
                 "depends_on", "output_memory", "output_dimensions", "onnx_nodes"}
SCHEDULE_KEYS = {"schema_version", "makespan_cycles", "assignments"}
ASSIGNMENT_KEYS = {"task_id", "operator", "pipeline", "start_cycle", "end_cycle"}
SUMMARY_KEYS = {"schema_version", "onnx", "makespan_cycles", "op_count"}
OPS_CSV_COLUMNS = ["name", "op_type", "task_id", "pipeline", "latency",
                   "depends_on", "output_memory", "output_dimensions", "onnx_nodes"]

# shape-class bucket edges over output element counts (op_type x class bucket)
SHAPE_EDGES = [0, 100, 10_000, 1_000_000, 100_000_000, float("inf")]
SHAPE_LABELS = ["<1e2", "1e2-1e4", "1e4-1e6", "1e6-1e8", ">=1e8"]


class ContractError(RuntimeError):
    """Strict-schema violation in the profile artifacts."""


def _load_json_strict(path: Path, allowed: set[str]) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"missing artifact: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError(f"{path} must be a JSON object")
    unknown = set(data) - allowed
    if unknown:
        raise ContractError(f"{path} has unknown keys {sorted(unknown)} "
                            f"(allowed: {sorted(allowed)})")
    return data


def _load_profile(profile_dir: Path) -> dict:
    taskgraph = _load_json_strict(profile_dir / "taskgraph.json", TASKGRAPH_KEYS)
    schedule = _load_json_strict(profile_dir / "schedule.json", SCHEDULE_KEYS)
    summary = _load_json_strict(profile_dir / "profile_summary.json", SUMMARY_KEYS)

    for i, op in enumerate(taskgraph["operators"]):
        unknown = set(op) - OPERATOR_KEYS
        if unknown:
            raise ContractError(f"taskgraph.operators[{i}] has unknown keys "
                                f"{sorted(unknown)} (allowed: {sorted(OPERATOR_KEYS)})")
    for i, a in enumerate(schedule["assignments"]):
        unknown = set(a) - ASSIGNMENT_KEYS
        if unknown:
            raise ContractError(f"schedule.assignments[{i}] has unknown keys "
                                f"{sorted(unknown)} (allowed: {sorted(ASSIGNMENT_KEYS)})")

    # cross-artifact consistency: the four files must describe one profile
    ops = taskgraph["operators"]
    if len(ops) != summary["op_count"]:
        raise ContractError(f"op_count {summary['op_count']} != operators {len(ops)}")
    if schedule["makespan_cycles"] != summary["makespan_cycles"]:
        raise ContractError(f"makespan mismatch: schedule "
                            f"{schedule['makespan_cycles']} vs summary "
                            f"{summary['makespan_cycles']}")
    task_ids = {op["task_id"] for op in ops}
    if len(task_ids) != len(ops):
        raise ContractError("duplicate task_id in taskgraph")
    if {a["task_id"] for a in schedule["assignments"]} != task_ids:
        raise ContractError("schedule assignments do not cover taskgraph exactly")

    with open(profile_dir / "ops.csv", newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    if not rows or rows[0] != OPS_CSV_COLUMNS:
        raise ContractError(f"ops.csv header must be exactly {OPS_CSV_COLUMNS}, "
                            f"got {rows[0] if rows else 'empty file'}")
    if len(rows) - 1 != len(ops):
        raise ContractError(f"ops.csv has {len(rows) - 1} data rows, "
                            f"taskgraph has {len(ops)} operators")

    return {"taskgraph": taskgraph, "schedule": schedule, "summary": summary}


def _critical_path(ops: list[dict]) -> list[str]:
    """Longest latency path (by summed operator latency) through depends_on.

    Iterative Kahn-style DP: no recursion limits on deep graphs, and a
    dependency cycle is reported as a ContractError instead of a stack
    overflow."""
    by_name = {op["name"]: op for op in ops}
    for op in ops:
        for dep in op["depends_on"]:
            if dep not in by_name:
                raise ContractError(f"operator {op['name']!r} depends on "
                                    f"unknown {dep!r}")

    dependents: dict[str, list[str]] = {name: [] for name in by_name}
    indegree: dict[str, int] = {name: 0 for name in by_name}
    for op in ops:
        for dep in op["depends_on"]:
            dependents[dep].append(op["name"])
            indegree[op["name"]] += 1

    best_cost = {name: 0 for name in by_name}
    best_prev: dict[str, str | None] = {name: None for name in by_name}
    queue = [name for name in sorted(by_name) if indegree[name] == 0]
    processed = 0
    while queue:
        name = queue.pop()
        processed += 1
        best_cost[name] += by_name[name]["latency"]
        for succ in dependents[name]:
            if best_cost[name] > best_cost[succ]:
                best_cost[succ] = best_cost[name]
                best_prev[succ] = name
            indegree[succ] -= 1
            if indegree[succ] == 0:
                queue.append(succ)
    if processed != len(by_name):
        cyclic = sorted(n for n, d in indegree.items() if d > 0)
        raise ContractError(f"taskgraph has a dependency cycle involving {cyclic}")

    sink = max(sorted(by_name), key=lambda n: best_cost[n])
    path = []
    cur: str | None = sink
    while cur is not None:
        path.append(cur)
        cur = best_prev[cur]
    return list(reversed(path))


def _shape_class(elements: int) -> str:
    for label, lo, hi in zip(SHAPE_LABELS, SHAPE_EDGES, SHAPE_EDGES[1:]):
        if lo <= elements < hi:
            return label
    return SHAPE_LABELS[-1]


def analyze(profile_dir: Path) -> dict:
    data = _load_profile(profile_dir)
    ops = data["taskgraph"]["operators"]
    schedule = data["schedule"]
    makespan = data["summary"]["makespan_cycles"]

    # schedule lookups (assignment keyed by task_id)
    sched_by_task = {a["task_id"]: a for a in schedule["assignments"]}

    # pipeline breakdown, grouped by the ACTUAL pipeline values in taskgraph
    pipelines: dict[str, dict] = {}
    for op in ops:
        entry = pipelines.setdefault(op["pipeline"], {"op_count": 0, "total_cycles": 0})
        entry["op_count"] += 1
        entry["total_cycles"] += op["latency"]
    pipeline_breakdown = [
        {"pipeline": p, **v, "share": round(v["total_cycles"] / makespan, 6) if makespan else 0.0}
        for p, v in sorted(pipelines.items(), key=lambda kv: (-kv[1]["total_cycles"], kv[0]))
    ]

    # critical path (extracted from depends_on; timing cross-read from schedule)
    path_names = _critical_path(ops)
    by_name = {op["name"]: op for op in ops}
    critical_path = []
    for name in path_names:
        op = by_name[name]
        a = sched_by_task[op["task_id"]]
        critical_path.append({
            "name": name, "op_type": op["op_type"], "pipeline": op["pipeline"],
            "latency": op["latency"],
            "start_cycle": a["start_cycle"], "end_cycle": a["end_cycle"],
        })
    critical_cycles = sum(op["latency"] for op in critical_path)

    # hot patterns: critical-path ops clustered by repeated op_type signature
    clusters: dict[str, dict] = {}
    for step in critical_path:
        c = clusters.setdefault(step["op_type"], {
            "op_type": step["op_type"], "count": 0, "total_cycles": 0,
            "onnx_nodes": [], "task_ids": [],
        })
        c["count"] += 1
        c["total_cycles"] += step["latency"]
        c["onnx_nodes"].extend(by_name[step["name"]]["onnx_nodes"])
        c["task_ids"].append(by_name[step["name"]]["task_id"])
    hot_patterns = []
    for rank, (op_type, c) in enumerate(
            sorted(clusters.items(), key=lambda kv: (-kv[1]["total_cycles"], kv[0])), 1):
        hot_patterns.append({
            "pattern_id": f"P{rank}", "op_type": op_type,
            "count": c["count"], "total_cycles": c["total_cycles"],
            "share": round(c["total_cycles"] / critical_cycles, 6) if critical_cycles else 0.0,
            "onnx_nodes": sorted(c["onnx_nodes"]), "task_ids": sorted(c["task_ids"]),
        })

    # cost table: op_type x shape-class buckets over ALL operators
    buckets: dict[tuple[str, str], dict] = {}
    for op in ops:
        elements = 1
        for d in op["output_dimensions"]:
            elements *= int(d)
        key = (op["op_type"], _shape_class(elements))
        b = buckets.setdefault(key, {"op_type": op["op_type"], "shape_class": key[1],
                                     "count": 0, "cycles_sum": 0,
                                     "min_cycles": op["latency"], "max_cycles": op["latency"]})
        b["count"] += 1
        b["cycles_sum"] += op["latency"]
        b["min_cycles"] = min(b["min_cycles"], op["latency"])
        b["max_cycles"] = max(b["max_cycles"], op["latency"])
    cost_table = [
        {"op_type": b["op_type"], "shape_class": b["shape_class"], "count": b["count"],
         "mean_cycles": b["cycles_sum"] // b["count"],
         "min_cycles": b["min_cycles"], "max_cycles": b["max_cycles"]}
        for _, b in sorted(buckets.items())
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "profile_dir": str(profile_dir.resolve()),
        "onnx": data["summary"]["onnx"],
        "makespan_cycles": makespan,
        "critical_path_cycles": critical_cycles,
        "pipeline_breakdown": pipeline_breakdown,
        "critical_path": critical_path,
        "hot_patterns": hot_patterns,
        "cost_table": cost_table,
    }


def freeze_origin(anchor_path: Path, baseline_makespan: int,
                  latency_reduction_min: float, accuracy_budget: float) -> str:
    """Write the immutable origin anchor, write-if-absent.

    Returns a short status string for the stdout summary. An existing anchor
    with field-identical content is a no-op; any difference raises
    ContractError (the anchor is immutable by contract)."""
    payload = {
        "baseline_makespan_cycles": int(baseline_makespan),
        "latency_reduction_min": float(latency_reduction_min),
        "accuracy_budget": float(accuracy_budget),
        "target_cycles": int(baseline_makespan * (1.0 - latency_reduction_min)) + 1,
        "frozen_at_round": 0,
    }
    if anchor_path.is_file():
        try:
            existing = json.loads(anchor_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ContractError(
                f"origin anchor exists but is unparseable: {anchor_path} "
                f"({exc})") from exc
        if existing != payload:
            raise ContractError(
                f"origin anchor {anchor_path} is IMMUTABLE and its content "
                f"differs from this request (existing {existing} vs "
                f"requested {payload}); origin 锚不可变——修改达标线/精度预算"
                f"需 fresh_start 重建工作区")
        return "origin_anchor: already frozen (identical), no-op"
    anchor_path.parent.mkdir(parents=True, exist_ok=True)
    anchor_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return f"origin_anchor: frozen at {anchor_path}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile-dir", required=True)
    ap.add_argument("--out", default=None,
                    help="output path (default: <profile-dir>/../bottleneck_report.json)")
    ap.add_argument("--freeze-origin", action="store_true",
                    help="also write ../origin_anchor.json (write-if-absent)")
    ap.add_argument("--latency-reduction-min", type=float, default=None,
                    help="latency line ratio in (0, 1); required with --freeze-origin")
    ap.add_argument("--accuracy-budget", type=float, default=None,
                    help="accuracy budget >= 0; required with --freeze-origin")
    ns = ap.parse_args()

    if ns.freeze_origin:
        if ns.latency_reduction_min is None or ns.accuracy_budget is None:
            print("analyze: --freeze-origin requires --latency-reduction-min "
                  "and --accuracy-budget", file=sys.stderr)
            return 2
        if not 0.0 < ns.latency_reduction_min < 1.0:
            print(f"analyze: --latency-reduction-min must be in (0, 1), got "
                  f"{ns.latency_reduction_min}", file=sys.stderr)
            return 2
        if not float(ns.accuracy_budget) >= 0:
            print(f"analyze: --accuracy-budget must be >= 0, got "
                  f"{ns.accuracy_budget}", file=sys.stderr)
            return 2

    profile_dir = Path(ns.profile_dir)
    out = Path(ns.out) if ns.out else profile_dir.parent / "bottleneck_report.json"
    try:
        report = analyze(profile_dir)
        anchor_status = ""
        if ns.freeze_origin:
            anchor_status = freeze_origin(
                profile_dir.parent / "origin_anchor.json",
                report["makespan_cycles"],
                ns.latency_reduction_min, ns.accuracy_budget)
    except ContractError as exc:
        print(f"analyze: contract violation: {exc}", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        "report": str(out.resolve()),
        "makespan_cycles": report["makespan_cycles"],
        "hot_patterns": len(report["hot_patterns"]),
        **({"origin_anchor": anchor_status} if anchor_status else {}),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
