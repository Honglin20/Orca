#!/usr/bin/env python3
"""select_and_report.py —— NAS slim 第五步：脚本化架构选择 + 报告（nas-select agent 调用）。

零 LLM（替代重流水线的 evaluator）：
  1. subprocess 调 ``nas-select-architecture``（选 top-N 帕累托架构 → selection_summary.json）。
  2. 读 selection_summary.json + search.jsonl，**模板填空** final_report.md。
  3. 推 C5 终态帕累托 + C6 漏斗（subprocess 同级 push_pareto_final.py / push_funnel.py）。
  4. stdout 打印结构化摘要。

fail loud：select CLI 退出非 0 → 把原因写 final_report.md 并以非 0 退出（不假装完成）。
推图（C5/C6）是 sidecar：失败不阻断主流程（except 吞 + stderr loud），但选择失败必须可见。
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent


def _run_select(output_dir: Path, n: int) -> tuple[int, str]:
    """跑 nas-select-architecture（用 sys.executable -m，不依赖 console_script 上 PATH）。

    cwd=<output_dir>：search_config.yaml 引用 `supernet.SearchSpace`（及 arch_codec /
    latency_estimator 等同级模块），`python -m` 把 cwd 放 sys.path[0]，故须在 output_dir
    内运行才能 `import supernet`。即便上游 agent 忘了 cd，这里也强制对齐（fail loud + 精确）。
    """
    config = output_dir / "search_config.yaml"
    inp = output_dir / "runs" / "search" / "search.jsonl"
    arch_out = output_dir / "runs" / "retrain" / "selected"
    if not config.is_file():
        sys.stderr.write(f"[select_and_report] 缺 search_config.yaml：{config}\n")
        return 2, f"missing {config}"
    if not inp.is_file():
        sys.stderr.write(f"[select_and_report] 缺 search.jsonl：{inp}\n")
        return 2, f"missing {inp}"
    # arch_output_dir 的 mkdir 由 CLI 内部完成（select_architecture.py: arch_output_dir.mkdir）。
    cmd = [
        sys.executable, "-m", "nas_agent.cli.select_architecture",
        "--config", str(config),
        "--input", str(inp),
        "--arch_output_dir", str(arch_out),
        "-n", str(n),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(output_dir))
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return out


def _classify_obj(values: list[float]) -> str:
    """与 tail_metrics/push_pareto 同口径：≤0 居多 → 质量（负向化，显示 -v）；否则成本。

    PARITY 契约：本函数 + ``_read_jsonl`` 与 ``nas-train-runner/scripts/tail_metrics.py`` 及
    ``nas-select/scripts/push_pareto_final.py`` 三处逻辑必须保持一致（Orca folder-agent 脚本
    需自包含，故未抽共享模块）。改分类口径时三处同步——否则 report/tail/pareto 显示会漂移。
    """
    vals = [v for v in values if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v))]
    if not vals:
        return "cost"
    return "quality" if sum(1 for v in vals if v <= 0) >= len(vals) / 2 else "cost"


def _best_per_obj(recs: list[dict[str, Any]]) -> dict[str, tuple[float, str]]:
    """每个 obj 的最佳值 + 性质（质量 best=max，成本 best=min）。objs 全体被最小化。"""
    obj_keys: list[str] = []
    for r in recs:
        for k in (r.get("objs") or {}):
            if k not in obj_keys:
                obj_keys.append(k)
    best: dict[str, tuple[float, str]] = {}
    for k in obj_keys:
        vals = [(r.get("objs") or {}).get(k) for r in recs]
        vals = [v for v in vals if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v))]
        if not vals:
            continue
        kind = _classify_obj(vals)
        best[k] = (max(vals), kind) if kind == "quality" else (min(vals), kind)
    return best


def _fmt_obj(name: str, value: float, kind: str) -> str:
    """质量目标显示 -value（正，直观）；成本目标原值。latency 加 ms。"""
    disp = -value if kind == "quality" else value
    unit = " ms" if "lat" in name.lower() else ""
    return f"{disp:.4g}{unit}"


def _build_report(
    output_dir: Path,
    summary: dict[str, Any] | None,
    recs: list[dict[str, Any]],
    select_ok: bool,
    select_log: str,
    n: int,
) -> str:
    sel = (summary or {}).get("selected", [])
    best = _best_per_obj(recs)
    lines = [
        "# NAS Final Report",
        "",
        f"- output_dir: `{output_dir}`",
        f"- total evaluated records: {len(recs)}",
        f"- pareto (input): {(summary or {}).get('num_input_pareto_records', '—')}",
        f"- feasible: {(summary or {}).get('num_feasible_architectures', '—')}",
        f"- feasible_pareto: {(summary or {}).get('num_feasible_pareto_architectures', '—')}",
        f"- selected (top-{n}): {len(sel)}",
        "",
        "## Best objective values (over all evaluated)",
        "",
    ]
    if best:
        for k, (v, kind) in best.items():
            lines.append(f"- **{k}** ({kind}): best = {_fmt_obj(k, v, kind)}")
    else:
        lines.append("- (no objective values found in search.jsonl)")
    lines += ["", "## Selected architectures", ""]
    if sel:
        for i, s in enumerate(sel, 1):
            # selection_summary.json 的 selected items 用 "objectives" key（非 "objs"，
            # 后者只在 search.jsonl 记录里）。kind 复用全局 _best_per_obj 的分类，避免逐值
            # 重判在同一目标上自相矛盾（见 _best_per_obj）。
            objs = s.get("objectives") or {}
            parts = []
            for k, v in objs.items():
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    parts.append(f"{k}={v}")
                    continue
                kind = best[k][1] if k in best else _classify_obj([fv])
                parts.append(f"{k}={_fmt_obj(k, fv, kind)}")
            obj_str = ", ".join(parts) or "—"
            lines.append(f"{i}. gene=`{json.dumps(s.get('gene'), separators=(',', ':'))}` — {obj_str}")
    else:
        lines.append("- (none)")
    if not select_ok:
        lines += ["", "## ⚠ Architecture selection FAILED", "", "```", select_log.strip()[-2000:], "```"]
    return "\n".join(lines) + "\n"


def _push_sidecar(script: str, output_dir: Path) -> None:
    """subprocess 跑同级 push_*.py（render_chart 经 env 链可用）。失败 stderr loud 但不 raise。"""
    path = SCRIPT_DIR / script
    if not path.is_file():
        sys.stderr.write(f"[select_and_report] sidecar 缺失：{path}\n")
        return
    try:
        proc = subprocess.run(
            [sys.executable, str(path), "--output_dir", str(output_dir)],
            capture_output=True, text=True,
        )
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        if proc.returncode != 0 and proc.stderr:
            sys.stderr.write(f"[select_and_report] {script} 非 0 退出（sidecar，不阻断）：\n{proc.stderr}\n")
    except Exception as e:  # sidecar：绝不阻断主流程
        sys.stderr.write(f"[select_and_report] {script} 异常（已吞）：{type(e).__name__}: {e}\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output_dir", required=True)
    ap.add_argument("-n", "--num_select", type=int, default=3, help="选 top-N 架构")
    args = ap.parse_args()
    output_dir = Path(args.output_dir)
    if not output_dir.is_dir():
        sys.stderr.write(f"[select_and_report] output_dir 不存在：{output_dir}\n")
        return 1

    rc, select_log = _run_select(output_dir, args.num_select)
    select_ok = rc == 0
    if select_ok:
        sys.stdout.write("[select_and_report] nas-select-architecture OK\n")
    else:
        sys.stderr.write(f"[select_and_report] nas-select-architecture 失败 (rc={rc})\n{select_log}\n")

    summary_path = output_dir / "runs" / "retrain" / "selected" / "selection_summary.json"
    summary: dict[str, Any] | None = None
    # 仅在本次选择成功时读 summary——失败时 summary 可能是上一次的 stale 残留，
    # 读它会与 "⚠ FAILED" 段自相矛盾（operator 误以为"选了 N 个又失败"）。失败 → summary=None。
    if select_ok and summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception as e:
            sys.stderr.write(f"[select_and_report] 解析 selection_summary.json 失败：{e}\n")

    recs = _read_jsonl(output_dir / "runs" / "search" / "search.jsonl")

    report = _build_report(output_dir, summary, recs, select_ok, select_log, args.num_select)
    report_path = output_dir / "final_report.md"
    report_path.write_text(report, encoding="utf-8")

    # C5 终态帕累托 + C6 漏斗（sidecar，不阻断）
    _push_sidecar("push_pareto_final.py", output_dir)
    _push_sidecar("push_funnel.py", output_dir)

    n_sel = len((summary or {}).get("selected", []))
    print("OUTPUT_DIR: " + str(output_dir), flush=True)
    print(f"SELECTED: {n_sel}", flush=True)
    print("SELECTION_SUMMARY: " + str(summary_path), flush=True)
    print("FINAL_REPORT: " + str(report_path), flush=True)
    return 0 if select_ok else rc


if __name__ == "__main__":
    sys.exit(main())
