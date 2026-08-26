#!/usr/bin/env python3
"""Build the prof-opt experiment ledger from append-only workspace state.

The ledger is the compact memory consumed by the next proposal round.  It keeps
one row per variant version and turns outcomes into deterministic next-step
hints; no free-form model or project knowledge is inferred here.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from history_lib import read_rows


def _load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def _proposals(artifacts: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    rounds_dir = artifacts / "rounds"
    if not rounds_dir.is_dir():
        return out
    for proposals_path in sorted(rounds_dir.glob("[0-9][0-9][0-9]/proposals.json")):
        data = _load_json(proposals_path)
        if not isinstance(data, dict):
            continue
        for proposal in data.get("proposals", []):
            if isinstance(proposal, dict) and proposal.get("vid"):
                out[str(proposal["vid"])] = proposal
    return out


def _latency_fields(row: dict[str, Any]) -> dict[str, Any]:
    base = row.get("base_at_proposal", {})
    base_makespan = base.get("makespan_cycles") if isinstance(base, dict) else None
    measured = row.get("makespan_cycles")
    predicted_delta = row.get("predicted_delta_cycles")
    improvement = None
    if isinstance(base_makespan, int) and isinstance(measured, int):
        improvement = base_makespan - measured
    return {
        "base_makespan_cycles": base_makespan,
        "measured_makespan_cycles": measured,
        "improvement_cycles": improvement,
        "predicted_delta_cycles": predicted_delta,
        "prediction_actual_ratio": row.get("pred_actual_ratio"),
        "latency_gate": row.get("latency_gate"),
    }


def _next_hint(row: dict[str, Any], proposal: dict[str, Any]) -> str:
    outcome = row.get("outcome")
    lever = str(proposal.get("lever", row.get("lever", "unknown")))
    if outcome == "promoted":
        return f"validated lever={lever}; compose complementary changes on this base"
    if outcome == "latency_fail":
        ratio = row.get("pred_actual_ratio")
        if isinstance(ratio, (int, float)) and ratio < 0.5:
            return (f"prediction overestimated for lever={lever}; split the change "
                    f"and re-price each site before retry")
        return f"no measured latency gain for lever={lever}; do not repeat unchanged"
    if outcome == "probe_insufficient":
        return (f"accuracy did not recover for lever={lever}; prefer a smaller "
                f"accuracy-risk variant or recover capacity elsewhere")
    if outcome == "structural_mismatch":
        return f"declaration did not match implementation for lever={lever}; fix the declaration first"
    if outcome == "variant_broken":
        return f"source template did not fit lever={lever}; use a narrower target site"
    if outcome == "unsupported_op":
        return f"profiler rejected an operator introduced by lever={lever}; choose a supported equivalent"
    if outcome == "latency_pass":
        return "latency passed; await accuracy probe before composing"
    return "no terminal outcome yet"


def build(artifacts: Path) -> dict[str, Any]:
    rows = read_rows(artifacts / "history.jsonl")
    proposals = _proposals(artifacts)
    versions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("vid"):
            versions[str(row["vid"])].append(row)

    ledger_rows: list[dict[str, Any]] = []
    for vid, vid_rows in sorted(versions.items()):
        latest = vid_rows[-1]
        proposal = proposals.get(vid, {})
        ledger_rows.append({
            "vid": vid,
            "round": latest.get("round"),
            "parent_vid": latest.get("parent_vid"),
            "change_sig": latest.get("change_sig"),
            "lever": proposal.get("lever"),
            "change_spec": proposal.get("change_spec"),
            "target_modules": latest.get("target_modules", proposal.get("target_modules")),
            "expected_accuracy_impact": proposal.get("expected_accuracy_impact"),
            "accuracy_confidence": proposal.get("accuracy_confidence"),
            "sota_refs": proposal.get("sota_ref"),
            **_latency_fields(latest),
            "proxy_acc": latest.get("proxy_acc"),
            "promote_gate": latest.get("promote_gate"),
            "outcome": latest.get("outcome"),
            "version_count": len(vid_rows),
            "next_hint": _next_hint(latest, proposal),
        })

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "variant_count": len(ledger_rows),
        "rows": ledger_rows,
    }


def _markdown(ledger: dict[str, Any]) -> str:
    lines = [
        "# Prof-Opt Experiment Ledger", "",
        f"Generated: `{ledger['generated_at']}` · Variants: `{ledger['variant_count']}`", "",
        "| VID | Round | Lever | Outcome | Δcycles | Measured Δ | Ratio | Proxy | Next hint |",
        "|---|---:|---|---|---:|---:|---:|---:|---|",
    ]
    for row in ledger["rows"]:
        lines.append(
            f"| `{row['vid']}` | {row.get('round', '')} | {row.get('lever') or ''} | "
            f"{row.get('outcome') or ''} | {row.get('predicted_delta_cycles', '')} | "
            f"{row.get('improvement_cycles', '')} | {row.get('prediction_actual_ratio', '')} | "
            f"{row.get('proxy_acc', '')} | {row['next_hint']} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", required=True)
    ns = ap.parse_args()
    try:
        ledger = build(Path(ns.artifacts))
    except (OSError, ValueError) as exc:
        print(f"experiment_ledger: FAIL {exc}", file=sys.stderr)
        return 2
    root = Path(ns.artifacts)
    (root / "experiment_ledger.json").write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "experiment_summary.md").write_text(_markdown(ledger), encoding="utf-8")
    print(json.dumps({
        "ledger": str(root / "experiment_ledger.json"),
        "summary": str(root / "experiment_summary.md"),
        "variant_count": ledger["variant_count"],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
