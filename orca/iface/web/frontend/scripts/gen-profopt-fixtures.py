#!/usr/bin/env python3
"""gen-profopt-fixtures.py — regenerate the W-P3 vitest fixtures from a REAL
push_curves.py run (web SPEC §6.3-1: the front-end integration tests consume
the pusher's ACTUAL payload shapes — never hand-written guesses).

Builds a synthetic prof-opt v6 artifacts workspace, runs ``push_curves.main()``
twice — live, then ``(final)`` after the workspace advances (a new round dir +
one more terminal variant, exactly the shapes the watchdog/report would leave
behind) — capturing every socket send in-process (``_push_best_effort`` is
replaced by a recorder, so the payloads are byte-identical to what the chart
socket would receive), and writes::

    test/fixtures/profopt-push-curves.json
    {"live":  {"line": …, "pareto": …, "docs": …},
     "final": {"line": …, "pareto": …, "docs": …}}

Coverage intent of the synthetic workspace: baseline curve + success variant +
in-flight variant (metric-fallback y) + 达线未训 variant (y=null placeholder)
+ eliminated latency_fail variant + rounds + rules snapshot — every §10.2
status color and every §10.4 row group rides along in the captured payloads.

Usage (WSL, repo root):
    .venv/bin/python orca/iface/web/frontend/scripts/gen-profopt-fixtures.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[5]
_SCRIPTS = _REPO / "workflows" / "prof-opt" / "agents" / "_po_scripts"
sys.path.insert(0, str(_SCRIPTS))

import push_curves  # noqa: E402

_OUT = Path(__file__).resolve().parents[1] / "test" / "fixtures" / \
    "profopt-push-curves.json"


def _write(art: Path, rel: str, text: str) -> None:
    target = art / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _json(obj) -> str:
    return json.dumps(obj) + "\n"


def _variant(art: Path, vid: str, *, curve=None, train_status=None,
             shard=None, verdict=None, docs=()) -> None:
    vdir = art / "variants" / vid
    vdir.mkdir(parents=True, exist_ok=True)
    if curve is not None:
        (vdir / "metrics").mkdir(parents=True, exist_ok=True)
        (vdir / "metrics" / "metrics.jsonl").write_text(
            "".join(_json(r) for r in curve), encoding="utf-8")
    if train_status is not None:
        (vdir / "train_status.json").write_text(json.dumps(train_status),
                                                encoding="utf-8")
    if shard is not None:
        (vdir / "ledger_entry.json").write_text(json.dumps(shard),
                                                encoding="utf-8")
    if verdict is not None:
        (vdir / "verdict.json").write_text(json.dumps(verdict),
                                           encoding="utf-8")
    for doc in docs:
        _write(art, f"variants/{vid}/{doc}", f"[sentinel] {vid} {doc}\n")


def _seed(art: Path) -> None:
    """The live mid-run workspace: baseline + 4 variants + rounds 1-2 + rules."""
    _write(art, "baseline/baseline_metrics.jsonl",
           '{"epoch": 1, "metric": 0.4}\n{"epoch": 2, "metric": 0.5}\n')
    _write(art, "base/origin_anchor.json", _json(
        {"baseline_makespan_cycles": 1000, "target_cycles": 500,
         "accuracy_budget": 0.1}))
    for rel in ("baseline/business_logic.md", "base/information_analysis.md",
                "base/profile/mfu_bottleneck_report.md",
                "base/accuracy_rules_snapshot.json",
                "rounds/001/analysis.md", "rounds/002/analysis.md"):
        _write(art, rel, f"[sentinel] {rel}\n")
    _DOCS4 = ("business_logic.md", "information_analysis.md",
              "conformance.md", "profile/mfu_bottleneck_report.md")
    _variant(art, "r1-01", curve=[{"epoch": 1, "metric": 0.38},
                                   {"epoch": 2, "metric": 0.42}],
             train_status={"vid": "r1-01", "stage": "done"},
             shard={"vid": "r1-01", "status": "success", "gap": 0.02,
                    "metric": 0.42},
             verdict={"vid": "r1-01", "makespan_cycles": 800,
                      "outcome": "latency_pass"}, docs=_DOCS4)
    _variant(art, "r2-01", curve=[{"epoch": 1, "metric": 0.45}],
             train_status={"vid": "r2-01", "stage": "training",
                           "ts": "2026-08-31T11:00:00+00:00"},
             shard={"vid": "r2-01", "status": "training", "gap": None,
                    "metric": 0.45},
             verdict={"vid": "r2-01", "makespan_cycles": 1200,
                      "outcome": "latency_pass"},
             docs=("business_logic.md",))
    # 达线未训: admitted, training not started -> y=null placeholder (§10.2)
    _variant(art, "r3-01",
             shard={"vid": "r3-01", "status": "latency_pass", "gap": None,
                    "metric": None},
             verdict={"vid": "r3-01", "makespan_cycles": 900,
                      "outcome": "latency_pass"},
             docs=("business_logic.md", "conformance.md"))
    # eliminated variant — its docs STAY listed (web §3.3)
    _variant(art, "r4-01",
             verdict={"vid": "r4-01", "makespan_cycles": 1150,
                      "outcome": "latency_fail"},
             docs=("business_logic.md",))


def _advance(art: Path) -> None:
    """The report-time workspace: one more round + one more success variant."""
    _write(art, "rounds/003/analysis.md", "[sentinel] rounds/003/analysis.md\n")
    _variant(art, "r5-01", curve=[{"epoch": 1, "metric": 0.41},
                                  {"epoch": 2, "metric": 0.46}],
             train_status={"vid": "r5-01", "stage": "done"},
             shard={"vid": "r5-01", "status": "success", "gap": 0.005,
                    "metric": 0.46},
             verdict={"vid": "r5-01", "makespan_cycles": 600,
                      "outcome": "latency_pass"},
             docs=("business_logic.md", "conformance.md"))


def _run(art: Path, *extra: str) -> dict[str, dict]:
    """Run push_curves.main() with the socket send recorded in-process."""
    captured: dict[str, dict] = {}

    def record(_sock, _node, _sid, payload):
        captured[payload["label"]] = payload
        return True

    push_curves._push_best_effort = record  # type: ignore[assignment]
    os.environ.update(ORCA_ARTIFACTS_DIR=str(art), ORCA_CHART_SOCK="/dev/null",
                      ORCA_NODE="po_propose", ORCA_SESSION_ID="s-fixture")
    argv_backup = sys.argv
    try:
        sys.argv = ["push_curves.py", "--artifacts", str(art), "--docs",
                    *extra]
        rc = push_curves.main()
    finally:
        sys.argv = argv_backup
    if rc != 0:
        raise SystemExit(f"push_curves.main() returned {rc}")
    return captured


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="profopt-fixture-") as td:
        art = Path(td) / "art"
        _seed(art)
        live = _run(art)
        _advance(art)
        final = _run(art, "--title", "(final)")
    expected = {"prof-opt/curves", "prof-opt/pareto", "prof-opt/docs"}
    missing = (expected - set(live)) | (expected - set(final))
    if missing:
        raise SystemExit(f"pushes incomplete, missing labels: {sorted(missing)}")
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(json.dumps(
        {"live": live, "final": final}, indent=2, sort_keys=True,
        ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[gen-profopt-fixtures] wrote {_OUT} "
          f"(live docs rows: {len(live['prof-opt/docs']['data'])}, "
          f"final docs rows: {len(final['prof-opt/docs']['data'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
