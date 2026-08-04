"""_common.py -- shared utilities for ns_visualize chart scripts.

Every chart script imports from here for artifact discovery, metric name/direction
discovery (no hardcoding), JSONL reading, and a fail-soft render_chart wrapper.

NAS data convention (verified against real artifacts):
  - ``search_config.yaml`` ``objs`` is a **list of strings** (e.g. ``["acc", "latency"]``).
  - All objectives are stored as **smaller-is-better**: higher-better metrics like
    accuracy are **negated** (e.g. ``-0.17`` means ``0.17`` accuracy).
  - ``search_results.jsonl`` records have objectives **nested** under the ``objs`` key:
    ``{"objs": {"acc": -0.17, "latency": 0.13}, "pareto": true, "arch": {...}}``.

This module models that convention: ``MetricInfo`` captures the stored polarity so
chart scripts can un-negate for human-friendly display while computing Pareto fronts
on the raw smaller-is-better values.

Design principles:
- pathlib only (no string path concatenation).
- Fail-soft per chart: render_chart failure writes stderr + records "skipped"; never
  crashes the orchestrator (design-charts H1).
- Metric name + direction discovered from search_config.yaml objs (authoritative) ->
  project_manifest.md fallback -> generic.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# render_chart: optional import (scripts may run outside Orca during dev/test).
try:
    from orca.chart import render_chart  # type: ignore[import-untyped]
except ImportError:
    render_chart = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Marker file: each chart script appends one JSON line per chart result.
CHART_MARKER = ".ns_visualize_charts.jsonl"

# Common latency field paths — includes NAS nested form ``objs.latency``.
LATENCY_FIELDS: tuple[str, ...] = (
    "objs.latency",
    "latency_ms",
    "latency",
    "latency_ms_avg",
    "mean_latency_ms",
    "avg_latency_ms",
)

# Common Pareto flag field names.
PARETO_FIELDS: tuple[str, ...] = (
    "pareto",
    "is_pareto",
    "on_pareto_front",
    "pareto_front",
)


# ---------------------------------------------------------------------------
# MetricInfo dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricInfo:
    """Discovered project metric metadata for NAS visualization.

    NAS convention: all objectives stored as smaller-is-better. Higher-better
    metrics (e.g. accuracy) are negated in the stored data. This struct captures
    both the raw field path and the display polarity so charts can show
    human-friendly values (positive accuracy) while Pareto computation uses the
    correct smaller-is-better direction.

    Attributes:
        name: Raw objective name from search_config.yaml (e.g. "acc").
        field_path: Full path in flattened record (e.g. "objs.acc").
        latency_path: Full path to latency in flattened record (e.g. "objs.latency").
        pareto_y_direction: Pareto y-axis direction for raw stored values ("min" always,
            per NAS smaller-is-better convention).
        display_direction: Human-friendly direction for labels ("higher" or "lower").
        negate_for_display: True if stored values should be negated for display
            (i.e. the metric is a higher-better metric stored as negated).
    """

    name: str
    field_path: str
    latency_path: str
    pareto_y_direction: str
    display_direction: str
    negate_for_display: bool

    def for_display(self, raw_value: float) -> float:
        """Convert a raw stored value to a human-friendly display value."""
        return -raw_value if self.negate_for_display else raw_value


# ---------------------------------------------------------------------------
# Artifact access
# ---------------------------------------------------------------------------


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file; skip blank/malformed lines. Returns [] if file missing."""
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def read_text(path: Path, default: str = "") -> str:
    """Read a text file; return default if missing."""
    if not path.is_file():
        return default
    return path.read_text(encoding="utf-8", errors="replace")


def find_latest_attempt_log(artifacts_dir: Path, subdir: str, prefix: str) -> Path | None:
    """Find the highest-numbered attempt log under ``runs/<subdir>/<prefix>.attemptN.log``.

    The highest N is the last attempt (typically the successful one, since self-heal
    runs sequentially). Returns None if directory or logs absent.
    """
    log_dir = artifacts_dir / "runs" / subdir
    if not log_dir.is_dir():
        return None
    candidates = sorted(log_dir.glob(f"{prefix}.attempt*.log"))
    return candidates[-1] if candidates else None


def safe_float(raw: str) -> float | None:
    """Parse a Jinja-rendered numeric arg that might be empty or quoted."""
    raw = raw.strip().strip("\"'")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Record flattening + field discovery
# ---------------------------------------------------------------------------


def flatten_record(record: dict[str, Any]) -> dict[str, Any]:
    """Flatten nested dict one level (e.g. ``{"objs": {"acc": 0.9}}`` -> ``{"objs.acc": 0.9}``)."""
    flat: dict[str, Any] = {}
    for key, val in record.items():
        if isinstance(val, dict):
            for sub_key, sub_val in val.items():
                flat[f"{key}.{sub_key}"] = sub_val
        else:
            flat[key] = val
    return flat


def find_field(records: list[dict[str, Any]], candidates: tuple[str, ...]) -> str:
    """Return the first candidate field name that exists in the first record (flattened)."""
    if not records:
        return ""
    flat0 = flatten_record(records[0])
    for name in candidates:
        if name in flat0:
            return name
    return ""


