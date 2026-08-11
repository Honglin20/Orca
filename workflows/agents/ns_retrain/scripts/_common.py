"""_common.py -- shared utilities for nas-supernet chart scripts.

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
  crashes the orchestrator.
- Static-file fallback: when the Orca chart socket is unavailable (headless / post-run
  rendering), push_chart writes a self-contained static file (plotly HTML, matplotlib
  PNG fallback) under ``<artifacts>/charts/<script_name>.{html,png}`` and records
  status="rendered_static" with the path. Lets runs whose charts were skipped after
  completion be re-rendered from their own artifacts. Static path
  supports chart_type subset {line, bar, pareto/scatter, table}; other live types
  (area/radar/heatmap) raise _UnsupportedChartType and are skipped loudly.
- Metric name + direction discovered from search_config.yaml objs (authoritative) ->
  project_manifest.md fallback -> generic.
"""

from __future__ import annotations

import json
import math
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
CHART_MARKER = ".nas-supernet_charts.jsonl"

# Latency unit whitelist: search_record_schema.json ``latency_unit`` must be
# one of these; any other value (or missing key) falls back to ``"ms"`` (default path
# is ms-only; non-ms declaration requires user latency script — enforced at bootstrap).
LATENCY_UNITS: frozenset[str] = frozenset({"ms", "us", "s"})

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
        latency_unit: Declared latency unit (``ms``/``us``/``s``) from
            ``search_record_schema.json``. Labels/columns/captions carry
            this unit. Default ``"ms"`` when schema is missing the key (back-compat with
            older runs). Note: values are NOT converted across units.
    """

    name: str
    field_path: str
    latency_path: str
    pareto_y_direction: str
    display_direction: str
    negate_for_display: bool
    latency_unit: str = "ms"

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


def best_val_metric_from_log(
    artifacts_dir: Path, metric_name: str, display_direction: str
) -> float | None:
    """Parse the best validation metric from the latest training attempt log.

    Training logs use ACTUAL metric values (not NAS-negated). ``best`` is
    determined by ``display_direction``: higher -> max, lower -> min. Handles both
    JSON-log lines and ``metric=value`` text (regex fallback). Returns None if the
    log is missing or contains no parseable metric.
    """
    log_path = find_latest_attempt_log(artifacts_dir, "train", "train")
    if log_path is None:
        return None

    text = read_text(log_path)
    if not text:
        return None

    best: float | None = None
    mn_lower = metric_name.lower()

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        val: float | None = None

        # JSON line.
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                for key in (f"val_{metric_name}", f"test_{metric_name}", metric_name, "val_metric", "best_metric"):
                    if key in rec:
                        try:
                            val = float(rec[key])
                            break
                        except (ValueError, TypeError):
                            pass
        except json.JSONDecodeError:
            pass

        # Regex fallback on text.
        if val is None:
            for pattern in (
                rf"val_{re.escape(mn_lower)}\s*[:=]\s*([\d.eE+-]+)",
                rf"{re.escape(mn_lower)}\s*[:=]\s*([\d.eE+-]+)",
            ):
                m = re.search(pattern, line, re.IGNORECASE)
                if m:
                    try:
                        val = float(m.group(1))
                        break
                    except ValueError:
                        pass

        if val is not None:
            # NaN/overflow sentinel filter: math.isfinite catches NaN/inf (IEEE-754
            # abs(NaN)>=1e6 is False, so isfinite is essential); abs>=1e6 catches
            # float32-max (3.4e38) overflow sentinels from failed evals.
            if not math.isfinite(val) or abs(val) >= 1e6:
                continue
            if best is None:
                best = val
            elif display_direction == "higher" and val > best:
                best = val
            elif display_direction == "lower" and val < best:
                best = val

    return best


def final_metric_from_json(artifacts_dir: Path, metric_name: str) -> float | None:
    """Read the retrain final test metric from ``runs/retrain/test_metrics.json``.

    Returns the raw stored value. Retrain scripts typically write un-negated values
    (e.g. 0.92 for accuracy), so the caller should NOT double-negate.
    """
    metrics_path = artifacts_dir / "runs" / "retrain" / "test_metrics.json"
    if not metrics_path.is_file():
        return None
    try:
        data = json.loads(metrics_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None

    for key in (metric_name, f"test_{metric_name}", f"val_{metric_name}", "test_metric", "best_metric", "metric"):
        if key in data:
            try:
                return float(data[key])
            except (ValueError, TypeError):
                pass

    # Fallback: first numeric value that is not loss.
    for key, val in data.items():
        if "loss" in key.lower():
            continue
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            try:
                return float(val)
            except (ValueError, TypeError):
                continue
    return None


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


def discover_latency_unit(artifacts_dir: Path) -> str:
    """Read the declared latency unit from ``search_record_schema.json``.

    Schema key ``latency_unit`` (string enum ``ms``/``us``/``s``). Missing key
    or non-whitelist value → ``"ms"`` (back-compat with older runs; illegal value also
    falls back + stderr so the misconfig is observable). Pure function — no side effects
    beyond the stderr warning on illegal values.

    Note: ``latency_ms_field`` key name is **frozen** (kept as-is for
    back-compat; only ``latency_unit`` was added). This function does NOT read or rewrite
    ``latency_ms_field``.
    """
    schema_path = artifacts_dir / "search_record_schema.json"
    if not schema_path.is_file():
        return "ms"
    try:
        data = json.loads(schema_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "ms"
    if not isinstance(data, dict):
        return "ms"
    raw = data.get("latency_unit")
    if not isinstance(raw, str):
        return "ms"
    unit = raw.strip().lower()
    if unit not in LATENCY_UNITS:
        sys.stderr.write(
            f"[_common] search_record_schema.json latency_unit={raw!r} not in "
            f"{sorted(LATENCY_UNITS)}; falling back to 'ms'\n"
        )
        return "ms"
    return unit


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

    # Determine display polarity: if all (valid) stored metric values are <= 0,
    # they are negated higher-better metrics (NAS convention). Invalid values
    # (e.g. NaN encoded as float32 max 3.4e38 from a failed evaluator run) are
    # excluded — they would otherwise flip the sign heuristics below. The garbage
    # threshold targets overflow sentinels only, never legitimate metrics whose
    # magnitude can exceed 1 (reward, BLEU, ...). If every value is garbage,
    # fall back to the raw set so the legacy heuristic still runs.
    negate = False
    display_direction = "higher"
    if records and metric_path:
        vals = extract_numeric_values(records, metric_path)
        valid = [v for v in vals if abs(v) < 1e6]
        if not valid:
            valid = vals
        if valid and all(v <= 0 for v in valid):
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
        latency_unit=discover_latency_unit(artifacts_dir),
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
# Static file rendering fallback (headless / post-run)
# ---------------------------------------------------------------------------

# Static chart files land under ``<artifacts>/<this subdir>/<script_name>.<ext>``.
STATIC_CHARTS_SUBDIR = "charts"

# Cap on rows rendered into a static matplotlib table (unreadable beyond this).
_MPL_TABLE_ROW_CAP = 30


class _UnsupportedChartType(ValueError):
    """Raised when neither plotly nor matplotlib static path supports a chart_type.

    Carries the offending chart_type so push_chart can record a precise reason
    instead of a generic "render failed". Subclasses ValueError for backward
    compatibility with any caller that catches ValueError on render.
    """


def _charts_dir(artifacts_dir_path: Path) -> Path:
    """Create + return ``<artifacts>/charts/`` (idempotent)."""
    out = artifacts_dir_path / STATIC_CHARTS_SUBDIR
    out.mkdir(parents=True, exist_ok=True)
    return out


def _series(data: list[dict[str, Any]], key: str) -> list[Any]:
    """Project one field across all records (missing -> "")."""
    return [d.get(key, "") for d in data]


def _fmt_cell(val: Any) -> str:
    """Format a cell value for static table rendering (mirrors search_table._to_str)."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "yes" if val else ""
    if isinstance(val, float):
        return f"{val:.4f}".rstrip("0").rstrip(".") or "0"
    return str(val)


