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
    status-colored point per variant; a latency_improved variant that has not
    started training keeps y=null (disclosed in the caption). C2: a distinct
    ``baseline`` anchor point (x=0, dedicated color) pins the origin whenever
    the baseline makespan anchor resolves.
  * table  ``prof-opt/docs``    — the analysis-docs manifest (§10.4, ``--docs``):
    rows of vid / doc / status / path relative to the artifacts root (+
    updated_at) plus the optional content channel (C3): rows also carry
    ``content`` (the document body, or "" when not carried this push) and
    ``content_omitted`` ("true" when the body was too large or the aggregate
    budget was spent). The whitelist is the run's own artifacts tree (every
    listed path is a constructed constant, never a discovered absolute path).
    Trigger points: the baseline chain's first push (run_baseline_chain.sh),
    the propose emit gate's per-round push (check_propose_emit.py, fired
    after the gate passes), and the report node's final pass
    (``--title "(final)"``) — the three ``--docs`` call sites.

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
manifest with the content channel (triggers: the baseline chain's first push /
the propose emit gate's per-round push / the report node's final pass).
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
    "latency_improved": "#94a3b8",   # 改善未训占位 (y=null)
    "accuracy_fail": "#ef4444",
    "latency_fail": "#f97316",
    "probe_insufficient": "#a855f7",
}
_NEUTRAL_COLOR = "#64748b"
# C2 baseline anchor color: sky-500 (#0ea5e9) — deliberately distinct from
# every _STATUS_COLORS entry AND the neutral gray above, so the origin
# reference reads as its own series instead of masquerading as a variant
# state (the closest neighbors, in-flight #3b82f6 / probe_insufficient
# #a855f7, are far enough in hue to stay distinguishable at plot size).
_BASELINE_COLOR = "#0ea5e9"

# C3 content-channel budgets, measured in utf-8 BYTES of the raw content
# BEFORE serialization. The aggregate cap keeps 0.5MB of headroom under the
# 2MB chart-socket line limit so the JSON envelope (field names + the ≈5%
# escape growth of quotes/backslashes/newlines under ensure_ascii=False)
# always fits; residual escapes are covered by that headroom, not re-metered
# after serialization.
MAX_DOC_CONTENT_BYTES = 256 * 1024
MAX_MANIFEST_CONTENT_BYTES = 1_500_000

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
    elif outcome == "latency_improved" and not stage:
        status = "latency_improved"      # 改善未训: admitted, training not started
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
    """The pareto x-axis denominator is the frozen baseline anchor."""
    doc = _read_json(artifacts / "base" / "origin_anchor.json")
    value = doc.get("baseline_makespan_cycles") if doc else None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def collect_pareto(artifacts: Path) -> list[dict[str, Any]]:
    """§10.2 one point per variant: x = latency reduction vs baseline (%,
    negative = slower), y = the final gap (or the latest metric while no gap
    exists yet; null = the 达线未训 placeholder), status-colored. Variants
    without a measured makespan have no x and are not plottable.

    C2 baseline anchor: whenever the origin anchor resolves, one extra
    ``vid="baseline"`` row pins the origin (x=0) so "relative to the
    baseline" has a visible reference point at every stage. A missing anchor
    keeps the chart exactly as it was (no fabricated origin)."""
    base_ms = _baseline_makespan(artifacts)
    if base_ms is None:
        return []
    rows: list[dict[str, Any]] = []
    any_gap_basis = False
    variants_dir = artifacts / "variants"
    if variants_dir.is_dir():
        for vdir in sorted(variants_dir.iterdir()):
            if not vdir.is_dir():
                continue
            state = _variant_state(vdir)
            if state["makespan"] is None:
                continue
            x = round((1.0 - state["makespan"] / base_ms) * 100.0, 4)
            y = state["gap"] if state["gap"] is not None else state["metric"]
            any_gap_basis = any_gap_basis or state["gap"] is not None
            rows.append({"vid": state["vid"], "x": x, "y": y,
                         "status": state["status"],
                         "color": _STATUS_COLORS.get(state["status"],
                                                     _NEUTRAL_COLOR)})
    if not any(r["vid"] == "baseline" for r in rows):  # idempotent anchor
        rows.append(_baseline_anchor_row(artifacts, any_gap_basis))
    return rows


