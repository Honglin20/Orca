#!/usr/bin/env python3
"""search_space_table.py -- push the generated SearchSpace as a table chart.

Deterministic sidecar for ns3_expand_supernet: reads ``supernet.py`` from the
artifacts dir, execs it, instantiates ``SearchSpace``, flattens its searchable
levers into one row per (stage, block choice), and pushes a ``table`` chart via
``orca.chart.render_chart`` (label=``nas-supernet/search-space``).

Covers all three supported model-type shapes:
  - staged (CNN / hierarchical transformer): ``stage_names`` + ``stage_widths``
    (or ``stage_emb_dims``) + ``stage_depth_candidates`` + ``stage_layer_configs``
  - isotropic transformer: ``global_dim`` / ``head_dim`` + ``depth_candidates``
    + ``layer_configs``

Fail-soft (H1/H7): no supernet.py / exec or instantiation failure / no searchable
levers / render_chart unavailable or raising → stderr + exit 0 (never blocks the
node; the agent.md call site appends ``|| true``). When the live chart socket is
unavailable, falls back to a self-contained ``charts/search_space_table.html`` so
headless runs still leave a viewable artifact (ns3_report scans ``charts/``).

Deterministic: no LLM, no network beyond the chart socket, no clock, no random.
Deterministic table row ordering = definition order (staged) / sorted keys
(isotropic layer_configs dict) + sorted config param names.

Pure extraction helpers (``_extract_rows`` / ``_fmt_config``) take a plain
SearchSpace-like object so unit tests can feed a mock without torch.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

# orca.chart web push (render_chart API). Not in an Orca subprocess → None.
try:
    from orca.chart import render_chart as _render_chart  # type: ignore[import-untyped]
except ImportError:
    _render_chart = None

# Chart result marker, same file the run_search chart scripts append to
# (ns3_report reads it to build charts_summary when no static files exist).
_MARKER = ".nas-supernet_charts.jsonl"

_LABEL = "nas-supernet/search-space"
_TITLE = "Search Space"
_COLUMNS = ["stage", "block", "depth", "fixed", "config"]


# ---------------------------------------------------------------------------
# SearchSpace loading (exec supernet.py — same approach as check_expand.sh)
# ---------------------------------------------------------------------------


def _load_search_space(artifacts_dir: Path) -> tuple[Any | None, str]:
    """exec ``supernet.py`` and instantiate ``SearchSpace``.

    Mirrors check_expand.sh's exec-based probe: compiles + execs the module, grabs
    ``SearchSpace`` (or ``build_supernet``). Instantiation is cheap (dataclass
    defaults, no forward). Returns ``(None, reason)`` on any failure so the caller
    fail-softs instead of crashing the node.
    """
    supernet_path = artifacts_dir / "supernet.py"
    if not supernet_path.is_file():
        return None, "supernet.py missing"

    ns: dict[str, Any] = {}
    try:
        src = supernet_path.read_text(encoding="utf-8", errors="replace")
        exec(compile(src, str(supernet_path), "exec"), ns)  # noqa: S102 -- same as check_expand.sh gate
    except Exception as exc:  # noqa: BLE001 -- fail-soft: report and skip
        return None, f"exec supernet.py failed: {exc}"

    ss_cls = ns.get("SearchSpace")
    try:
        if ss_cls is not None:
            ss = ss_cls()
        else:
            builder = ns.get("build_supernet")
            if builder is None:
                return None, "supernet.py exposes neither SearchSpace nor build_supernet"
            ss = builder()
    except Exception as exc:  # noqa: BLE001 -- fail-soft: report and skip
        return None, f"SearchSpace/build_supernet instantiation failed: {exc}"
    return ss, ""


# ---------------------------------------------------------------------------
# Table extraction (pure — testable with a mock SearchSpace)
# ---------------------------------------------------------------------------


def _fmt_tuple(values: Any) -> str:
    """Render a tuple/list candidate set as ``1, 2, 3``; scalars as str."""
    if values is None:
        return ""
    if isinstance(values, (list, tuple)):
        parts = [str(v) for v in values]
        return ", ".join(parts)
    return str(values)


def _fmt_config(cfg_space: Any) -> str:
    """Render a block's config space as ``param=1, 2; other=3`` (sorted keys)."""
    if not isinstance(cfg_space, dict):
        return str(cfg_space)
    parts: list[str] = []
    for param in sorted(cfg_space):
        parts.append(f"{param}={_fmt_tuple(cfg_space[param])}")
    return "; ".join(parts)


