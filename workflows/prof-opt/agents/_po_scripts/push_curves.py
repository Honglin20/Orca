#!/usr/bin/env python3
"""push_curves.py — best-effort live-chart sidecar for the training curves (v6 §10).

Three charts over the chart socket, each an idempotent REPLACE (same
label+title on every push -> the front end replaces the previous chart):

  * line   ``prof-opt/curves``  — top-10 training curves (§10.1): the baseline
    always present plus at most 9 variants, selected ① in-flight trainings
    (curve on disk, no terminal state) by most recent update first, ② terminal
    success next, ③ the rest by ascending gap. The FULL curve files stay on
    disk — only the push is narrowed.
  * pareto ``prof-opt/pareto``  — every variant as one point (§10.2): x = the
    latency reduction vs the baseline makespan in % (negative = slower), y =
    the final gap (or the latest metric while no gap exists yet), one
    status-colored point per variant; a latency_pass variant that has not
    started training keeps y=null (disclosed in the caption).
  * table  ``prof-opt/docs``    — the analysis-docs manifest (§10.4, ``--docs``):
    rows of vid / doc / status / path relative to the artifacts root (+
    updated_at). Paths only, NEVER the document bodies; the whitelist is the
    run's own artifacts tree (every listed path is a constructed constant,
    never a discovered absolute path). Trigger points: the proposal node's
    latency_pass emit (wired) and the report node's final pass (wired with
    the report node's v6 finalization).

Fail-soft by contract — this sidecar must never stall or fail a worker:
  * ``ORCA_CHART_SOCK`` unset           -> silent exit 0;
  * socket connect/send/ack exceeding 5s each (hard timeout) -> stderr note
    + exit 0 (a hung chart daemon must not drag the finalizer with it);
  * any push failure                    -> stderr note + exit 0 (one chart
    failing never blocks the others);
  * missing curve files / half-written JSONL rows / unreadable state files
    -> skipped, never fatal.

Missing ``ORCA_NODE`` / ``ORCA_SESSION_ID`` (not inside an Orca run) is the
same best-effort skip (stderr note + exit 0). Each successful line push
APPENDS an audit line to ``$ORCA_ARTIFACTS_DIR/.chart_push.log``:
``{ts, baseline_epochs, curves:[...]}``.

Usage:
    push_curves.py [--artifacts DIR] [--label L] [--title T] [--docs]
``--title`` is a TITLE SUFFIX (default empty) applied to every chart; the
report's finalize push passes ``(final)`` so the terminal charts are visibly
distinct from the live ones. ``--docs`` additionally pushes the analysis-docs
manifest (§10.4 trigger: propose latency_pass emit / report final).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SOCK_TIMEOUT_SECONDS = 5.0  # hard per-op cap: connect / send / ack each <= 5s
_DEFAULT_LABEL = "prof-opt/curves"
_BASE_TITLE = "prof-opt training curves"
_PARETO_LABEL = "prof-opt/pareto"
_PARETO_TITLE = "prof-opt variants pareto"
_DOCS_LABEL = "prof-opt/docs"
_DOCS_TITLE = "prof-opt analysis docs"
_TOP_N_VARIANTS = 9  # §10.1: baseline + at most 9 variant curves

# §7.4 train_status.json terminal stages / §4.3 terminal history outcomes
_TERMINAL_STAGES = {"killed", "done", "failed"}
_TERMINAL_OUTCOMES = {"success", "accuracy_fail", "probe_insufficient",
                      "latency_fail"}

# §10.2 status coloring: one CSS color per variant state (the payload's
# per-row ``color`` field carries it; the front end renders it as-is)
_STATUS_COLORS = {
    "success": "#10b981",
    "in-flight": "#3b82f6",
    "latency_pass": "#94a3b8",       # 达线未训占位 (y=null)
    "accuracy_fail": "#ef4444",
    "latency_fail": "#f97316",
    "probe_insufficient": "#a855f7",
}
_NEUTRAL_COLOR = "#64748b"

# §10.4 manifest row set — the whitelist IS this table of constructed
# artifacts-relative constants; nothing discovered outside it is ever listed.
_BASELINE_DOC_ROWS = (
    ("baseline", "business_logic.md", "baseline/business_logic.md"),
    ("baseline", "information_analysis.md", "base/information_analysis.md"),
    ("baseline", "mfu_bottleneck_report.md",
     "base/profile/mfu_bottleneck_report.md"),
)
_VARIANT_DOC_FILES = (
    "assessment.md",
    "profile/mfu_bottleneck_report.md",
)
_RULES_ROW = ("rules", "accuracy_rules_snapshot.json",
              "base/accuracy_rules_snapshot.json")


def _load_curve(path: Path, vid: str) -> list[dict[str, Any]]:
    """Parse a metric-curve JSONL; half-written/corrupt rows are skipped."""
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            epoch, metric = int(row["epoch"]), float(row["metric"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue  # half-written tail row — the next poll picks it up whole
        rows.append({"vid": vid, "epoch": epoch, "metric": metric})
    return rows


def _read_json(path: Path) -> dict[str, Any] | None:
    """Best-effort state-file read (None on missing/unparseable — the state
    files are other writers' outputs; an unreadable one degrades that one
    variant's tier/color, it never fails the sidecar — but it is noted on
    stderr so the degradation stays visible, never silent)."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"[push_curves] state file unreadable "
                         f"(degrading that variant's push metadata): "
                         f"{path}: {exc}\n")
        return None
    return data if isinstance(data, dict) else None


