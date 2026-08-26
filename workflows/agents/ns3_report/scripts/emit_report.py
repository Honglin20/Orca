#!/usr/bin/env python3
"""emit_report.py — ns3_report terminal-state determination (the only producer of the node output).

Reads the on-disk status files under $ORCA_ARTIFACTS_DIR, determines the terminal state
(first match wins), writes .report.json for check_report.sh, and prints a single line of JSON.
"""
import glob
import json
import os

ad = os.environ["ORCA_ARTIFACTS_DIR"]


def exists(path):
    return os.path.isfile(path) and os.path.getsize(path) > 0


def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except FileNotFoundError:
        return default


# ── helper: find flat/optimized ──
def find_prepared_model():
    for pattern in ("*_llm-optimized.py", "*_flat.py"):
        files = glob.glob(os.path.join(ad, pattern))
        if files:
            return os.path.basename(files[0])
    return ""


# ── read rc files (scripts write to runs/train/ and runs/retrain/ subdirs) ──
train_rc = read_text(os.path.join(ad, "runs", "train", ".train_rc"), None)
retrain_rc = read_text(os.path.join(ad, "runs", "retrain", ".retrain_rc"), None)

# ── read select data ──
selected_arch = None
selected_acc = 0
selected_latency = 0
latency_unit = "ms"
pareto_size = 0
select_reason = "none"
try:
    with open(os.path.join(ad, ".selected_arch.json"), "r") as f:
        sdata = json.loads(f.read().strip())
    if isinstance(sdata, dict):
        selected_arch = sdata.get("selected_arch")
        selected_acc = sdata.get("selected_acc", 0)
        # read both key forms: new runs write selected_latency; old runs may write selected_latency_ms.
        selected_latency = sdata.get("selected_latency", sdata.get("selected_latency_ms", 0))
        latency_unit = sdata.get("latency_unit", "ms")
        pareto_size = sdata.get("pareto_size", 0)
        select_reason = sdata.get("select_reason", "none")
except (FileNotFoundError, json.JSONDecodeError, ValueError):
    pass

# ── read subnet structure path (produced by ns3_retrain via subnet_profile.py) ──
subnet_structure_md = os.path.join(ad, "subnet_structure.md")
subnet_structure = "subnet_structure.md" if os.path.isfile(subnet_structure_md) else ""

# ── read supernet info ──
supernet_path = os.path.join(ad, "supernet.py") if exists(os.path.join(ad, "supernet.py")) else ""

# ── determine terminal state (first match wins) ──
has_manifest = exists(os.path.join(ad, "project_manifest.md"))
has_flat = bool(find_prepared_model())
has_supernet = exists(os.path.join(ad, "supernet.py"))
has_summary = exists(os.path.join(ad, "supernet_summary.md"))
has_search_results = exists(os.path.join(ad, "search_results.jsonl"))
has_select_attempt = os.path.isfile(os.path.join(ad, ".select_attempt"))

# read unsupported marker (structured signal from ns3_expand; content check 'true', not isfile,
# DRY with the fidelity flag convention). Best-effort acceleration signal: missing/non-'true'
# falls back to the summary substring grep below (ground truth for in-flight runs predating the marker).
unsupported_marker = read_text(os.path.join(ad, ".ns_expand_unsupported.flag"), "") == "true"

# summary substring fallback (LLM-generated free text; the marker is the preferred signal because
# the LLM may forget the literal "No supported match" phrase or phrase it differently).
summary_has_unsupported = False
if has_summary:
    summary = read_text(os.path.join(ad, "supernet_summary.md"), "")
    for line in summary.split("\n"):
        if "No supported match" in line:
            summary_has_unsupported = True
            break

# double signal: marker (structured) OR summary substring (free-text fallback).
unsupported = unsupported_marker or summary_has_unsupported

status = "failed"
stage = "report"
reason = "unknown terminal state"

# 1. flatten_failed
if not has_supernet and (not has_flat or not has_manifest):
    status, stage = "failed", "flatten"
    reason = "flatten failed: flat/optimized or manifest missing and supernet.py absent"

# 2. unsupported (double signal: marker from expand + summary substring fallback for in-flight runs)
elif unsupported:
    status, stage = "failed", "expand"
    reason = "model type not supported for NAS"