def _baseline_anchor_row(artifacts: Path, any_gap_basis: bool) -> dict[str, Any]:
    """C2 the baseline origin reference row (x=0). y follows the variant
    rows' basis (option b): 0 when any variant plots a gap (the baseline gap
    is 0 by definition — dimension-consistent), else the baseline's latest
    metric from the same curve loader the variant y values use. A missing or
    empty baseline curve keeps y=null (disclosed in the caption) — never a
    fabricated 0."""
    if any_gap_basis:
        y: float | None = 0
    else:
        curve = _load_curve(artifacts / "baseline" / "baseline_metrics.jsonl",
                            "baseline")
        y = curve[-1]["metric"] if curve else None
    return {"vid": "baseline", "x": 0, "y": y, "status": "baseline",
            "color": _BASELINE_COLOR}


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
            for name in ("architecture_decision.md",):
                if (r := row("round", name, f"rounds/{rdir.name}/{name}",
                             "final")):
                    rows.append(r)
            for name in ("semantic.md", "hardware.md", "sota.md"):
                if (r := row("round", name,
                             f"rounds/{rdir.name}/candidates/{name}", "candidate")):
                    rows.append(r)
    if (r := row(*_RULES_ROW, "snapshot")):              # 规则组（S-9 快照源）
        rows.append(r)
    return rows


# ── C3 content channel state (per-run, in the shared $ART root) ─────────────


def _docs_state_path(artifacts: Path) -> Path | None:
    """Per-run state file ``$ART/.docs_push_state.<ORCA_RUN_ID>.json``.

    ``$ART`` is project-scoped and shared across runs — the per-run suffix
    keeps one run's push state from polluting another's. ``ORCA_RUN_ID``
    unset (manual run outside an Orca run) → None: no state read, every push
    carries full content, nothing persisted."""
    run_id = os.environ.get("ORCA_RUN_ID", "")
    if not run_id:
        return None
    return artifacts / f".docs_push_state.{run_id}.json"


def _load_docs_state(path: Path | None) -> dict[str, dict[str, Any]]:
    """Read the push state ``relpath -> {mtime, size}``.

    No file yet → {} (the normal first push — silent). Unreadable/corrupt →
    {} + one stderr line (full re-push this time, never a crash). Malformed
    entries are dropped (treated as not-pushed)."""
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"[push_curves] docs push state unreadable "
                         f"(full re-push): {path}: {exc}\n")
        return {}
    if not isinstance(data, dict):
        return {}
    return {rel: entry for rel, entry in data.items()
            if isinstance(rel, str) and isinstance(entry, dict)
            and isinstance(entry.get("mtime"), (int, float))
            and isinstance(entry.get("size"), int)}


