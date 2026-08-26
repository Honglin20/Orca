#!/usr/bin/env python3
"""emit_report.py — pz_report terminal-state determination (the only producer of the node output).

Reads the on-disk status files under $ORCA_ARTIFACTS_DIR, determines the terminal state
(first match wins), writes .report.json for check_report.sh, and prints a single line of JSON.
"""
import glob
import json
import os

ad = os.environ["ORCA_ARTIFACTS_DIR"]


def exists(path):
    return os.path.isfile(path) and os.path.getsize(path) > 0


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except FileNotFoundError:
        return default


# ── helper: find flat / optimized_flat (optimized must not be mistaken for flat) ──
def find_optimized_flat():
    files = glob.glob(os.path.join(ad, "*_optimized_flat.py"))
    return os.path.basename(files[0]) if files else ""


def find_flat():
    files = [f for f in glob.glob(os.path.join(ad, "*_flat.py")) if "optimized" not in os.path.basename(f)]
    return os.path.basename(files[0]) if files else ""


# ── read state files ──
gate_result = read_json(os.path.join(ad, "gate_result.json"))
baseline = read_json(os.path.join(ad, "baseline_metrics.json"))
selected = read_json(os.path.join(ad, "selected_arch.json"))
flat = find_flat()
optimized_flat = find_optimized_flat()

final_model = os.path.join(ad, "runs", "retrain", "final_model.pt")
retrain_rc = read_text(os.path.join(ad, "runs", "retrain", ".retrain_rc"), None)

has_manifest = exists(os.path.join(ad, "manifest.yaml"))
has_search_space = exists(os.path.join(ad, "search_space.yaml"))
has_block_map = exists(os.path.join(ad, "block_map.json"))
has_block_library = os.path.isdir(os.path.join(ad, "block_library"))
has_scores = exists(os.path.join(ad, "scores.jsonl"))
has_gate_result = gate_result is not None

# search_space empty-slots detection (E22 unsupported): search_space.yaml declares 0 slots
search_space_slot_count = None
if has_search_space:
    ss_text = read_text(os.path.join(ad, "search_space.yaml"), "")
    search_space_slot_count = ss_text.count("kind: transformer_layer") + ss_text.count("kind: attention") + ss_text.count("kind: ffn")

# ── terminal state (first match wins) ──
status, stage, reason = "failed", "report", "unknown terminal state"

selected_arch = (selected or {}).get("selected_arch") if isinstance(selected, dict) else (selected if selected else None)

if has_gate_result and exists(final_model):
    gs = gate_result.get("gate_status")
    if gs == "pass":
        status, stage, reason = "success", "report", "AC gate both-met (metric + latency)"
    else:
        status, stage, reason = "failed", "report", "AC gate not met"
elif has_gate_result and not exists(final_model):
    # stale gate_result from a prior run while this run has no final ckpt → gate crash / inconsistent
    status, stage, reason = (
        "failed", "report",
        "gate_result.json 存在但 final_model.pt 缺失（跨 run 残留或 gate 前崩溃）",
    )
elif exists(final_model):
    # final ckpt exists but gate_report.py did not produce gate_result.json → gate crash, fail loud
    status, stage, reason = (
        "failed", "report",
        "final_model.pt 存在但 gate_result.json 缺失——gate_report.py 未产出（可能崩溃），回看日志",
    )
elif optimized_flat:
    # optimized_flat exists but final_model.pt missing → GKD did not finish
    if retrain_rc is not None and retrain_rc != "0":
        status, stage, reason = "failed", "retrain", f"GKD retrain 失败（.retrain_rc={retrain_rc}）"
    else:
        status, stage, reason = "failed", "retrain", "GKD 未产出 final_model.pt"
elif selected_arch:
    # selected_arch non-empty but optimized_flat missing → materialize failed
    status, stage, reason = "failed", "materialize", "optimized_flat 装配失败（materialize_optimized.py 自检不过）"
elif has_scores:
    # scores.jsonl exists but selected_arch missing/empty → select failed
    status, stage, reason = "failed", "select", "MIP 未选出架构（selected_arch 空/缺失）"
elif has_block_library:
    status, stage, reason = "failed", "score", "打分未产出 scores.jsonl"
elif baseline is not None:
    if baseline.get("latency_target_feasible") is False:
        status, stage, reason = (
            "failed", "baseline",
            "latency 目标结构性不可达：" + str(baseline.get("latency_infeasible_reason", "")),
        )
    elif has_block_map:
        status, stage, reason = "failed", "build_library", "BLD 未产出 block_library"
    else:
        status, stage, reason = "failed", "baseline", "measure_baseline smoke 未全过"
elif has_block_map:
    status, stage, reason = "failed", "baseline", "measure_baseline 未产出 baseline_metrics.json"
elif has_search_space and search_space_slot_count in (None, 0):
    status, stage, reason = "failed", "search_space", "无可用 transformer_layer slot（unsupported）"
