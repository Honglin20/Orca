#!/usr/bin/env python3
"""predict_delta.py — op_delta x cost_table -> predicted_delta_cycles (v7 §5.2).

Pure deterministic function of its inputs (same input -> byte-identical
output); this is the single source for BOTH the admission number and the
normalized change_sig params (LLM hand-writing params is forbidden).

    --report    bottleneck_report.json (carries cost_table + critical_path)
    --op-delta  {"Erf": -4, "Relu": +4, ...} (inline JSON or @file)
    --nodes     the affected taskgraph operator NAMES (inline JSON or @file)
                — the REMOVED sites; each must exist in
                <report-dir>/taskgraph.json by name. The predictor derives
                each site's shape class from that operator's
                output_dimensions itself (v7: the --sites hand-binning mode
                is deleted — the LLM never hand-computes element counts).
    --added-cost OP=cycles  per-op cost override for ADDED op types absent
                from the cost table (never guessed)

Pricing is PER SITE, never a whole-model per-op average: the cost_table
already buckets every op_type by output-shape class, and a change that
removes N small sites must be priced at the small rows. Per op_type in the
op delta:

    removed sites (--nodes)   each priced at the mean_cycles of its
                              (op_type, shape_class) row, derived from
                              taskgraph.json output_dimensions
    added sites (op_delta > 0) priced at the --added-cost override (an
                              added op has no taskgraph node yet); an added
                              op without an override fails loud

Critical-path weighting (v7 §5.2): a site whose taskgraph node is on the
report's `critical_path` contributes at weight 1.0; any other site
contributes at weight 0.25 (heuristic — off-path work overlaps other work
on the schedule). Added sites carry no path information and are weighted
1.0 (conservative). The split {on_path_cycles, off_path_cycles_weighted}
and the weights used are disclosed in the stdout basis.

An op_type absent from the table (nothing similar in the base model) is
NOT guessed: pass an explicit --added-cost override or the script fails
loud — a made up number would silently corrupt admission.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ON_PATH_WEIGHT = 1.0
OFF_PATH_WEIGHT = 0.25


def _load_op_delta(raw: str) -> dict[str, int]:
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--op-delta is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not all(
            isinstance(v, int) for v in data.values()):
        raise ValueError("--op-delta must be an object of {op_type: int delta}")
    if any(v == 0 for v in data.values()):
        raise ValueError("--op-delta values must be non-zero (+add / -remove)")
    return data


def _load_nodes(raw: str) -> list[str]:
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--nodes is not valid JSON: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(n, str) for n in data):
        raise ValueError("--nodes must be a list of taskgraph operator names")
    if len(set(data)) != len(data):
        raise ValueError("--nodes carries duplicate operator names")
    return data


def _rows_by_op(cost_table: list[dict]) -> dict[str, dict[str, float]]:
    rows: dict[str, dict[str, float]] = {}
    for r in cost_table:
        rows.setdefault(r["op_type"], {})[r["shape_class"]] = \
            float(r["mean_cycles"])
    return rows


def _load_taskgraph(report: dict, nodes: list[str]) -> dict[str, dict]:
    """taskgraph.json operator rows keyed by name, for exactly the requested
    nodes (shape-class derivation source; v7 — never an LLM hand count)."""
    tg_path = Path(report.get("profile_dir", ".")) / "taskgraph.json"
    try:
        tg = json.loads(tg_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(
            f"taskgraph.json not found at {tg_path} (expected next to the "
            f"report's profile_dir — the shape-class derivation source)"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"taskgraph.json unparseable: {tg_path} ({exc})") from exc
    by_name: dict[str, dict] = {}
    for op in tg.get("operators", []):
        if isinstance(op, dict) and isinstance(op.get("name"), str):
            by_name[op["name"]] = op
    missing = [n for n in nodes if n not in by_name]
    if missing:
        raise ValueError(
            f"--nodes names operators absent from taskgraph.json: {missing} "
            f"(looked in {tg_path}) — name the actual taskgraph operator "
            f"names, never approximate")
    return {n: by_name[n] for n in nodes}


def _shape_class(elements: int) -> str:
    # same bucketing as analyze.py (single source of the class labels)
    from analyze import SHAPE_EDGES, SHAPE_LABELS
    for label, lo, hi in zip(SHAPE_LABELS, SHAPE_EDGES, SHAPE_EDGES[1:]):
        if lo <= elements < hi:
            return label
    return SHAPE_LABELS[-1]


def _removed_site_costs(report: dict, op: str, op_rows: dict[str, float],
                        sites: list[dict], added_costs: dict[str, float],
                        basis_rows: list[dict]) -> None:
    """Price the removed sites of one op_type at their derived shape class,
    weighted on/off the critical path (v7 §5.2)."""
    on_path = report.get("critical_path") or []
    on_path_names = {step.get("name") for step in on_path
                     if isinstance(step, dict)}
    for site in sites:
        if site["op_type"] != op:
            continue
        cls = site["shape_class"]
        if op_rows and cls in op_rows:
            raw_cost = op_rows[cls]
        elif op in added_costs:
            raw_cost = float(added_costs[op])
            cls = "<override>"
        elif not op_rows:
            raise ValueError(
                f"op_type {op!r} not in cost_table and no --added-cost "
                f"override — refusing to guess a cost for an op the base "
                f"model never runs")
        else:
            raise ValueError(
                f"op_type {op!r} has cost_table rows but node "
                f"{site['node']!r} derives shape class {cls!r} with no row "
                f"(has {sorted(op_rows)}) — pass --added-cost {op}=<cycles> "
                f"derived from the closest same-class row")
        weight = (ON_PATH_WEIGHT if site["node"] in on_path_names
                  else OFF_PATH_WEIGHT)
        site["raw_cycles"] = raw_cost
        site["weight"] = weight
        site["weighted_cycles"] = raw_cost * weight
        site["on_critical_path"] = site["node"] in on_path_names
        basis_rows.append(site)


def predict_delta(report: dict, op_delta: dict[str, int],
                  added_costs: dict[str, float],
                  nodes: list[str] | None = None) -> dict:
    rows_by_op = _rows_by_op(report.get("cost_table", []))
    node_names = nodes or []
    if len(set(node_names)) != len(node_names):
        raise ValueError("--nodes carries duplicate operator names")
    removed_ops = {op for op, d in op_delta.items() if d < 0}
    added_ops = {op for op, d in op_delta.items() if d > 0}

    # every removed site must be a named taskgraph node of the right op_type
    tg = _load_taskgraph(report, node_names) if removed_ops else {}
    sites: list[dict] = []
    for name, op in tg.items():
        elements = 1
        for d in op.get("output_dimensions", []):
            elements *= int(d)
        sites.append({"node": name, "op_type": op.get("op_type"),
                      "shape_class": _shape_class(elements)})
    by_op_sites: dict[str, list[dict]] = {}
    for s in sites:
        by_op_sites.setdefault(s["op_type"], []).append(s)

    basis: list[dict] = []
    on_path_cycles = 0.0
    off_path_weighted = 0.0
    total = 0

    for op in sorted(op_delta):
        delta_n = op_delta[op]
        if delta_n < 0:
            got = by_op_sites.get(op, [])
            if len(got) != abs(delta_n):
                known = [s["node"] for s in sites]
                raise ValueError(
                    f"--nodes lists {len(got)} operator(s) of op_type {op!r} "
                    f"but the op delta is {delta_n:+d} ({abs(delta_n)} "
                    f"removed sites) — name every removed site (all named "
                    f"nodes: {known})")
            rows: list[dict] = []
            _removed_site_costs(report, op, rows_by_op.get(op, {}), got,
                                added_costs, rows)
            # delta_n < 0 (removal): the weighted site sum leaves the makespan
            contribution = int(round(-sum(r["weighted_cycles"] for r in rows)))
            for r in rows:
                if r["on_critical_path"]:
                    on_path_cycles += r["weighted_cycles"]
                else:
                    off_path_weighted += r["weighted_cycles"]
            basis.append({"op_type": op, "delta": delta_n,
                          "site_classes": sorted({r["shape_class"] for r in rows}),
                          "sites": [{k: r[k] for k in
                                     ("node", "shape_class", "raw_cycles",
                                      "weight", "weighted_cycles",
                                      "on_critical_path")} for r in rows],
                          "contribution": contribution,
                          "source": "cost_table:by-node"})
        else:
            # added sites: no taskgraph node exists yet — an explicit
            # override is the only honest price, weighted 1.0 (conservative:
            # added work has no path information)
            if op not in added_costs:
                raise ValueError(
                    f"op_type {op!r} is ADDED (+{delta_n}) and absent from "
                    f"cost_table — pass an explicit --added-cost {op}=<cycles> "
                    f"(derived from the closest same-class row and recorded "
                    f"in prediction_basis); refusing to guess")
            per_site = float(added_costs[op])
            # one dict per site (never a shared alias: a later per-site
            # annotation would otherwise mutate every twin at once)
            rows = [{"node": None, "shape_class": "<override>",
                     "raw_cycles": per_site, "weight": ON_PATH_WEIGHT,
                     "weighted_cycles": per_site * ON_PATH_WEIGHT,
                     "on_critical_path": None}
                    for _ in range(abs(delta_n))]
            contribution = int(round(sum(r["weighted_cycles"] for r in rows)))
            on_path_cycles += sum(r["weighted_cycles"] for r in rows)
            basis.append({"op_type": op, "delta": delta_n,
                          "site_classes": ["<override>"],
                          "sites": rows, "contribution": contribution,
                          "source": "override:added"})
        total += basis[-1]["contribution"]

    # canonical params for change_sig: sorted, signed, ';' -joined
    params = ";".join(f"{op}{op_delta[op]:+d}" for op in sorted(op_delta))
    return {"predicted_delta_cycles": total, "params": params,
            "on_path_cycles": round(on_path_cycles, 6),
            "off_path_cycles_weighted": round(off_path_weighted, 6),
            "weights": {"on_critical_path": ON_PATH_WEIGHT,
                        "off_critical_path": OFF_PATH_WEIGHT,
                        "added_sites": ON_PATH_WEIGHT},
            "basis": basis}


def build_change_sig(lever: str, params: str, modules: list[str]) -> str:
    """Canonical change signature: ``lever:params:modules_canonical``.

    Dedup is exact-string, so the canonical form MUST be machine-built: params
    come from predict_delta, modules are sorted then comma-joined here. A
    hand-ordered module list would silently dodge the permanent-dedup set and
    the joint retry budget.
    """
    modules_canonical = ",".join(sorted(m for m in modules if m))
    if not lever or not params or not modules_canonical:
        raise ValueError("build_change_sig needs non-empty lever, params and modules")
    return f"{lever}:{params}:{modules_canonical}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--report", required=True, help="bottleneck_report.json")
    ap.add_argument("--op-delta", required=True, metavar="JSON",
                    help='inline JSON or @file, e.g. \'{"Erf":-4,"Relu":4}\'')
    ap.add_argument("--nodes", default=None, metavar="JSON",
                    help='the affected taskgraph operator names (the removed '
                         'sites), inline JSON or @file, e.g. '
                         '\'["op_12","op_13"]\' — one per removed instance '
                         '(len per op == abs(negative op_delta)); the '
                         'predictor derives each site\'s shape class from '
                         'taskgraph.json itself')
    ap.add_argument("--added-cost", action="append", default=[], metavar="OP=CYCLES",
                    help="per-op cost override for ADDED op types absent "
                         "from the table")
    ns = ap.parse_args()

    try:
        report = json.loads(Path(ns.report).read_text(encoding="utf-8"))
        op_delta = _load_op_delta(ns.op_delta)
        nodes = _load_nodes(ns.nodes) if ns.nodes is not None else None
        added = {}
        for pair in ns.added_cost:
            if "=" not in pair:
                raise ValueError(f"--added-cost expects OP=CYCLES, got {pair!r}")
            op, val = pair.split("=", 1)
            added[op] = float(val)
        result = predict_delta(report, op_delta, added, nodes)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"predict_delta: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
