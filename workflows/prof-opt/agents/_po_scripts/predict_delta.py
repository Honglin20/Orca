#!/usr/bin/env python3
"""predict_delta.py — op_delta x cost_table -> predicted_delta_cycles.

Pure deterministic function of its inputs (same input -> byte-identical
output); this is the single source for BOTH the admission number and the
normalized change_sig params (LLM hand-writing params is forbidden).

    --report    bottleneck_report.json (carries cost_table)
    --op-delta  {"Erf": -4, "Relu": +4, ...} (inline JSON or @file)
    --sites     optional per-site shape classes, inline JSON or @file:
                {"Softmax": ["1e2-1e4", ...], "Relu": ["<1e2", ...]} — one
                entry per affected op instance (len == abs(op_delta)),
                classes as they appear in the cost_table's shape_class labels

Pricing is PER SHAPE-CLASS ROW, never a whole-model per-op average: the
cost_table already buckets every op_type by output-shape class, and a
change that removes N small sites must be priced at the small rows — a
count-weighted mean over all sites of that op_type drowns small sites in
big-site costs (the E2E softmax->relu regression predicted exactly 0 that
way). Per op_type in the op delta:

    sites given       each affected site is priced at the mean_cycles of
                      its (op_type, shape_class) row
    sites not given   every site is priced at the SUM of all shape-class
                      rows of that op_type (the worst-case bound — exact
                      when the op_type has a single shape class, the
                      common case; over-priced otherwise, which pushes the
                      caller to pass --sites)

An op_type absent from the table (nothing similar in the base model) is
NOT guessed: pass an explicit --added-cost Op=cycles override or the
script fails loud — a made up number would silently corrupt admission. The
override also prices a site whose declared shape class has no row for an
otherwise present op_type.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


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


def _load_sites(raw: str | None) -> dict[str, list[str]] | None:
    if raw is None:
        return None
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--sites is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not all(
            isinstance(v, list) and all(isinstance(c, str) for c in v)
            for v in data.values()):
        raise ValueError("--sites must be an object of "
                         "{op_type: [shape_class, ...]}")
    return data


def _rows_by_op(cost_table: list[dict]) -> dict[str, dict[str, float]]:
    rows: dict[str, dict[str, float]] = {}
    for r in cost_table:
        rows.setdefault(r["op_type"], {})[r["shape_class"]] = \
            float(r["mean_cycles"])
    return rows


def _per_site_costs(rows_by_op: dict[str, dict[str, float]], op: str,
                    delta_n: int, site_classes: list[str] | None,
                    added_costs: dict[str, float]) -> tuple[list[float], list[str], str]:
    """Price every affected op instance. Returns (per_site_costs, classes
    consulted, source). Removed and added sites are priced identically —
    both are real graph work the change adds or removes."""
    op_rows = rows_by_op.get(op)

    if op_rows is None:
        if op not in added_costs:
            raise ValueError(
                f"op_type {op!r} not in cost_table and no --added-cost override — "
                f"refusing to guess a cost for an op the base model never runs")
        n = abs(delta_n)
        return ([float(added_costs[op])] * n, ["<override>"], "override")

    if site_classes is None:
        # shape info unobtainable: worst-case bound = the sum of every
        # shape-class row of this op_type (exact for a single-class op_type)
        return ([sum(op_rows.values())] * abs(delta_n),
                sorted(op_rows), "cost_table:all-shapes")

    if len(site_classes) != abs(delta_n):
        raise ValueError(
            f"--sites[{op!r}] lists {len(site_classes)} shape classes but the "
            f"op delta is {delta_n:+d} ({abs(delta_n)} sites) — one class per "
            f"affected op instance is required")

    costs: list[float] = []
    for cls in site_classes:
        if cls in op_rows:
            costs.append(op_rows[cls])
        elif op in added_costs:
            costs.append(float(added_costs[op]))
        else:
            raise ValueError(
                f"op_type {op!r} has cost_table rows but none in shape class "
                f"{cls!r} (has {sorted(op_rows)}) — the declared site class "
                f"disagrees with the profile; fix the class or pass "
                f"--added-cost {op}=<cycles>")
    return costs, sorted(set(site_classes)), "cost_table:by-site"


def predict_delta(report: dict, op_delta: dict[str, int],
                  added_costs: dict[str, float],
                  sites: dict[str, list[str]] | None = None) -> dict:
    rows_by_op = _rows_by_op(report.get("cost_table", []))
    if sites:
        unknown = sorted(set(sites) - set(op_delta))
        if unknown:
            raise ValueError(f"--sites carries op types not in the op delta: "
                             f"{unknown}")

    basis = []
    total = 0
    for op in sorted(op_delta):
        delta_n = op_delta[op]
        site_classes = sites.get(op) if sites else None
        per_site, classes, source = _per_site_costs(
            rows_by_op, op, delta_n, site_classes, added_costs)
        contribution = int(round(delta_n * (sum(per_site) / len(per_site))))
        total += contribution
        basis.append({"op_type": op, "delta": delta_n,
                      "site_classes": classes,
                      "per_site_cycles": per_site,
                      "contribution": contribution, "source": source})

    # canonical params for change_sig: sorted, signed, ';' -joined
    params = ";".join(f"{op}{op_delta[op]:+d}" for op in sorted(op_delta))
    return {"predicted_delta_cycles": total, "params": params, "basis": basis}


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
    ap.add_argument("--sites", default=None, metavar="JSON",
                    help='optional per-site shape classes, inline JSON or '
                         '@file, e.g. \'{"Softmax":["1e2-1e4"],"Relu":["1e2-1e4"]}\' '
                         '— len per op == abs(op_delta); omit only when the '
                         'site shapes are genuinely unobtainable')
    ap.add_argument("--added-cost", action="append", default=[], metavar="OP=CYCLES",
                    help="per-op cost override for op types absent from the table")
    ns = ap.parse_args()

    try:
        report = json.loads(Path(ns.report).read_text(encoding="utf-8"))
        op_delta = _load_op_delta(ns.op_delta)
        sites = _load_sites(ns.sites)
        added = {}
        for pair in ns.added_cost:
            if "=" not in pair:
                raise ValueError(f"--added-cost expects OP=CYCLES, got {pair!r}")
            op, val = pair.split("=", 1)
            added[op] = float(val)
        result = predict_delta(report, op_delta, added, sites)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"predict_delta: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