def extract_numeric_values(records: list[dict[str, Any]], field_path: str) -> list[float]:
    """Extract all numeric values for ``field_path`` across records.

    Handles both flat and one-level-nested paths (e.g. ``objs.acc``).
    Skips records where the field is missing or non-numeric.
    """
    values: list[float] = []
    for rec in records:
        flat = flatten_record(rec)
        raw = flat.get(field_path)
        if raw is None:
            continue
        try:
            values.append(float(raw))
        except (ValueError, TypeError):
            continue
    return values


# ---------------------------------------------------------------------------
# Metric discovery (NAS-aware)
# ---------------------------------------------------------------------------


def _normalize_direction(raw: str) -> str:
    """Normalize various direction strings to ``"higher"`` or ``"lower"``."""
    s = raw.lower().strip()
    if any(s.startswith(p) for p in ("max", "high", "larger", "big")):
        return "higher"
    if any(s.startswith(p) for p in ("min", "low", "small", "neg")):
        return "lower"
    return "higher"


def discover_metric_info(
    artifacts_dir: Path, records: list[dict[str, Any]] | None = None
) -> MetricInfo | None:
    """Discover project metric metadata from search_config.yaml + records.

    NAS convention: ``objs`` is a list of strings; the non-latency entry is the
    metric name. Records store values nested under ``objs.<name>``. All stored
    objectives are smaller-is-better; higher-better metrics are negated.

    Returns None if the metric objective cannot be identified.
    """
    if records is None:
        records = read_jsonl(artifacts_dir / "search_results.jsonl")

    metric_name = _metric_objective_name(artifacts_dir)
    if not metric_name:
        # Fallback: infer from records (first non-latency numeric under objs.*).
        metric_name = _infer_metric_from_records(records)

    if not metric_name:
        return None

    # Resolve field paths (check nested objs.* first, then flat).
    latency_path = find_field(records, LATENCY_FIELDS) if records else ""
    metric_path = find_field(records, (f"objs.{metric_name}", metric_name)) if records else ""

    # Determine display polarity: if all stored metric values are <= 0,
    # they are negated higher-better metrics (NAS convention).
    negate = False
    display_direction = "higher"
    if records and metric_path:
        vals = extract_numeric_values(records, metric_path)
        if vals and all(v <= 0 for v in vals):
            negate = True
            display_direction = "higher"
        elif vals:
            # Positive values: lower-better metric stored as-is.
            display_direction = "lower"

    return MetricInfo(
        name=metric_name,
        field_path=metric_path,
        latency_path=latency_path,
        pareto_y_direction="min",  # NAS: all stored objectives smaller-is-better
        display_direction=display_direction,
        negate_for_display=negate,
    )


def _metric_objective_name(artifacts_dir: Path) -> str:
    """Read the non-latency objective name from ``search_config.yaml`` ``objs``.

    Handles both list-of-strings (``["acc", "latency"]``) and list-of-dicts formats.
    Returns "" if not found.
    """
    cfg_path = artifacts_dir / "search_config.yaml"
    text = read_text(cfg_path)
    if not text:
        return ""

    # Tier A: PyYAML authoritative parse.
    try:
        import yaml  # type: ignore[import-untyped]

        cfg = yaml.safe_load(text)
        objs = cfg.get("objs") if isinstance(cfg, dict) else None
        if isinstance(objs, list):
            for obj in objs:
                # list-of-strings format (the real NAS format).
                if isinstance(obj, str) and "latency" not in obj.lower():
                    return obj
                # list-of-dicts format (defensive fallback).
                if isinstance(obj, dict):
                    oname = str(obj.get("name", obj.get("metric", "")))
                    if oname and "latency" not in oname.lower():
                        return oname
        elif isinstance(objs, dict):
            for oname in objs:
                if "latency" not in str(oname).lower():
                    return str(oname)
    except ImportError:
        pass  # PyYAML not available -> regex fallback

    # Tier B: regex fallback for list-of-strings format.
    in_objs = False
    for line in text.splitlines():
        raw = line.strip()
        if raw.startswith("objs:"):
            in_objs = True
            continue
        if in_objs:
            if raw and not line[0].isspace() and not raw.startswith("-"):
                break
            # Match: - "acc"  or  - acc  or  - 'acc'
            m = re.match(r"-\s*['\"]?(\w+)['\"]?", raw)
            if m:
                name = m.group(1)
                if "latency" not in name.lower():
                    return name

    return ""


def _infer_metric_from_records(records: list[dict[str, Any]]) -> str:
    """Infer the metric objective name from records when search_config.yaml is unavailable.

    Looks for a nested ``objs`` dict and picks the first non-latency key.
    """
    if not records:
        return ""
    flat0 = flatten_record(records[0])
    # Prefer nested objs.* keys.
    for key in flat0:
        if key.startswith("objs."):
            name = key.split(".", 1)[1]
            if "latency" not in name.lower():
                return name
    return ""


# ---------------------------------------------------------------------------
# Loss log parsing
# ---------------------------------------------------------------------------


