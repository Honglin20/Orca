#!/usr/bin/env python3
"""emit_result.py — ns3_run_search final JSON (the only producer of the node output).

The deterministic parts (status / artifacts / max_retries_hit) are judged from the real
filesystem; the behavior-trace parts (healed_files / fidelity_retriggered / assessment)
are read from the marker files. Prints a single line of JSON to stdout.
"""
import argparse
import glob
import json
import os

parser = argparse.ArgumentParser()
parser.add_argument("--latency-unit", default="ms")
args = parser.parse_args()

ad = os.environ["ORCA_ARTIFACTS_DIR"]


def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except FileNotFoundError:
        return default


def read_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [ln.strip() for ln in f if ln.strip()]
    except FileNotFoundError:
        return []


def tail(path, n=20):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return "\n".join(lines[-n:])
    except FileNotFoundError:
        return ""


results_path = os.path.join(ad, "search_results.jsonl")
recs = 0
try:
    with open(results_path, "r", encoding="utf-8", errors="replace") as fh:
        for _ in fh:
            recs += 1
except FileNotFoundError:
    pass

script_path = os.path.join(ad, "run_search_supernet.sh")
script_exists = os.path.exists(script_path)

if script_exists and recs >= 1:
    status, artifacts, max_retries_hit = "executed", [results_path], False
else:
    status, artifacts, max_retries_hit = "failed", [], True
    # take the latest attempt log (with unlimited retries the last N≠3, so no hardcoded attempt3)
    logs = sorted(glob.glob(os.path.join(ad, "runs", "search", "search.attempt*.stdout.log")))
    log_tail = tail(logs[-1]) if logs else ""
    if log_tail:
        prev = read_text(os.path.join(ad, ".ns_run_search_assessment.txt"), "")
        with open(os.path.join(ad, ".ns_run_search_assessment.txt"), "w", encoding="utf-8") as fh:
            fh.write((prev + "\n" if prev else "") + "last_error:\n" + log_tail)

healed_files = read_lines(os.path.join(ad, ".ns_run_search_healed.txt"))
fidelity_retriggered = read_text(os.path.join(ad, ".ns_run_search_fidelity.flag"), "false") == "true"
assessment = read_text(os.path.join(ad, ".ns_run_search_assessment.txt"),
                       "no assessment recorded" if status == "executed" else "")

# ── select 5 fields + latency_unit (read from the .selected_arch.json marker; failure safety net: always valid JSON) ──
select_defaults = {
    "selected_arch": None,
    "selected_acc": 0,
    "selected_latency": 0,
    "latency_unit": args.latency_unit,
    "pareto_size": 0,
    "select_reason": "none",
}
selected_path = os.path.join(ad, ".selected_arch.json")
try:
    with open(selected_path, "r", encoding="utf-8") as f:
        select_data = json.loads(f.read().strip())
    if isinstance(select_data, dict):
        for k, v in select_data.items():
            if k in select_defaults:
                select_defaults[k] = v
        # read-side dual recognition: new .selected_arch.json uses selected_latency; old runs may still write selected_latency_ms.
        if "selected_latency" not in select_data and "selected_latency_ms" in select_data:
            select_defaults["selected_latency"] = select_data["selected_latency_ms"]
except (FileNotFoundError, json.JSONDecodeError, ValueError):
    pass  # select not run / marker missing → falsy defaults (node_failed forbidden)

print(json.dumps({
    "status": status,
    "artifacts": artifacts,
    "assessment": assessment,
    "max_retries_hit": max_retries_hit,
    "healed_files": healed_files,
    "fidelity_retriggered": fidelity_retriggered,
    "selected_arch": select_defaults["selected_arch"],
    "selected_acc": select_defaults["selected_acc"],
    "selected_latency": select_defaults["selected_latency"],
    "latency_unit": select_defaults["latency_unit"],
    "pareto_size": select_defaults["pareto_size"],
    "select_reason": select_defaults["select_reason"],
}))