def _compute_pareto_front(
    data: list[dict[str, Any]],
    x_key: str,
    y_key: str,
    x_dir: str,
    y_dir: str,
) -> list[int]:
    """Return indices of non-dominated points under (x_dir, y_dir) in {"min","max"}.

    A point p is dominated if some q is at-least-as-good in both axes and strictly
    better in at least one. Duplicate points (same x,y) are de-duplicated by keeping
    only the smallest index (others treated as dominated) so the front does not
    collapse to all-duplicates when ties exist. Used only for static rendering
    (the live front-end computes the front itself).
    """
    pts: list[tuple[int, float, float]] = []
    for i, rec in enumerate(data):
        try:
            pts.append((i, float(rec[x_key]), float(rec[y_key])))
        except (KeyError, ValueError, TypeError):
            continue
    front: list[int] = []
    for i, xi, yi in pts:
        dominated = False
        for j, xj, yj in pts:
            if j == i:
                continue
            # Duplicate-coord de-duplication: a later identical point is dominated
            # by the earlier one (keeps the smallest-index representative).
            if xj == xi and yj == yi:
                if j < i:
                    dominated = True
                    break
                else:
                    continue
            x_le = (xj <= xi) if x_dir != "max" else (xj >= xi)
            y_le = (yj <= yi) if y_dir != "max" else (yj >= yi)
            x_strict = (xj < xi) if x_dir != "max" else (xj > xi)
            y_strict = (yj < yi) if y_dir != "max" else (yj > yi)
            if x_le and y_le and (x_strict or y_strict):
                dominated = True
                break
        if not dominated:
            front.append(i)
    return front


