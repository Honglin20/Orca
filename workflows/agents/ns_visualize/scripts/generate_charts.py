#!/usr/bin/env python3
"""generate_charts.py -- Headless static chart renderer for ns_visualize.

Renders the 6 ns_visualize charts directly to static files under
``<artifacts>/charts/`` WITHOUT an Orca run / chart socket. Use case: a NAS run
completed all 8 nodes but the ns_visualize charts were skipped because the Orca
chart socket (``$ORCA_CHART_SOCK``) was already gone by render time (headless /
post-run). This script re-renders all 6 charts from the run's own artifacts.

How it works:
  - Drives the existing 6 chart scripts (pareto / search_table / loss_curve /
    metrics_bar / compare_table / latency_dist) as subprocesses with the same
    argv shape the workflow passes.
  - Each script's ``push_chart`` detects the missing chart socket and writes a
    self-contained static file via ``_common._render_static`` (plotly HTML, or
    matplotlib PNG if plotly is unavailable).
  - Selected-arch latency/acc are discovered from ``.ns_run_search_assessment.txt``
    (``"best acc X @ Yms"``) when not passed on the CLI. NAS convention: stored
    acc is negated, so the raw value passed to scripts = ``-X``.

Usage:
  /path/to/python generate_charts.py --artifacts-dir <run artifacts abs path> \
      [--selected-latency-ms 0.384 --selected-acc -0.973]

Exit code: 0 if every script ran (returncode 0); non-zero otherwise. Missing
chart files are reported loudly at the end.

Design principles:
- pathlib only; no shell variable expansion (caller passes absolute paths).
- Reuses existing scripts (no chart-logic duplication) — DRY.
- Discovers selected-arch coords deterministically (regex on assessment file),
  not via model judgment (rule 5).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# generate_charts.py lives next to _common.py and the 6 chart scripts.
SCRIPTS_DIR = Path(__file__).resolve().parent

# init_marker imported here (not duplicated) so the CHART_MARKER filename stays
# a single source of truth in _common.py.
from _common import init_marker  # noqa: E402 -- after SCRIPTS_DIR is on path

# (script filename, extra-argv builder) tuples. Built lazily after selected-coord
# resolution so empty selected values don't leak as bogus CLI args.
#
# Ordering note: pareto.py MUST stay first — among the 6 scripts only it calls
# ``_common.init_marker`` to truncate the marker file. generate_charts.py also
# pre-truncates (defensive double-cut), but if you reorder, keep pareto first so
# a future change to init_marker ownership does not silently wipe mid-run results.
SCRIPT_ORDER: tuple[str, ...] = (
    "pareto.py",
    "search_table.py",
    "loss_curve.py",
    "metrics_bar.py",
    "compare_table.py",
    "latency_dist.py",
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Render ns_visualize charts as static files (headless fallback).",
    )
    ap.add_argument(
        "--artifacts-dir", required=True,
        help="Run artifacts dir ($ORCA_ARTIFACTS_DIR). Must be absolute.",
    )
    ap.add_argument(
        "--selected-latency-ms", default="",
        help="Selected-arch latency in ms. Auto-discovered from assessment file if empty.",
    )
    ap.add_argument(
        "--selected-acc", default="",
        help=(
            "Selected-arch metric in RAW stored polarity (NAS acc is negated, "
            "e.g. -0.973). Auto-discovered from assessment file if empty."
        ),
    )
    args = ap.parse_args()

    artifacts_dir = Path(args.artifacts_dir).resolve()
    if not artifacts_dir.is_dir():
        sys.stderr.write(f"[generate_charts] artifacts dir not found: {artifacts_dir}\n")
        return 2

    sel_lat, sel_acc_raw = _resolve_selected(
        artifacts_dir, args.selected_latency_ms, args.selected_acc,
    )
    sys.stderr.write(
        f"[generate_charts] selected: latency_ms={sel_lat or '<none>'}, "
        f"acc_raw={sel_acc_raw or '<none>'}\n"
    )

    # Truncate the marker so results reflect this run only (reuses _common.init_marker
    # rather than hardcoding the CHART_MARKER filename — single source of truth).
    init_marker(artifacts_dir)

    failures: list[tuple[str, int]] = []
    for script in SCRIPT_ORDER:
        extra = _extra_argv(script, sel_lat, sel_acc_raw)
        argv = [sys.executable, str(SCRIPTS_DIR / script),
                "--artifacts-dir", str(artifacts_dir)] + extra
        sys.stderr.write("[generate_charts] $ " + " ".join(argv) + "\n")
        proc = subprocess.run(argv, cwd=str(SCRIPTS_DIR))
        if proc.returncode != 0:
            failures.append((script, proc.returncode))

    _report_outputs(artifacts_dir)
    if failures:
        sys.stderr.write("\n[generate_charts] FAILED scripts (non-zero rc):\n")
        for name, rc in failures:
            sys.stderr.write(f"  {name}: rc={rc}\n")
        return 1
    return 0


def _resolve_selected(
    artifacts_dir: Path, lat_arg: str, acc_arg: str,
) -> tuple[str, str]:
    """Resolve selected latency (ms) + raw acc from CLI args or assessment file.

    Assessment file format (deterministic): ``"...best acc 0.973 @ 0.384ms..."``.
    NAS convention: acc stored negated -> raw acc passed to scripts = -display.
    Returns ("", "") if neither CLI nor assessment provides values (scripts will
    record those charts as skipped-missing gracefully).
    """
    sel_lat = lat_arg.strip()
    sel_acc_raw = acc_arg.strip()
    if sel_lat and sel_acc_raw:
        return sel_lat, sel_acc_raw

    assessment = artifacts_dir / ".ns_run_search_assessment.txt"
    if assessment.is_file():
        text = assessment.read_text(encoding="utf-8", errors="replace")
        m = re.search(
            r"best\s+acc\s+(\d+(?:\.\d+)?)\s*@\s*(\d+(?:\.\d+)?)\s*ms", text, re.IGNORECASE,
        )
        if m:
            acc_display = float(m.group(1))
            lat_ms = float(m.group(2))
            if not sel_lat:
                sel_lat = f"{lat_ms:.4f}"
            if not sel_acc_raw:
                sel_acc_raw = f"{-acc_display:.4f}"
    return sel_lat, sel_acc_raw


def _extra_argv(script: str, sel_lat: str, sel_acc_raw: str) -> list[str]:
    """Build the per-script extra argv (selected-* args only when non-empty)."""
    if script in ("pareto.py", "compare_table.py"):
        extra: list[str] = []
        if sel_lat:
            extra += ["--selected-latency-ms", sel_lat]
        if sel_acc_raw:
            extra += ["--selected-acc", sel_acc_raw]
        return extra
    if script == "metrics_bar.py":
        return ["--selected-acc", sel_acc_raw] if sel_acc_raw else []
    return []


def _report_outputs(artifacts_dir: Path) -> None:
    """Print a loud summary of generated static chart files (verification aid)."""
    charts_dir = artifacts_dir / "charts"
    sys.stderr.write("\n[generate_charts] === static chart outputs ===\n")
    if not charts_dir.is_dir():
        sys.stderr.write(f"[generate_charts] ERROR: charts dir missing: {charts_dir}\n")
        return
    generated = sorted(charts_dir.iterdir())
    if not generated:
        sys.stderr.write(f"[generate_charts] WARNING: no files generated under {charts_dir}\n")
        return
    for f in generated:
        size = f.stat().st_size if f.is_file() else 0
        sys.stderr.write(f"  {f.name:40s} {size:>10d} bytes\n")

    # Derive expected filenames from SCRIPT_ORDER (single source of truth) so
    # adding a 7th chart only requires editing SCRIPT_ORDER.
    expected = [Path(s).stem + ".html" for s in SCRIPT_ORDER]
    names = {f.name for f in generated}
    missing = [n for n in expected if n not in names]
    if missing:
        sys.stderr.write(
            "[generate_charts] MISSING (plotly expected): " + ", ".join(missing) + "\n"
        )
    else:
        sys.stderr.write(
            f"[generate_charts] all {len(expected)} expected plotly HTML files present.\n"
        )


if __name__ == "__main__":
    sys.exit(main())
