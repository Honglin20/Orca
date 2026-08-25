#!/usr/bin/env python3
"""push_curves.py — best-effort live-chart sidecar for the training curves.

Reads the baseline curve (``baseline/baseline_metrics.jsonl``) plus every
variant curve on disk (``variants/<vid>/metrics.jsonl``) and pushes ONE live
line chart over the chart socket: hue = baseline/vid, x = epoch, y = metric.
Same label+title on every push -> the front end REPLACES the previous chart
(the live-update semantics), so calling this repeatedly is idempotent for the
chart while each successful push APPENDS an audit line to
``$ORCA_ARTIFACTS_DIR/.chart_push.log``: ``{ts, baseline_epochs, curves:[...]}``.

Fail-soft by contract — this sidecar must never stall or fail a worker:
  * ``ORCA_CHART_SOCK`` unset           -> silent exit 0;
  * socket connect/send/ack exceeding 5s each (hard timeout) -> stderr note
    + exit 0 (a hung chart daemon must not drag the finalizer with it);
  * any push failure                    -> stderr note + exit 0;
  * missing curve files / half-written JSONL rows -> skipped, never fatal.

Missing ``ORCA_NODE`` / ``ORCA_SESSION_ID`` (not inside an Orca run) is the
same best-effort skip (stderr note + exit 0).

Usage:
    push_curves.py [--artifacts DIR] [--label L] [--title T]
``--title`` is a TITLE SUFFIX (default empty); the report's finalize push
passes ``(final)`` so the terminal chart is visibly distinct from the live one.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SOCK_TIMEOUT_SECONDS = 5.0  # hard per-op cap: connect / send / ack each <= 5s
_DEFAULT_LABEL = "prof-opt/curves"
_BASE_TITLE = "prof-opt training curves"


class _Skip(RuntimeError):
    """Best-effort skip: message to stderr, exit 0."""


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


def collect(artifacts: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Data rows + the per-curve audit summary (vid + parsed point count)."""
    data: list[dict[str, Any]] = []
    curves: list[dict[str, Any]] = []
    baseline = _load_curve(artifacts / "baseline" / "baseline_metrics.jsonl",
                           "baseline")
    data.extend(baseline)
    if baseline:
        curves.append({"vid": "baseline", "epochs": len(baseline)})
    variants_dir = artifacts / "variants"
    if variants_dir.is_dir():
        for vdir in sorted(variants_dir.iterdir()):
            if not vdir.is_dir():
                continue
            rows = _load_curve(vdir / "metrics.jsonl", vdir.name)
            if rows:
                data.extend(rows)
                curves.append({"vid": vdir.name, "epochs": len(rows)})
    return data, curves


def push(sock_path: str, node: str, session_id: str, label: str, title: str,
         data: list[dict[str, Any]]) -> None:
    """One socket push. Raises on any failure (caller decides fail-soft)."""
    payload = {
        "chart_type": "line",
        "data": data,
        "label": label,
        "title": title,
        "x": "epoch",
        "y": "metric",
        "hue": "vid",
        "color": "",
        "value": "",
    }
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", default=os.environ.get("ORCA_ARTIFACTS_DIR", "."),
                    help="workspace root (default: $ORCA_ARTIFACTS_DIR)")
    ap.add_argument("--label", default=_DEFAULT_LABEL,
                    help=f"chart group key (default: {_DEFAULT_LABEL})")
    ap.add_argument("--title", default="",
                    help="TITLE SUFFIX appended to the base title (the report "
                         "finalize push passes '(final)'); default: no suffix")
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
    data, curves = collect(art)
    if not data:
        return 0  # nothing trained yet — the next poll pushes

    title = f"{_BASE_TITLE} {ns.title}".rstrip()
    try:
        push(sock_path, node, session_id, ns.label, title, data)
    except OSError as exc:  # FileNotFoundError/ConnectionRefused/socket.timeout
        sys.stderr.write(f"[push_curves] push failed (best-effort, ignored): "
                         f"{exc}\n")
        return 0
    except (ConnectionError, json.JSONDecodeError, ValueError) as exc:
        sys.stderr.write(f"[push_curves] push failed (best-effort, ignored): "
                         f"{exc}\n")
        return 0

    audit = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "baseline_epochs": next((c["epochs"] for c in curves
                                      if c["vid"] == "baseline"), 0),
             "curves": curves}
    try:
        with open(art / ".chart_push.log", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(audit, sort_keys=True) + "\n")
    except OSError as exc:  # audit is best-effort too
        sys.stderr.write(f"[push_curves] audit append failed (ignored): {exc}\n")
    print(json.dumps({"pushed": True, "rows": len(data),
                      "title": title}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