def _parse_selected_from_caption(caption: str) -> tuple[float, float] | None:
    """Best-effort extract selected (latency, metric_display) from a pareto.py caption.

    Matches ``"Selected arch: latency=X.XX<unit>, <metric>=Y.YYYY."`` (case-insensitive)
    where ``<unit>`` ∈ {ms, us, s}. The numeric groups use a strict
    ``\\d+(?:\\.\\d+)?`` pattern so the trailing sentence period is NOT swallowed
    (``[\\d.]+`` would eat it and break float()). Returns None if the caption does not
    carry selected coords.
    """
    m = re.search(
        r"selected arch:.*?latency\s*=\s*(\d+(?:\.\d+)?)\s*(?:ms|us|s)\s*,\s*\w+\s*=\s*(-?\d+(?:\.\d+)?)",
        caption,
        re.IGNORECASE,
    )
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None


def _render_static(
    artifacts_dir_path: Path,
    script_name: str,
    title: str,
    chart_type: str,
    data: list[dict[str, Any]],
    render_kwargs: dict[str, Any],
) -> tuple[Path, str] | tuple[None, str]:
    """Render one chart to a static file when the Orca chart socket is unavailable.

    Prefers plotly HTML (self-contained, interactive in a browser). Falls back to
    matplotlib PNG if plotly is not installed. Returns ``(path, fmt)`` on success,
    ``(None, reason)`` on failure (caller records "skipped" with the reason).
    Fail-soft: never raises. ``_UnsupportedChartType`` from plotly short-circuits
    the matplotlib fallback (unsupported types are unsupported in both backends).
    """
    plotly_reason = ""
    try:
        return _render_plotly(
            artifacts_dir_path, script_name, title, chart_type, data, render_kwargs,
        )
    except _UnsupportedChartType as exc:
        # Unrecoverable for both backends -> skip matplotlib, fail fast.
        sys.stderr.write(
            f"[{script_name}] static render unsupported for '{title}': {exc}\n"
        )
        return None, str(exc)
    except ImportError:
        plotly_reason = "plotly not installed"
        sys.stderr.write(
            f"[{script_name}] plotly unavailable; falling back to matplotlib PNG\n"
        )
    except Exception as exc:  # noqa: BLE001 -- fail-soft: try matplotlib next
        plotly_reason = f"plotly failed: {exc}"
        sys.stderr.write(
            f"[{script_name}] plotly render failed for '{title}': {exc}; "
            f"trying matplotlib\n"
        )
    try:
        return _render_matplotlib(
            artifacts_dir_path, script_name, title, chart_type, data, render_kwargs,
        )
    except _UnsupportedChartType as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 -- fail-soft: caller records "skipped"
        mpl_reason = f"matplotlib failed: {exc}"
        sys.stderr.write(f"[{script_name}] matplotlib render failed for '{title}': {exc}\n")
        combined = f"{plotly_reason}; {mpl_reason}" if plotly_reason else mpl_reason
        return None, combined