def _variant_state(vdir: Path) -> dict[str, Any]:
    """Per-variant state for every push decision (§10.1/§10.2/§10.4).

    Sources: train_status.json (stage/epoch/metric/gap/ts — the watchdog's),
    ledger_entry.json (status — the propose-seeded / watchdog-kept shard),
    verdict.json (outcome + makespan_cycles — the propose measurement).
    """
    train_status = _read_json(vdir / "train_status.json") or {}
    shard = _read_json(vdir / "ledger_entry.json") or {}
    verdict = _read_json(vdir / "verdict.json") or {}
    stage = train_status.get("stage")
    outcome = shard.get("status") or verdict.get("outcome") or ""
    terminal = (stage in _TERMINAL_STAGES
                or outcome in _TERMINAL_OUTCOMES)
    if terminal:
        status = outcome or str(stage or "")
    elif outcome == "latency_pass" and not stage:
        status = "latency_pass"          # 达线未训: admitted, training not started
    else:
        status = "in-flight"
    gap = shard.get("gap", train_status.get("gap"))
    metric = shard.get("metric", train_status.get("metric"))
    ts = train_status.get("ts")
    return {"vid": vdir.name, "terminal": terminal, "status": status,
            "gap": gap if isinstance(gap, (int, float)) else None,
            "metric": metric if isinstance(metric, (int, float)) else None,
            "ts": ts if isinstance(ts, str) else "",
            "makespan": verdict.get("makespan_cycles")
            if isinstance(verdict.get("makespan_cycles"), (int, float))
            else None}


def _recency(state: dict[str, Any], curve_path: Path) -> float:
    """§10.1 ① 'most recent update': the watchdog's ts when recorded, else the
    curve file's mtime (both normalize to a comparable epoch float)."""
    if state["ts"]:
        try:
            return datetime.fromisoformat(state["ts"]).timestamp()
        except ValueError:
            pass  # fall through to the file's mtime
    try:
        return curve_path.stat().st_mtime
    except OSError:
        return 0.0


def _gap_key(state: dict[str, Any]) -> float:
    return state["gap"] if state["gap"] is not None else math.inf


