"""finalize_kd.py —— KD-NAS finalize 节点的确定性后端（SPEC §6.10）。

为什么独立脚本：yaml inline prompt 里嵌 multi-line python（heredoc）会破坏 YAML literal
block 的缩进规则；放脚本里既保持 yaml 干净，又让 finalize 的复杂逻辑可单测、可复用。

职责（SPEC §6.10 N10/N19）：
  1. 从 ledger 查 champion 详情（variant_id / student_path / accepted_cfg / ckpt）。
  2. champion=baseline → 跳 student eval/ONNX/latency，用 setup 透传 baseline 值（MAJOR-1 兜底）。
  3. champion=真 student → eval（复用 champion ckpt，N10 不重训）+ ONNX 导出（N19）+ latency 真测。
  4. 写 final_report.md（baseline/teacher/students 对比 + 选择依据 + 帕累托 + 探索轮数）。
  5. emit stdout KEY: value（yaml agent dumb copy 进 output_schema 字段）。

yaml agent 仍负责 viz_kd_stage --stage final 调用 + viz_status 合并（保持 sidecar 边界）。

CLI：
    finalize_kd.py \\
      --ledger <path> --champions <path> --champion_id <id> --terminate_reason <str> \\
      --baseline_contract_path <path> --train_pipeline_path <path> \\
      --baseline_latency_us <f> --baseline_accuracy <f> --teacher_latency_us <f> \\
      --target_latency_us <f> --accuracy_baseline <f> --accuracy_baseline_kind <kind> \\
      --kd_artifacts_dir <dir> --struct_scripts_dir <dir> --kd_scripts_dir <dir> \\
      --device <auto|cuda|cpu|npu> --seed <int> --latency_provider <path::func> \\
      --project_root <path> --per_run_artifacts_dir <dir>

stdout KEY::
    CHAMPION_IS_BASELINE: <1|0>
    CHAMPION_STUDENT: <path>
    CHAMPION_CKPT: <path|空串>
    FINAL_LATENCY_US: <f>
    FINAL_ACCURACY: <f>
    FINAL_ONNX: <path|空串>
    FINAL_REPORT: <path>

fail loud：eval / export_onnx / latency_provider 子进程 rc≠0 → exit 2 + stderr。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any


def _lookup_champion(ledger_path: str, champion_id: str, baseline_contract_path: str) -> dict[str, Any]:
    """从 ledger 找 champion row；champion_id=baseline → 构造虚拟 row。"""
    if champion_id == "baseline":
        return {
            "variant_id": "baseline",
            "student_path": baseline_contract_path,
            "accepted_cfg": "{}",
            "ckpt": "",
            "round": 0,
        }
    rows: list[dict[str, Any]] = []
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    for r in rows:
        if r.get("variant_id") == champion_id:
            cfg = r.get("accepted_cfg", {})
            if not isinstance(cfg, str):
                cfg = json.dumps(cfg)
            return {
                "variant_id": r.get("variant_id", champion_id),
                "student_path": r.get("student_path", ""),
                "accepted_cfg": cfg,
                "ckpt": r.get("ckpt", ""),
                "round": r.get("round", 0),
            }
    raise ValueError(
        f"champion_id={champion_id!r} 在 ledger 中找不到（ledger 行数={len(rows)}）"
    )


def _read_baseline_dummy(baseline_contract_path: str) -> str:
    """读 baseline DUMMY_INPUT（export_onnx 用；shape 跟 baseline，不写死）。"""
    p = baseline_contract_path
    d = os.path.dirname(p)
    if d not in sys.path:
        sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location("_fin_b", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return json.dumps(m.DUMMY_INPUT)


def _run_eval(
    train_pipeline_path: str,
    champion: dict[str, Any],
    accuracy_baseline: str,
    accuracy_baseline_kind: str,
    device: str,
    seed: str,
    project_root: str,
    per_run_artifacts_dir: str,
) -> float:
    """跑 train_pipeline --mode eval（复用 champion ckpt，N10 不重训）；返回 accuracy。"""
    if not champion["ckpt"]:
        raise ValueError("champion ckpt 为空（无法 eval；champion 应是 SUCCESS&met_* student）")
    out = subprocess.run(
        [
            sys.executable, train_pipeline_path,
            "--mode", "eval",
            "--student_model_path", champion["student_path"],
            "--build_fn", "build_model", "--build_cfg", champion["accepted_cfg"],
            "--student_ckpt", champion["ckpt"], "--out_ckpt", champion["ckpt"],
            "--accuracy_baseline", str(accuracy_baseline),
            "--accuracy_baseline_kind", str(accuracy_baseline_kind),
            "--device", str(device), "--seed", str(seed),
            "--project_root", str(project_root),
            "--env_anchor", str(per_run_artifacts_dir),
        ],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(
            f"champion eval rc={out.returncode}（champion ckpt 损坏？eval 路径异常？）\n"
            f"stdout: {out.stdout[-800:]}\nstderr: {out.stderr[-800:]}"
        )
    for line in out.stdout.splitlines():
        if line.startswith("STUDENT_ACCURACY:"):
            return float(line.split(":", 1)[1].strip())
    raise RuntimeError(f"--mode eval 未 emit STUDENT_ACCURACY：\n{out.stdout[-800:]}")


def _export_onnx(
    struct_scripts_dir: str,
    champion: dict[str, Any],
    dummy_input: str,
    out_onnx: str,
    device: str,
    seed: str,
) -> None:
    """确定性 ONNX 导出。"""
    out = subprocess.run(
        [
            sys.executable, f"{struct_scripts_dir}/export_onnx.py",
            "--model_path", champion["student_path"],
            "--build_fn", "build_model",
            "--dummy_input", dummy_input,
            "--opset", "17",
            "--out", out_onnx,
            "--device", str(device),
            "--seed", str(seed),
        ],
        capture_output=True, text=True,
    )
    if out.returncode != 0 or not os.path.isfile(out_onnx):
        raise RuntimeError(
            f"export_onnx rc={out.returncode}\nstderr: {out.stderr[-800:]}"
        )


def _measure_latency(latency_provider: str, onnx_path: str, device: str) -> float:
    """动态加载 latency_provider::func，measure(onnx[, device])。fail loud。"""
    if "::" not in latency_provider:
        raise ValueError(f"latency_provider 非 path::func：{latency_provider!r}")
    path, func = latency_provider.split("::", 1)
    spec = importlib.util.spec_from_file_location("_fin_lp", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    measure = getattr(mod, func)
    sig = inspect.signature(measure)
    if "device" in sig.parameters:
        return float(measure(onnx_path, device=device))
    return float(measure(onnx_path))


def _write_report(
    report_path: str,
    ledger_path: str,
    champion_id: str,
    champion: dict[str, Any],
    is_baseline: bool,
    terminate_reason: str,
    final_latency_us: float,
    final_accuracy: float,
    baseline_latency_us: float,
    baseline_accuracy: float,
    teacher_latency_us: float,
    target_latency_us: float,
    accuracy_baseline: float,
    accuracy_baseline_kind: str,
    teacher_obj: dict[str, Any] | None = None,
    champion_ids: set[str] | None = None,
) -> None:
    rows: list[dict[str, Any]] = []
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    student_rows = [r for r in rows if r.get("round", 0) and int(r.get("round", 0)) > 0]
    ok = [r for r in student_rows if r.get("status") == "SUCCESS"]
    fail_latency = [r for r in student_rows if r.get("status") == "FAIL_latency"]
    fail_train = [r for r in student_rows if r.get("status") == "FAIL_train"]
    fail_build = [r for r in student_rows if r.get("status") == "FAIL_build"]
    admitted = [
        r for r in student_rows
        if r.get("status") == "SUCCESS"
        and r.get("met_latency") is True
        and r.get("met_accuracy") is True
    ]

    lines: list[str] = []
    lines.append("# KD-NAS Final Report")
    lines.append("")
    lines.append(f"- **champion**: `{champion_id}`")
    lines.append(f"- **terminate_reason**: {terminate_reason or '(无)'}")
    lines.append(f"- **explored rounds**: {len(student_rows)}")
    lines.append(f"- **final_latency_us**: {final_latency_us:.6g}")
    lines.append(f"- **final_accuracy**: {final_accuracy:.6g}")
    lines.append(f"- **baseline_latency_us**: {baseline_latency_us:.6g}")
    lines.append(f"- **baseline_accuracy**: {baseline_accuracy:.6g}")
    lines.append(f"- **teacher_latency_us**: {teacher_latency_us:.6g}")
    lines.append(f"- **target_latency_us**: {target_latency_us:.6g}")
    lines.append(f"- **accuracy_baseline** ({accuracy_baseline_kind}): {accuracy_baseline:.6g}")
    lines.append("")

    # ── All Architectures 总表（baseline + teacher + students + champions）──
    # 一张表看全所有架构的 latency+accuracy；accuracy 来自 evaluate（非 training loss）。
    champ_ids = champion_ids or set()
    lines.append("## All Architectures (baseline + teacher + students + champions)")
    lines.append("")
    lines.append("| id | role | round | latency_us | accuracy | met_lat | met_acc | status |")
    lines.append("|---|---|---|---|---|---|---|---|")
    # baseline
    # b_met 统一 "true"/"false" 字面量（与 student 行 str(bool(...)).lower() 一致；
    # 旧 "" 空串是第三类目，前端 hue 渲染会漂移）。
    b_met = (
        "true" if baseline_latency_us is not None
        and target_latency_us is not None
        and baseline_latency_us <= target_latency_us else "false"
    )
    lines.append(
        f"| baseline | baseline | 0 | {baseline_latency_us:.6g} | "
        f"{baseline_accuracy:.6g} | {b_met} | — | baseline |"
    )
    # teacher
    if teacher_obj:
        t_lat = teacher_obj.get("teacher_latency_us")
        t_acc = teacher_obj.get("teacher_accuracy")
        t_known = bool(teacher_obj.get("teacher_accuracy_known", False))
        lines.append(
            f"| teacher | teacher | — | {t_lat if t_lat is not None else ''} | "
            f"{t_acc if t_acc is not None else ''} | — | — | "
            f"{'teacher' if t_known else 'teacher(unknown acc)'} |"
        )
    # students / champions
    # latency 双 fallback（latency_us_median → latency_us）与 all_models_table / pareto 一致；
    # met_lat / met_acc 统一 str(bool(...)).lower()（旧 Python 原生 True/False 与 baseline true/false 字面不一致）。
    for r in student_rows:
        vid = str(r.get("variant_id", "?"))
        lat = r.get("latency_us_median", r.get("latency_us", ""))
        role = "champion" if vid in champ_ids else "student"
        met_lat = str(bool(r.get("met_latency"))).lower()
        met_acc = str(bool(r.get("met_accuracy"))).lower()
        lines.append(
            f"| {vid} | {role} | {r.get('round', '')} | {lat} | "
            f"{r.get('accuracy', '')} | {met_lat} | "
            f"{met_acc} | {r.get('status', '')} |"
        )
    lines.append("")

    # ── Search Outcome 纯文本计数（SPEC §3.3：图表是唯一真相源，这里不算非支配集）──
    fail_accuracy = [r for r in student_rows if r.get("status") == "FAIL_accuracy"]
    fail_export = [r for r in student_rows if r.get("status") == "FAIL_export"]
    known_statuses = {
        "SUCCESS", "FAIL_latency", "FAIL_train", "FAIL_build",
        "FAIL_accuracy", "FAIL_export",
    }
    n_other = sum(1 for r in student_rows if r.get("status") not in known_statuses)
    lines.append("## Search Outcome")
    lines.append(
        f"- SUCCESS: {len(ok)} / FAIL_latency: {len(fail_latency)} / "
        f"FAIL_train: {len(fail_train)} / FAIL_build: {len(fail_build)} / "
        f"FAIL_accuracy: {len(fail_accuracy)} / FAIL_export: {len(fail_export)} / "
        f"其它: {n_other}"
    )
    lines.append("")

    lines.append("## Champion 选择依据")
    if is_baseline:
        lines.append(
            "**无 student 达标**——所有轮 FAIL_latency / FAIL_train / FAIL_build，admitted 集合为空，"
            "champion 维持 baseline (round=0)。"
        )
        lines.append("")
        lines.append(f"- FAIL_latency: {len(fail_latency)} 轮（latency 未达 target）")
        lines.append(f"- FAIL_train: {len(fail_train)} 轮（训练/eval rc≠0）")
        lines.append(f"- FAIL_build: {len(fail_build)} 轮（validate_contract 3 strikes）")
    else:
        lines.append(
            f"champion = min-latency ratchet（admitted 集合 {len(admitted)} 个 SUCCESS ∧ met_latency ∧ "
            f"met_accuracy 中 latency 最小，FIFO tiebreak N12）。"
        )
    lines.append("")
    lines.append("## 各轮 student 汇总")
    lines.append("| round | variant_id | latency_us | accuracy | met_lat | met_acc | status | direction_id |")
    lines.append("|---|---|---|---|---|---|---|---|")
    # latency 双 fallback（latency_us_median → latency_us）对齐 "All Architectures" 段；
    # bool 渲染统一 str(bool(...)).lower()（与 baseline 行 true/false 一致）。
    for r in student_rows:
        lat = r.get("latency_us_median", r.get("latency_us", ""))
        met_lat = str(bool(r.get("met_latency"))).lower()
        met_acc = str(bool(r.get("met_accuracy"))).lower()
        lines.append(
            f"| {r.get('round', '')} | {r.get('variant_id', '')} | {lat} | "
            f"{r.get('accuracy', '')} | {met_lat} | {met_acc} | "
            f"{r.get('status', '')} | {r.get('direction_id', '')} |"
        )
    lines.append("")
    lines.append("## viz_status")
    lines.append("终态对比图（baseline / teacher / champion latency）由 viz_kd_stage --stage final 推送；"
                 "见 finalize.output.viz_status 字段（dumb copy 自 sidecar stdout）。")

    Path(report_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _main() -> int:
    p = argparse.ArgumentParser(description="KD-NAS finalize 确定性后端（SPEC §6.10）")
    p.add_argument("--ledger", required=True)
    p.add_argument("--champions", required=True)
    p.add_argument("--champion_id", required=True)
    p.add_argument("--terminate_reason", default="")
    p.add_argument("--baseline_contract_path", required=True)
    p.add_argument("--train_pipeline_path", required=True)
    p.add_argument("--baseline_latency_us", type=float, required=True)
    p.add_argument("--baseline_accuracy", type=float, required=True)
    p.add_argument("--teacher_latency_us", type=float, required=True)
    p.add_argument("--target_latency_us", type=float, required=True)
    p.add_argument("--accuracy_baseline", type=float, required=True)
    p.add_argument("--accuracy_baseline_kind", required=True)
    p.add_argument("--kd_artifacts_dir", required=True)
    p.add_argument("--struct_scripts_dir", required=True)
    p.add_argument("--kd_scripts_dir", required=True)
    p.add_argument("--device", default="auto")
    p.add_argument("--seed", default="0")
    p.add_argument("--latency_provider", required=True)
    p.add_argument("--project_root", default="")
    p.add_argument("--per_run_artifacts_dir", default="")
    p.add_argument(
        "--teacher_meta", default="",
        help="teacher_meta.json 路径（All Architectures 总表读 teacher latency+accuracy）",
    )
    args = p.parse_args()

    try:
        champion = _lookup_champion(args.ledger, args.champion_id, args.baseline_contract_path)
        is_baseline = (args.champion_id == "baseline")

        if is_baseline:
            final_latency = args.baseline_latency_us
            final_accuracy = args.baseline_accuracy
            final_onnx = ""
        else:
            dummy = _read_baseline_dummy(args.baseline_contract_path)
            final_accuracy = _run_eval(
                args.train_pipeline_path, champion,
                str(args.accuracy_baseline), args.accuracy_baseline_kind,
                args.device, args.seed, args.project_root, args.per_run_artifacts_dir,
            )
            final_onnx = os.path.join(args.kd_artifacts_dir, "onnx", "final.onnx")
            _export_onnx(
                args.struct_scripts_dir, champion, dummy, final_onnx,
                args.device, args.seed,
            )
            final_latency = _measure_latency(args.latency_provider, final_onnx, args.device)

        report_path = os.path.join(args.kd_artifacts_dir, "reports", "final_report.md")
        # teacher_meta + champion_ids 喂给 All Architectures 总表。
        teacher_obj: dict[str, Any] | None = None
        if args.teacher_meta and os.path.isfile(args.teacher_meta):
            try:
                teacher_obj = json.loads(
                    Path(args.teacher_meta).read_text(encoding="utf-8")
                )
            except Exception as e:  # noqa: BLE001
                print(f"[finalize_kd] WARN: teacher_meta 不可读：{e}", file=sys.stderr)
        champ_ids: set[str] = set()
        if os.path.isfile(args.champions):
            with open(args.champions, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        c = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(c, dict):
                        continue
                    if c.get("round") != 0 and c.get("id") and c.get("id") != "baseline":
                        champ_ids.add(str(c["id"]))
        _write_report(
            report_path, args.ledger, args.champion_id, champion, is_baseline,
            args.terminate_reason, final_latency, final_accuracy,
            args.baseline_latency_us, args.baseline_accuracy, args.teacher_latency_us,
            args.target_latency_us, args.accuracy_baseline, args.accuracy_baseline_kind,
            teacher_obj=teacher_obj, champion_ids=champ_ids,
        )

        print(f"CHAMPION_IS_BASELINE: {1 if is_baseline else 0}")
        print(f"CHAMPION_STUDENT: {champion['student_path']}")
        print(f"CHAMPION_CKPT: {champion['ckpt']}")
        print(f"FINAL_LATENCY_US: {final_latency:.6f}")
        print(f"FINAL_ACCURACY: {final_accuracy:.6f}")
        print(f"FINAL_ONNX: {final_onnx}")
        print(f"FINAL_REPORT: {report_path}")
    except Exception as e:
        print(f"[finalize_kd] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main())