def _render_plotly(
    artifacts_dir_path: Path,
    script_name: str,
    title: str,
    chart_type: str,
    data: list[dict[str, Any]],
    kw: dict[str, Any],
) -> tuple[Path, str]:
    """Render a self-contained plotly HTML file. Raises on failure (caller fail-soft)."""
    import html  # noqa: PLC0415 -- stdlib, imported lazily for escape
    import plotly.graph_objects as go  # noqa: PLC0415 -- optional dep

    x = kw.get("x", "")
    y = kw.get("y", "")
    x_label = kw.get("x_label") or x or "x"
    y_label = kw.get("y_label") or y or "y"
    caption = kw.get("caption", "")

    fig = go.Figure()
    if chart_type == "line":
        fig.add_trace(go.Scatter(
            x=_series(data, x), y=_series(data, y), mode="lines+markers", name=title,
        ))
    elif chart_type == "bar":
        fig.add_trace(go.Bar(x=_series(data, x), y=_series(data, y), name=title))
    elif chart_type in ("pareto", "scatter"):
        xs = _series(data, x)
        ys = _series(data, y)
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="markers", name="candidates"))
        front = _compute_pareto_front(
            data, x, y,
            kw.get("pareto_x_direction", "min"),
            kw.get("pareto_y_direction", "min"),
        )
        if front:
            fig.add_trace(go.Scatter(
                x=[xs[i] for i in front], y=[ys[i] for i in front],
                mode="markers",
                marker=dict(size=13, color="crimson", symbol="star"),
                name="pareto front",
            ))
        sel = _parse_selected_from_caption(caption)
        if sel is not None:
            fig.add_trace(go.Scatter(
                x=[sel[0]], y=[sel[1]], mode="markers",
                marker=dict(
                    size=16, color="gold", symbol="x",
                    line=dict(width=2, color="black"),
                ),
                name="selected",
            ))
    elif chart_type == "table":
        columns = kw.get("columns") or (list(data[0].keys()) if data else [])
        header_vals = [c for c in columns]
        cell_vals = [[_fmt_cell(d.get(c, "")) for d in data] for c in columns]
        fig = go.Figure(data=[go.Table(
            header=dict(
                values=header_vals, align="left",
                fill_color="paleturquoise",
                font=dict(size=12, color="black"),
            ),
            cells=dict(values=cell_vals, align="left", height=22),
        )])
    else:
        raise _UnsupportedChartType(
            f"static plotly render does not support chart_type={chart_type!r} "
            f"(supported: line, bar, pareto/scatter, table)"
        )

    # HTML-escape title/caption so a metric name containing '<'/'&' cannot break
    # the page. The <br><sup> wrapper is intentional structure (kept as-is).
    esc_caption = html.escape(caption) if caption else ""
    full_title = html.escape(title) + (f"<br><sup>{esc_caption}</sup>" if caption else "")
    fig.update_layout(
        title=dict(text=full_title),
        xaxis_title=x_label if chart_type != "table" else None,
        yaxis_title=y_label if chart_type != "table" else None,
        font=dict(size=12),
        legend=dict(orientation="h", y=-0.25) if chart_type != "table" else None,
    )
    out = _charts_dir(artifacts_dir_path) / f"{script_name}.html"
    # include_plotlyjs=True -> self-contained HTML (viewable offline).
    fig.write_html(str(out), include_plotlyjs=True, full_html=True, auto_open=False)
    return out, "html"