elif has_search_space:
    status, stage, reason = "failed", "baseline", "measure_baseline 未产出 block_map.json"
elif has_manifest or flat:
    status, stage, reason = "failed", "search_space", "search_space.yaml 缺失（unsupported）"
else:
    status, stage, reason = "failed", "ingest", "manifest/flat 缺失（ingest 未产出）"

# ── structured report ──
if has_gate_result:
    gate_status = gate_result.get("gate_status", "none")
    gate_reason = gate_result.get("gate_reason", "none")
    final_metric = gate_result.get("final_metric", 0)
    final_latency = gate_result.get("final_latency", 0)
    baseline_metric = gate_result.get("baseline_metric", (baseline or {}).get("baseline_acc", 0))
    baseline_latency = gate_result.get("baseline_latency", (baseline or {}).get("baseline_latency", 0))
    metric_delta = gate_result.get("metric_delta", 0)
    latency_ratio = gate_result.get("latency_ratio", 0)
    latency_unit = gate_result.get("latency_unit", (baseline or {}).get("latency_unit", "ms"))
    report_path = gate_result.get("report_path", "")
    metric_direction = gate_result.get("metric_direction", "")
    metric_threshold = gate_result.get("metric_threshold", 0)
    metric_tolerance_kind = gate_result.get("metric_tolerance_kind", "")
    metric_pass_formula = gate_result.get("metric_pass_formula", "")
    latency_ratio_threshold = gate_result.get("latency_ratio_threshold", 0)
    latency_reduction_target = gate_result.get("latency_reduction_target", 0.5)
else:
    gate_status = "none"
    gate_reason = "none"
    final_metric = 0
    final_latency = 0
    baseline_metric = (baseline or {}).get("baseline_acc", 0)
    baseline_latency = (baseline or {}).get("baseline_latency", 0)
    metric_delta = 0
    latency_ratio = 0
    latency_unit = (baseline or {}).get("latency_unit", "ms")
    report_path = ""
    metric_direction = (baseline or {}).get("metric_direction", "none")
    metric_threshold = 0
    metric_tolerance_kind = ""
    metric_pass_formula = ""
    latency_ratio_threshold = 0
    latency_reduction_target = 0.5

artifacts = []
for name in (
    "manifest.yaml", "search_space.yaml", "block_map.json", "baseline_metrics.json",
    "bld_summary.json", "scores.jsonl", "latency_table.jsonl", "selected_arch.json",
    "gate_result.json", "final_report.md",
):
    if exists(os.path.join(ad, name)):
        artifacts.append(name)
if optimized_flat:
    artifacts.append(optimized_flat)
if has_block_library:
    artifacts.append("block_library/")
if exists(final_model):
    artifacts.append("runs/retrain/final_model.pt")

report = {
    "status": status,
    "stage": stage,
    "reason": reason,
    "gate_status": gate_status,
    "final_metric": final_metric,
    "final_latency": final_latency,
    "baseline_metric": baseline_metric,
    "baseline_latency": baseline_latency,
    "metric_delta": metric_delta,
    "latency_ratio": latency_ratio,
    "latency_unit": latency_unit,
    "gate_reason": gate_reason,
    "report_path": report_path,
    "metric_direction": metric_direction,
    "metric_threshold": metric_threshold,
    "metric_tolerance_kind": metric_tolerance_kind,
    "metric_pass_formula": metric_pass_formula,
    "latency_ratio_threshold": latency_ratio_threshold,
    "latency_reduction_target": latency_reduction_target,
    "selected_arch": selected_arch,
    "optimized_flat_path": os.path.join(ad, optimized_flat) if optimized_flat else "",
    "output_dir": ad,
    "block_map": os.path.join(ad, "block_map.json") if has_block_map else "",
    "error": "" if status == "success" else reason,
    "artifacts": artifacts,
}

# write to disk for check_report.sh
with open(os.path.join(ad, ".report.json"), "w") as f:
    json.dump(report, f)

# unified terminal marker final_status.json（U6 root cause J 契约，同 schema 给 terminate 路径用）。
# 成功路径 gate_report.py 已写（stage=pz_report, status=pass/fail, 含 gate metrics）——不覆盖；
# 失败路径（未到 gate）由本 reporter 补写，供 web / 下游消费统一终态。
final_status_path = os.path.join(ad, "final_status.json")
if not os.path.isfile(final_status_path):
    with open(final_status_path, "w", encoding="utf-8") as f:
        json.dump({
            "stage": "pz_report",
            "status": "pass" if status == "success" else "fail",
            "reason": reason,
            "metrics": {
                "baseline_metric": baseline_metric,
                "final_metric": final_metric,
                "baseline_latency": baseline_latency,
                "final_latency": final_latency,
                "latency_ratio": latency_ratio,
                "metric_direction": metric_direction,
            },
        }, f, ensure_ascii=False, indent=2)

print(json.dumps(report))