def collect(artifacts: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """§10.1 top-10 data rows + the per-curve audit summary (vid + parsed
    point count). The baseline is always present; the variant curves are the
    selected top-9 (in-flight by recency -> terminal success -> gap asc)."""
    data: list[dict[str, Any]] = []
    curves: list[dict[str, Any]] = []
    baseline = _load_curve(artifacts / "baseline" / "baseline_metrics.jsonl",
                           "baseline")
    data.extend(baseline)
    if baseline:
        curves.append({"vid": "baseline", "epochs": len(baseline)})
    candidates: list[tuple[dict[str, Any], list[dict[str, Any]], Path]] = []
    variants_dir = artifacts / "variants"
    if variants_dir.is_dir():
        for vdir in sorted(variants_dir.iterdir()):
            if not vdir.is_dir():
                continue
            curve_path = vdir / "metrics" / "metrics.jsonl"
            rows = _load_curve(curve_path, vdir.name)
            if rows:
                candidates.append((_variant_state(vdir), rows, curve_path))
    # §10.1 selection: ① in-flight (curve + not terminal) by most recent
    # update first; ② terminal success next; ③ the rest by ascending gap
    # (vid as the deterministic tiebreak). Variants without a curve cannot be
    # drawn and are never selected.
    in_flight = sorted(
        (c for c in candidates if not c[0]["terminal"]),
        key=lambda c: (-_recency(c[0], c[2]), c[0]["vid"]))
    successes = sorted((c for c in candidates if c[0]["terminal"]
                        and c[0]["status"] == "success"),
                       key=lambda c: (_gap_key(c[0]), c[0]["vid"]))
    rest = sorted((c for c in candidates if c[0]["terminal"]
                   and c[0]["status"] != "success"),
                  key=lambda c: (_gap_key(c[0]), c[0]["vid"]))
    for state, rows, _ in (*in_flight, *successes, *rest)[:_TOP_N_VARIANTS]:
        data.extend(rows)
        curves.append({"vid": state["vid"], "epochs": len(rows)})
    return data, curves


def _baseline_makespan(artifacts: Path) -> float | None:
    """The pareto x-axis denominator: the frozen origin anchor first, then the
    profiled bottleneck reports (the same chain of authorities the gates read,
    most-frozen first)."""
    for rel, key in (("base/origin_anchor.json", "baseline_makespan_cycles"),
                     ("base/bottleneck_report.json", "makespan_cycles"),
                     ("base/profile/profile_summary.json", "makespan_cycles")):
        doc = _read_json(artifacts / rel)
        value = doc.get(key) if doc else None
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


def collect_pareto(artifacts: Path) -> list[dict[str, Any]]:
    """§10.2 one point per variant: x = latency reduction vs baseline (%,
    negative = slower), y = the final gap (or the latest metric while no gap
    exists yet; null = the 达线未训 placeholder), status-colored. Variants
    without a measured makespan have no x and are not plottable."""
    base_ms = _baseline_makespan(artifacts)
    if base_ms is None:
        return []
    rows: list[dict[str, Any]] = []
    variants_dir = artifacts / "variants"
    if not variants_dir.is_dir():
        return rows
    for vdir in sorted(variants_dir.iterdir()):
        if not vdir.is_dir():
            continue
        state = _variant_state(vdir)
        if state["makespan"] is None:
            continue
        x = round((1.0 - state["makespan"] / base_ms) * 100.0, 4)
        y = state["gap"] if state["gap"] is not None else state["metric"]
        rows.append({"vid": state["vid"], "x": x, "y": y,
                     "status": state["status"],
                     "color": _STATUS_COLORS.get(state["status"],
                                                 _NEUTRAL_COLOR)})
    return rows


def collect_docs(artifacts: Path) -> list[dict[str, Any]]:
    """§10.4 the analysis-docs manifest: vid / doc / status / artifacts-
    relative path (+updated_at from the file's mtime). Only files that exist
    are listed; the paths come exclusively from the constructed whitelist
    constants — an absolute or traversing path is a bug and is dropped with a
    stderr note (fail loud for us, invisible to the front end)."""
    def row(vid: str, doc: str, rel: str, status: str) -> dict[str, Any] | None:
        # whitelist guard BEFORE any filesystem resolution: an absolute or
        # traversing rel must never even be probed against the host fs
        parts = Path(rel).parts
        if not rel or Path(rel).is_absolute() or ".." in parts:
            sys.stderr.write(f"[push_curves] whitelist violation dropped: "
                             f"{rel}\n")
            return None
        path = artifacts / rel
        if not path.is_file():
            return None
        try:
            updated_at = datetime.fromtimestamp(
                path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        except OSError:
            updated_at = ""
        return {"vid": vid, "doc": doc, "status": status, "path": rel,
                "updated_at": updated_at}

    rows: list[dict[str, Any]] = []
    for vid, doc, rel in _BASELINE_DOC_ROWS:            # 基线组
        if (r := row(vid, doc, rel, "baseline")):
            rows.append(r)
    variants_dir = artifacts / "variants"
    if variants_dir.is_dir():                            # 变体组（含淘汰，web §3.3）
        for vdir in sorted(variants_dir.iterdir(), key=lambda p: p.name):
            if not vdir.is_dir():
                continue
            state = _variant_state(vdir)
            for name in _VARIANT_DOC_FILES:
                if (r := row(vdir.name, name, f"variants/{vdir.name}/{name}",
                             state["status"])):
                    rows.append(r)
    rounds_dir = artifacts / "rounds"                    # 轮次组（S-9）
    if rounds_dir.is_dir():
        for rdir in sorted((d for d in rounds_dir.iterdir() if d.is_dir()
                            and d.name.isdigit()), key=lambda d: int(d.name)):
            if (r := row("round", "analysis.md", f"rounds/{rdir.name}/analysis.md",
                         "final")):
                rows.append(r)
    if (r := row(*_RULES_ROW, "snapshot")):              # 规则组（S-9 快照源）
        rows.append(r)
    return rows


def _send(sock_path: str, node: str, session_id: str,
          payload: dict[str, Any]) -> None:
    """One socket push. Raises on any failure (caller decides fail-soft)."""
    msg = {"node": node, "session_id": session_id, "payload": payload}
    encoded = (json.dumps(msg) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(_SOCK_TIMEOUT_SECONDS)  # connect + send + ack, each <= 5s
        s.connect(sock_path)
        s.sendall(encoded)
        with s.makefile("rb") as fh:
            ack_raw = fh.readline()
    if not ack_raw:
        raise ConnectionError("chart socket closed without an ack")
    ack = json.loads(ack_raw)
    if not ack.get("ok"):
        raise ConnectionError(f"chart daemon rejected the payload: "
                              f"{ack.get('error', '<no error>')}")


def _push_best_effort(sock_path: str, node: str, session_id: str,
                      payload: dict[str, Any]) -> bool:
    """One chart, fail-soft: a failure is a stderr note, never a worker stall
    and never a blocker for the other charts."""
    try:
        _send(sock_path, node, session_id, payload)
        return True
    except (OSError, ConnectionError, json.JSONDecodeError,
            ValueError) as exc:
        # FileNotFoundError/ConnectionRefused/socket.timeout/daemon NACK
        sys.stderr.write(f"[push_curves] push failed for "
                         f"{payload.get('label')} (best-effort, ignored): "
                         f"{exc}\n")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", default=os.environ.get("ORCA_ARTIFACTS_DIR", "."),
                    help="workspace root (default: $ORCA_ARTIFACTS_DIR)")
    ap.add_argument("--label", default=_DEFAULT_LABEL,
                    help=f"line chart group key (default: {_DEFAULT_LABEL})")
    ap.add_argument("--title", default="",
                    help="TITLE SUFFIX appended to every chart's base title "
                         "(the report finalize push passes '(final)'); "
                         "default: no suffix")
    ap.add_argument("--docs", action="store_true",
                    help="also push the analysis-docs manifest table "
                         "(§10.4 trigger: propose latency_pass emit / report "
                         "final)")
    ns = ap.parse_args()

    sock_path = os.environ.get("ORCA_CHART_SOCK", "")
    if not sock_path:
        return 0  # silent by contract: no daemon configured, nothing to do
    node = os.environ.get("ORCA_NODE", "")
    session_id = os.environ.get("ORCA_SESSION_ID", "")
    if not node or not session_id:
        sys.stderr.write("[push_curves] not inside an Orca run "
                         "(ORCA_NODE/ORCA_SESSION_ID unset) — skipping\n")
        return 0

    art = Path(ns.artifacts)
    suffix = f" {ns.title}".rstrip()

    # chart 1 — top-10 training curves (§10.1)
    data, curves = collect(art)
    pushed: dict[str, bool] = {}
    if data:
        pushed["curves"] = _push_best_effort(sock_path, node, session_id, {
            "chart_type": "line", "data": data, "label": ns.label,
            "title": _BASE_TITLE + suffix,
            "x": "epoch", "y": "metric", "hue": "vid",
            "color": "", "value": "",
        })
        if pushed["curves"]:
            audit = {"ts": datetime.now(timezone.utc).isoformat(
                         timespec="seconds"),
                     "baseline_epochs": next(
                         (c["epochs"] for c in curves
                          if c["vid"] == "baseline"), 0),
                     "curves": curves}
            try:
                with open(art / ".chart_push.log", "a",
                          encoding="utf-8") as fh:
                    fh.write(json.dumps(audit, sort_keys=True) + "\n")
            except OSError as exc:  # audit is best-effort too
                sys.stderr.write(f"[push_curves] audit append failed "
                                 f"(ignored): {exc}\n")

    # chart 2 — full pareto (§10.2): every variant one status-colored point
    pareto_rows = collect_pareto(art)
    if pareto_rows:
        pushed["pareto"] = _push_best_effort(sock_path, node, session_id, {
            "chart_type": "pareto", "data": pareto_rows,
            "label": _PARETO_LABEL, "title": _PARETO_TITLE + suffix,
            "x": "x", "y": "y", "color": "color", "hue": "", "value": "",
            "pareto_x_direction": "max", "pareto_y_direction": "min",
            "x_label": "latency reduction vs baseline (%)",
            "y_label": "final gap / latest metric",
            "caption": "y=null = no measurable outcome yet (达线未训占位, or "
                       "trained but still awaiting the baseline anchor); y "
                       "falls back to the latest metric while no gap exists "
                       "(lower-is-better holds for gap only)",
        })

    # chart 3 — analysis-docs manifest (§10.4): paths only, never bodies
    if ns.docs:
        docs_rows = collect_docs(art)
        if docs_rows:
            pushed["docs"] = _push_best_effort(sock_path, node, session_id, {
                "chart_type": "table", "data": docs_rows,
                "columns": ["vid", "doc", "status", "path", "updated_at"],
                "label": _DOCS_LABEL, "title": _DOCS_TITLE + suffix,
                "x": "", "y": "", "hue": "", "color": "", "value": "",
            })

    print(json.dumps({"pushed": pushed, "rows": len(data),
                      "pareto_points": len(pareto_rows)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