def _write_docs_state(path: Path,
                      updates: dict[str, dict[str, Any]]) -> None:
    """Merge-write the push state, tmp + ``os.replace`` atomic.

    Existing entries survive (stale relpaths are harmless — rows only ever
    come from the whitelist's live files); ``updates`` win. No locking: the
    three ``--docs`` trigger points sit on serial DAG nodes, there is no
    concurrent-writer surface."""
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            data = loaded
    data.update(updates)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        sys.stderr.write(f"[push_curves] docs push state write failed "
                         f"(ignored): {exc}\n")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _fill_docs_content(artifacts: Path, rows: list[dict[str, Any]],
                       state: dict[str, dict[str, Any]]
                       ) -> dict[str, dict[str, Any]]:
    """C3 fill ``content`` / ``content_omitted`` on manifest rows, in
    whitelist order, under the aggregate budget. Three-state truth table:

      * content carried      ⇔ needs push (absent from state, or mtime/size
        changed) AND size <= MAX_DOC_CONTENT_BYTES AND within budget;
      * content_omitted=true ⇔ size > MAX_DOC_CONTENT_BYTES (state-free —
        judged fresh every push) OR budget-truncated this push;
      * empty + no omission  ⇔ unchanged row (the front end merges it from
        an earlier push in the same label's event history).

    Returns the state write-back updates: carried rows ∪ size-omitted rows
    (retrying either is useless). Budget-truncated rows are NOT included —
    the next trigger retries them once the budget is free. Rows whose read
    fails (OSError/decode) stay contentless and out of the state (stderr
    line; the front end's merge/fallback path covers them)."""
    updates: dict[str, dict[str, Any]] = {}
    total = 0
    for row in rows:
        path = artifacts / row["path"]
        try:
            st = path.stat()
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            sys.stderr.write(f"[push_curves] doc read failed (row stays "
                             f"contentless): {row['path']}: {exc}\n")
            row["content"] = ""
            row["content_omitted"] = ""
            continue
        if st.st_size > MAX_DOC_CONTENT_BYTES:
            sys.stderr.write(f"[push_curves] doc over "
                             f"{MAX_DOC_CONTENT_BYTES} bytes, content "
                             f"omitted: {row['path']}\n")
            row["content"] = ""
            row["content_omitted"] = "true"
            updates[row["path"]] = {"mtime": st.st_mtime, "size": st.st_size}
            continue
        prev = state.get(row["path"])
        needs_push = not (prev is not None
                          and prev.get("mtime") == st.st_mtime
                          and prev.get("size") == st.st_size)
        if not needs_push:
            row["content"] = ""
            row["content_omitted"] = ""
            continue
        nbytes = len(text.encode("utf-8"))
        if total + nbytes > MAX_MANIFEST_CONTENT_BYTES:
            sys.stderr.write(f"[push_curves] manifest content budget "
                             f"exceeded, content omitted this push: "
                             f"{row['path']}\n")
            row["content"] = ""
            row["content_omitted"] = "true"
            continue  # NOT recorded — retried on the next trigger
        total += nbytes
        row["content"] = text
        row["content_omitted"] = ""
        updates[row["path"]] = {"mtime": st.st_mtime, "size": st.st_size}
    return updates


def _send(sock_path: str, node: str, session_id: str,
          payload: dict[str, Any]) -> None:
    """One socket push. Raises on any failure (caller decides fail-soft).

    ``ensure_ascii=False`` (C3): the docs manifest now carries CJK document
    bodies — the default ascii escaping would inflate them 2-3x in \\uXXXX
    form and blow the 2MB line limit for an all-CJK payload. The transport
    is utf-8 line decoding + ``json.loads`` (chart ingestor), which takes
    raw CJK natively."""
    msg = {"node": node, "session_id": session_id, "payload": payload}
    encoded = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
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
                    help="also push the analysis-docs manifest table with the "
                         "content channel (triggers: baseline chain first "
                         "push / propose emit gate per round / report final "
                         "push)")
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
                       "(lower-is-better holds for gap only); "
                       "the baseline anchor (x=0) reads y=0 on the gap basis "
                       "(baseline gap is 0 by definition) or the baseline's "
                       "latest metric on the metric basis",
        })

    # chart 3 — analysis-docs manifest (§10.4) + content channel (C3):
    # rows carry the doc body when it changed and fits; unchanged rows stay
    # empty (the front end merges history), oversized/budget-truncated rows
    # are flagged. State is written back ONLY after a successful send.
    if ns.docs:
        docs_rows = collect_docs(art)
        if docs_rows:
            state_path = _docs_state_path(art)
            state = _load_docs_state(state_path)
            state_updates = _fill_docs_content(art, docs_rows, state)
            pushed["docs"] = _push_best_effort(sock_path, node, session_id, {
                "chart_type": "table", "data": docs_rows,
                "columns": ["vid", "doc", "status", "path", "updated_at"],
                "label": _DOCS_LABEL, "title": _DOCS_TITLE + suffix,
                "x": "", "y": "", "hue": "", "color": "", "value": "",
            })
            if pushed["docs"] and state_path is not None and state_updates:
                _write_docs_state(state_path, state_updates)

    print(json.dumps({"pushed": pushed, "rows": len(data),
                      "pareto_points": len(pareto_rows)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