# 3. retrain_failed (retrain_status.md exists + .retrain_rc != 0)
elif retrain_rc is not None and retrain_rc != "0" and exists(os.path.join(ad, "retrain_status.md")):
    status, stage = "failed", "retrain"
    reason = f"retrain failed: .retrain_rc={retrain_rc}"

# 4. select_failed
elif has_search_results and has_select_attempt and not selected_arch:
    status, stage = "failed", "run_search"
    reason = "select failed: no candidate selected (selected_arch is null)"

# 5. train_failed (train_status.md exists + .train_rc != 0 + no search_results)
elif train_rc is not None and train_rc != "0" and not has_search_results and exists(os.path.join(ad, "train_status.md")):
    status, stage = "failed", "run_train"
    reason = f"train failed: .train_rc={train_rc}"

# 6. success
elif retrain_rc == "0":
    # verify final ckpt exists
    retrain_ckpts = glob.glob(os.path.join(ad, "runs", "retrain", "*.pth"))
    if retrain_ckpts:
        status, stage = "success", "retrain"
        reason = "full pipeline completed: flatten → expand → train → search → select → retrain"
    else:
        status, stage = "failed", "retrain"
        reason = "retrain_rc=0 but no final checkpoint found"

# ── read final metrics from retrain status ──
final_metrics = ""
retrain_status_path = os.path.join(ad, "retrain_status.md")
if exists(retrain_status_path):
    final_metrics = read_text(retrain_status_path, "")

# ── read assessment from train/search if failed there ──
if stage == "run_train":
    final_metrics = read_text(os.path.join(ad, ".ns_run_train_assessment.txt"), final_metrics)
elif stage == "run_search":
    final_metrics = read_text(os.path.join(ad, ".ns_run_search_assessment.txt"), final_metrics)

# ── charts_summary (best-effort: list chart output files) ──
# Chart scripts render static files to <ad>/charts/ (via _common.push_chart static fallback) and
# append every result (pushed/rendered_static/skipped) to .nas-supernet_charts.jsonl. The old scan
# of runs/{train,search,retrain} missed charts/ entirely AND falsely caught metrics files (e.g.
# runs/retrain/test_metrics.json). Scan charts/ for rendered files; if none (live-pushed run with no
# static fallback, or nothing rendered), fall back to the marker so live charts are still listed.
chart_files = []
charts_dir = os.path.join(ad, "charts")
if os.path.isdir(charts_dir):
    for f in sorted(os.listdir(charts_dir)):
        if f.endswith(".png") or f.endswith(".html"):
            chart_files.append(os.path.join("charts", f))
if chart_files:
    charts_summary = ", ".join(chart_files)
else:
    titles = []
    seen = set()
    marker = os.path.join(ad, ".nas-supernet_charts.jsonl")
    if os.path.isfile(marker):
        with open(marker, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("status") in ("pushed", "rendered_static"):
                    t = rec.get("title", "")
                    if t and t not in seen:
                        seen.add(t)
                        titles.append(t)
    charts_summary = ("live charts: " + ", ".join(titles)) if titles else "no chart files found"

# ── artifacts list ──
artifacts = []
if supernet_path:
    artifacts.append("supernet.py")
if has_search_results:
    artifacts.append("search_results.jsonl")
if retrain_rc == "0":
    for ckpt in glob.glob(os.path.join(ad, "runs", "retrain", "*.pth")):
        artifacts.append(os.path.relpath(ckpt, ad))
if has_summary:
    artifacts.append("supernet_summary.md")
if has_manifest:
    artifacts.append("project_manifest.md")

report = {
    "status": status,
    "stage": stage,
    "reason": reason,
    "selected_arch": selected_arch,
    "selected_acc": selected_acc,
    "selected_latency": selected_latency,
    "latency_unit": latency_unit,
    "subnet_structure": subnet_structure,
    "pareto_size": pareto_size,
    "supernet_path": supernet_path,
    "output_dir": ad,
    "final_metrics": final_metrics[:500] if final_metrics else "",
    "artifacts": artifacts,
    "charts_summary": charts_summary,
    "error": "" if status == "success" else reason,
}

# write to disk for check_report.sh
with open(os.path.join(ad, ".report.json"), "w") as f:
    json.dump(report, f)

print(json.dumps(report))