def _render_matplotlib(
    artifacts_dir_path: Path,
    script_name: str,
    title: str,
    chart_type: str,
    data: list[dict[str, Any]],
    kw: dict[str, Any],
) -> tuple[Path, str]:
    """Render a static PNG via matplotlib. Raises on failure (caller fail-soft)."""
    import matplotlib  # noqa: PLC0415 -- optional dep

    matplotlib.use("Agg")  # headless-safe backend
    import matplotlib.pyplot as plt  # noqa: PLC0415 -- after backend set

    x = kw.get("x", "")
    y = kw.get("y", "")
    x_label = kw.get("x_label") or x
    y_label = kw.get("y_label") or y
    caption = kw.get("caption", "")

    fig, ax = plt.subplots(figsize=(11, 6.5))
    if chart_type == "line":
        ax.plot(_series(data, x), _series(data, y), "-o", linewidth=1.5, markersize=4)
    elif chart_type == "bar":
        xs = _series(data, x)
        ys = _series(data, y)
        ax.bar(range(len(data)), ys)
        ax.set_xticks(range(len(data)))
        ax.set_xticklabels([str(v) for v in xs], rotation=45, ha="right", fontsize=8)
    elif chart_type in ("pareto", "scatter"):
        xs = _series(data, x)
        ys = _series(data, y)
        ax.scatter(xs, ys, label="candidates", s=25)
        front = _compute_pareto_front(
            data, x, y,
            kw.get("pareto_x_direction", "min"),
            kw.get("pareto_y_direction", "min"),
        )
        if front:
            ax.scatter(
                [xs[i] for i in front], [ys[i] for i in front],
                c="crimson", s=180, marker="*", label="pareto front",
            )
        sel = _parse_selected_from_caption(caption)
        if sel is not None:
            ax.scatter(
                [sel[0]], [sel[1]], c="gold", s=220, marker="X",
                edgecolors="black", linewidths=1.5, label="selected",
            )
        ax.legend(loc="best", fontsize=9)
    elif chart_type == "table":
        ax.axis("off")
        columns = kw.get("columns") or (list(data[0].keys()) if data else [])
        capped = data[:_MPL_TABLE_ROW_CAP]
        rows = [[_fmt_cell(d.get(c, "")) for c in columns] for d in capped]
        tbl = ax.table(
            cellText=rows, colLabels=[str(c) for c in columns],
            loc="center", cellLoc="left",
        )
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(8)
        tbl.scale(1, 1.35)
        omitted = len(data) - len(capped)
        if omitted > 0:
            ax.set_title(
                f"{title}\n(matplotlib PNG fallback: showing first {_MPL_TABLE_ROW_CAP} "
                f"of {len(data)} rows; {omitted} omitted — see plotly HTML for full table)",
            )
        else:
            ax.set_title(title)
    else:
        raise _UnsupportedChartType(
            f"static matplotlib render does not support chart_type={chart_type!r} "
            f"(supported: line, bar, pareto/scatter, table)"
        )

    if chart_type != "table":
        ax.set_title(title + (f"\n{caption}" if caption else ""), fontsize=11)
        ax.set_xlabel(x_label)
        ax.set_ylabel(y_label)
    fig.tight_layout()
    out = _charts_dir(artifacts_dir_path) / f"{script_name}.png"
    fig.savefig(str(out), dpi=120, bbox_inches="tight")
    plt.close(fig)
    return out, "png"


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

    Fail-soft: on missing/empty data → status="skipped". On
    render_chart unavailable OR failure (e.g. Orca chart socket gone in headless /
    post-run rendering), falls back to a static file under
    ``<artifacts>/charts/<script_name>.{html,png}`` via ``_render_static`` and
    records status="rendered_static" + path. Only if the static fallback also
    fails does it record status="skipped". Never raises.
    """
    if not data and not skip_reason:
        skip_reason = "empty data (artifact produced no records)"

    if skip_reason:
        sys.stderr.write(f"[{script_name}] skipped: {skip_reason}\n")
        _record_result(artifacts_dir_path, script_name, title, chart_type, "skipped", reason=skip_reason)
        return

    # 1. Try the live Orca chart socket first.
    if render_chart is not None:
        try:
            seq = render_chart(
                chart_type=chart_type, data=data, label=label, title=title, **render_kwargs,
            )
            print(f"[{script_name}] pushed '{title}', seq={seq}", flush=True)
            _record_result(artifacts_dir_path, script_name, title, chart_type, "pushed", seq=seq)
            return
        except Exception as exc:  # noqa: BLE001 -- fall through to static fallback
            sys.stderr.write(
                f"[{script_name}] render_chart failed for '{title}': {exc}; "
                f"falling back to static file\n"
            )
            static_reason = str(exc)
    else:
        sys.stderr.write(
            f"[{script_name}] orca.chart not available; using static file fallback\n"
        )
        static_reason = "orca.chart.render_chart unavailable"

    # 2. Static file fallback (headless / post-run).
    path_fmt = _render_static(
        artifacts_dir_path, script_name, title, chart_type, data, render_kwargs,
    )
    if path_fmt[0] is not None:
        path, fmt = path_fmt
        sys.stderr.write(
            f"[{script_name}] rendered static '{title}' -> {path} ({fmt})\n"
        )
        _record_result(
            artifacts_dir_path, script_name, title, chart_type, "rendered_static",
            path=str(path), fmt=fmt,
        )
        return

    # 3. Both paths failed -> skipped. Carry BOTH the live + static failure
    #    reasons so the marker JSONL is self-explanatory (no silent distortion).
    static_fail_reason = path_fmt[1]
    combined_reason = (
        f"live=[{static_reason}]; static=[{static_fail_reason}]"
        if static_fail_reason else static_reason
    )
    sys.stderr.write(
        f"[{script_name}] static fallback also failed for '{title}'\n"
    )
    _record_result(
        artifacts_dir_path, script_name, title, chart_type, "skipped", reason=combined_reason,
    )


def _record_result(
    artifacts_dir_path: Path,
    name: str,
    title: str,
    chart_type: str,
    status: str,
    seq: int = 0,
    reason: str = "",
    path: str = "",
    fmt: str = "",
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
    if status == "rendered_static":
        result["path"] = path
        result["fmt"] = fmt
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