def parse_loss_log(log_path: Path) -> list[dict[str, float]]:
    """Parse a training log for ``(step, loss)`` pairs.

    Handles JSON lines, ``loss=X`` text, and epoch-based formats. Falls back to
    ordinal x-axis if no step/epoch token is found. Returns [] if no loss found.
    """
    if not log_path.is_file():
        return []

    points: list[dict[str, float]] = []
    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue

            # JSON log format: {"step": N, "loss": X}
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    step = rec.get("step", rec.get("global_step", rec.get("iteration")))
                    loss = rec.get("loss", rec.get("train_loss"))
                    if step is not None and loss is not None:
                        try:
                            points.append({"step": float(step), "loss": float(loss)})
                            continue
                        except (ValueError, TypeError):
                            pass
            except json.JSONDecodeError:
                pass

            # Text format: look for loss token.
            loss_match = re.search(
                r"(?:train_)?loss\s*[:=]\s*([\d.]+(?:[eE][+-]?\d+)?)", line, re.IGNORECASE
            )
            if not loss_match:
                continue
            try:
                loss_val = float(loss_match.group(1))
            except ValueError:
                continue

            step_match = re.search(
                r"(?:global_)?step\s*[:=]?\s*(\d+)", line, re.IGNORECASE
            )
            if step_match:
                step_val = float(step_match.group(1))
            else:
                epoch_match = re.search(r"epoch\s*[:=]?\s*(\d+)", line, re.IGNORECASE)
                if epoch_match:
                    step_val = float(epoch_match.group(1))
                else:
                    step_val = float(len(points) + 1)

            points.append({"step": step_val, "loss": loss_val})

    return points


# ---------------------------------------------------------------------------
# Chart push + result recording
# ---------------------------------------------------------------------------


def push_chart(
    *,
    artifacts_dir_path: Path,
    script_name: str,
    title: str,
    chart_type: str,
    label: str,
    data: list[dict[str, Any]],
    skip_reason: str = "",
    **render_kwargs: Any,
) -> None:
    """Push one chart via render_chart, then record the result to the marker file.

    Args:
        label: Dedup/grouping key (e.g. ``"nas-supernet/search"``). Same ``label`` +
            different ``title`` → independent charts under one fold; same label +
            same title → front-end replaces (live-update semantic).

    Fail-soft: if render_chart is unavailable or raises, records status="skipped"
    with the error message and continues (design-charts H1).
    """
    if not data and not skip_reason:
        skip_reason = "empty data (artifact produced no records)"

    if skip_reason:
        sys.stderr.write(f"[{script_name}] skipped: {skip_reason}\n")
        _record_result(artifacts_dir_path, script_name, title, chart_type, "skipped", reason=skip_reason)
        return

    if render_chart is None:
        sys.stderr.write(f"[{script_name}] orca.chart not available (outside Orca run?)\n")
        _record_result(
            artifacts_dir_path, script_name, title, chart_type, "skipped",
            reason="orca.chart.render_chart unavailable",
        )
        return

    try:
        seq = render_chart(
            chart_type=chart_type, data=data, label=label, title=title, **render_kwargs,
        )
        print(f"[{script_name}] pushed '{title}', seq={seq}", flush=True)
        _record_result(artifacts_dir_path, script_name, title, chart_type, "pushed", seq=seq)
    except Exception as exc:  # noqa: BLE001 -- fail-soft per chart
        sys.stderr.write(f"[{script_name}] render_chart failed for '{title}': {exc}\n")
        _record_result(
            artifacts_dir_path, script_name, title, chart_type, "skipped", reason=str(exc),
        )


def _record_result(
    artifacts_dir_path: Path,
    name: str,
    title: str,
    chart_type: str,
    status: str,
    seq: int = 0,
    reason: str = "",
) -> None:
    """Append one chart result line to the marker JSONL file."""
    result: dict[str, Any] = {
        "name": name,
        "title": title,
        "chart_type": chart_type,
        "status": status,
    }
    if status == "pushed":
        result["seq"] = seq
    if reason:
        result["reason"] = reason

    marker = artifacts_dir_path / CHART_MARKER
    with marker.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(result, ensure_ascii=False) + "\n")


def init_marker(artifacts_dir_path: Path) -> None:
    """Truncate the marker file at the start of a run (idempotent)."""
    marker = artifacts_dir_path / CHART_MARKER
    marker.write_text("", encoding="utf-8")


def run_inspect_supernet(artifacts_dir_path: Path, timeout_s: int = 60) -> str:
    """Best-effort run of ``inspect_supernet.py`` to harvest params/FLOPs/latency.

    Returns captured stdout+stderr (parseable text). Empty string on any failure.
    This is read-only (inspect_supernet is designed to print, not mutate).
    """
    script = artifacts_dir_path / "inspect_supernet.py"
    if not script.is_file():
        return ""
    try:
        result = subprocess.run(
            ["python3", str(script)],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(artifacts_dir_path),
        )
        return (result.stdout or "") + (result.stderr or "")
    except Exception:  # noqa: BLE001 -- best-effort harvest
        return ""