def _extract_rows(ss: Any) -> tuple[list[dict[str, str]], str]:
    """Flatten a SearchSpace into table rows: one per (stage, block choice).

    Returns ``(rows, "")`` on success, ``([], reason)`` when no searchable levers
    are found (caller records "skipped"). Both staged (``stage_layer_configs``)
    and isotropic (``layer_configs``) shapes are handled.
    """
    rows: list[dict[str, str]] = []

    staged_cfgs = getattr(ss, "stage_layer_configs", None)
    if staged_cfgs:
        names = getattr(ss, "stage_names", ()) or ()
        widths = getattr(ss, "stage_widths", None)
        emb_dims = getattr(ss, "stage_emb_dims", None)
        depths = getattr(ss, "stage_depth_candidates", None)
        for i, stage_cfg in enumerate(staged_cfgs):
            if not isinstance(stage_cfg, dict):
                continue
            stage_name = names[i] if i < len(names) else f"stage{i + 1}"
            depth = _fmt_tuple(depths[i]) if depths and i < len(depths) else ""
            fixed = ""
            if widths is not None and i < len(widths):
                fixed = f"width={widths[i]}"
            elif emb_dims is not None and i < len(emb_dims):
                fixed = f"emb_dim={emb_dims[i]}"
            for choice, cfg in stage_cfg.items():
                rows.append({
                    "stage": stage_name,
                    "block": str(choice),
                    "depth": depth,
                    "fixed": fixed,
                    "config": _fmt_config(cfg),
                })
        if not rows:
            return [], "stage_layer_configs present but empty"
        return rows, ""

    layer_cfgs = getattr(ss, "layer_configs", None)
    if layer_cfgs:
        depth = _fmt_tuple(getattr(ss, "depth_candidates", ()))
        fixed_parts: list[str] = []
        for attr in ("global_dim", "head_dim", "d_model"):
            v = getattr(ss, attr, None)
            if v is not None:
                fixed_parts.append(f"{attr}={v}")
        fixed = "; ".join(fixed_parts)
        for choice, cfg in layer_cfgs.items():
            rows.append({
                "stage": "isotropic",
                "block": str(choice),
                "depth": depth,
                "fixed": fixed,
                "config": _fmt_config(cfg),
            })
        if not rows:
            return [], "layer_configs present but empty"
        return rows, ""

    return [], "no stage_layer_configs or layer_configs in SearchSpace"


# ---------------------------------------------------------------------------
# Chart push + marker recording
# ---------------------------------------------------------------------------


def _record(artifacts_dir: Path, result: dict[str, Any]) -> None:
    """Append one chart result line to the marker JSONL (best-effort)."""
    try:
        with (artifacts_dir / _MARKER).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result, ensure_ascii=False) + "\n")
    except OSError as exc:
        sys.stderr.write(f"[search_space_table] marker write failed (non-blocking): {exc}\n")


def _write_static_html(
    artifacts_dir: Path, columns: list[str], rows: list[dict[str, str]], title: str,
) -> Path:
    """Render a self-contained plain-HTML table under ``charts/`` (no plotly)."""
    charts_dir = artifacts_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    out = charts_dir / "search_space_table.html"

    def esc(v: Any) -> str:
        return html.escape(str(v))

    body = ["<!DOCTYPE html><html><head><meta charset='utf-8'>",
            f"<title>{esc(title)}</title></head><body>",
            f"<h2>{esc(title)}</h2>",
            "<table border='1' cellspacing='0' cellpadding='4'>",
            "<thead><tr>" + "".join(f"<th>{esc(c)}</th>" for c in columns) + "</tr></thead>",
            "<tbody>"]
    for r in rows:
        body.append("<tr>" + "".join(f"<td>{esc(r.get(c, ''))}</td>" for c in columns) + "</tr>")
    body.append("</tbody></table></body></html>")
    out.write_text("".join(body), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Push SearchSpace table chart.")
    ap.add_argument("--artifacts-dir", required=True, help="$ORCA_ARTIFACTS_DIR")
    args = ap.parse_args()
    ad = Path(args.artifacts_dir)

    ss, err = _load_search_space(ad)
    if ss is None:
        sys.stderr.write(f"[search_space_table] skipped: {err}\n")
        _record(ad, {"name": "search_space_table", "title": _TITLE, "chart_type": "table",
                     "status": "skipped", "reason": err})
        return 0

    rows, err2 = _extract_rows(ss)
    if not rows:
        sys.stderr.write(f"[search_space_table] skipped: {err2}\n")
        _record(ad, {"name": "search_space_table", "title": _TITLE, "chart_type": "table",
                     "status": "skipped", "reason": err2})
        return 0

    caption = (f"SearchSpace from supernet.py: {len(rows)} (stage, block choice) "
               f"searchable levers; config values are the candidate grids.")

    # 1. Live chart socket first.
    if _render_chart is not None:
        try:
            seq = _render_chart(
                chart_type="table", data=rows, label=_LABEL, title=_TITLE,
                columns=_COLUMNS, caption=caption,
            )
            print(f"[search_space_table] pushed '{_TITLE}', seq={seq}", flush=True)
            _record(ad, {"name": "search_space_table", "title": _TITLE, "chart_type": "table",
                         "status": "pushed", "seq": seq})
            return 0
        except Exception as exc:  # noqa: BLE001 -- fail-soft: fall back to static
            sys.stderr.write(
                f"[search_space_table] render_chart failed for '{_TITLE}': {exc}; "
                f"falling back to static file\n"
            )
    else:
        sys.stderr.write("[search_space_table] orca.chart unavailable; using static file fallback\n")

    # 2. Static fallback (headless / post-run rendering).
    try:
        path = _write_static_html(ad, _COLUMNS, rows, _TITLE)
        sys.stderr.write(f"[search_space_table] rendered static '{_TITLE}' -> {path}\n")
        _record(ad, {"name": "search_space_table", "title": _TITLE, "chart_type": "table",
                     "status": "rendered_static", "path": str(path), "fmt": "html"})
    except Exception as exc:  # noqa: BLE001 -- fail-soft: record skipped
        sys.stderr.write(f"[search_space_table] static render failed for '{_TITLE}': {exc}\n")
        _record(ad, {"name": "search_space_table", "title": _TITLE, "chart_type": "table",
                     "status": "skipped", "reason": f"static render failed: {exc}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
