#!/usr/bin/env python3
"""search_space_table.py -- push the generated SearchSpace as a table chart.

Deterministic sidecar for psu_expand_supernet: reads ``supernet.py`` from the
artifacts dir, execs it, instantiates ``SearchSpace``, flattens the choice-only
search space into one row per (slot, branch), and pushes a ``table`` chart via
``orca.chart.render_chart`` (label=``puzzle-supernet/search-space``).

Choice-only shape: the searchable dimension is the per-slot branch choice
(``branch_choices``); all dimensions (``depth`` / widths / head layout /
sequence length) are pinned scalars shown as a fixed-dims column — no candidate
grids to expand.

Fail-soft: no supernet.py / exec or instantiation failure / no branch set /
render_chart unavailable or raising → stderr + exit 0 (never blocks the node;
the agent.md call site appends ``|| true``). When the live chart socket is
unavailable, falls back to a self-contained ``charts/search_space_table.html``
so headless runs still leave a viewable artifact (psu_report scans ``charts/``).

Deterministic: no LLM, no network beyond the chart socket, no clock, no random.
Deterministic table row ordering = slot index × branch enumeration order.

Pure extraction helpers (``_extract_rows``) take a plain SearchSpace-like object
so unit tests can feed a mock without torch.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

# repo 根 bootstrap：cwd=artifacts + 无 PYTHONPATH 时 orca.chart 仍可 import（orca.chart
# 只依赖 stdlib）。幂等：仓库根不含 orca 包 / 已在 sys.path 时不动。
_REPO_ROOT = Path(__file__).resolve().parents[5]
if (_REPO_ROOT / "orca" / "__init__.py").is_file() and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# orca.chart web push (render_chart API). Not in an Orca subprocess → None.
try:
    from orca.chart import render_chart as _render_chart  # type: ignore[import-untyped]
except ImportError:
    _render_chart = None

# Chart result marker, same file the run_search chart scripts append to
# (psu_report reads it to build charts_summary when no static files exist).
_MARKER = ".psu_charts.jsonl"

_LABEL = "puzzle-supernet/search-space"
_TITLE = "Search Space"
_COLUMNS = ["slot", "branch", "fixed"]

# 钉死维度展示列（标量属性，缺哪个跳过哪个）。
_PINNED_ATTRS = ("depth", "global_dim", "head_dim", "num_heads", "ffn_dim",
                 "max_seq_len", "activation")


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


def _fmt_pinned(ss: Any) -> str:
    """Render the pinned scalar dims as ``depth=4; global_dim=128; ...``."""
    parts: list[str] = []
    for attr in _PINNED_ATTRS:
        val = getattr(ss, attr, None)
        if val is not None:
            parts.append(f"{attr}={val}")
    return "; ".join(parts)


def _extract_rows(ss: Any) -> tuple[list[dict[str, str]], str]:
    """Flatten a choice-only SearchSpace into table rows: one per (slot, branch).

    Returns ``(rows, "")`` on success, ``([], reason)`` when the choice container
    is missing/empty (caller records "skipped"). Row order = slot index × branch
    enumeration order (deterministic).
    """
    branches = getattr(ss, "branch_choices", None)
    if not branches:
        return [], "no branch_choices in SearchSpace (choice container missing/empty)"

    depth = getattr(ss, "depth", None)
    if not isinstance(depth, int) or depth < 1:
        return [], f"SearchSpace.depth={depth!r} is not a positive int"

    fixed = _fmt_pinned(ss)
    rows: list[dict[str, str]] = []
    for slot in range(depth):
        for branch in branches:
            rows.append({
                "slot": f"layer{slot}",
                "branch": str(branch),
                "fixed": fixed,
            })
    return rows, ""


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

    caption = (f"SearchSpace from supernet.py: {len(rows)} (slot, branch) rows; "
               f"branch choice is the only searchable dimension, all dims pinned.")

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
