#!/usr/bin/env python3
"""push_results.py —— 读 results.jsonl 推 3 张图（contracts §7）。

构造 3 个 chart payload：
    1. pareto：latency_ms × accuracy，方向由 accuracy_kind 推。
    2. bar：variant × accuracy，hue=kd。
    3. table：全实验总表。

推图策略：
    - 在 Orca 编排子进程内（``orca.chart`` 可导入 + ORCA_* env 齐全）→ 推 3 张图，
      stdout ``CHART_PUSHED``。
    - 否则（无 orca / 缺 env / 全部 render_chart 失败）→ 3 个 payload 写 <out> JSON，
      stdout ``CHART_FALLBACK_JSON: <path>``。不崩。

accuracy 方向（contracts §7）：
    {snr, acc} → max；{mse, nmse, ber, db} → min；未知 → fail loud。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# accuracy_kind → pareto y 轴方向（contracts §7）。
MAX_KINDS = {"snr", "acc"}
MIN_KINDS = {"mse", "nmse", "ber", "db"}

# 推图固定 label / titles。
LABEL = "rx-sweep/results"
TITLES = {
    "pareto": "Latency vs Accuracy (Pareto)",
    "bar": "Accuracy by Variant",
    "table": "All Experiments",
}


def accuracy_direction(accuracy_kind: str | None) -> str:
    """据 accuracy_kind 决定 pareto y 轴方向（max / min）。未知 → ValueError。"""
    k = (accuracy_kind or "").strip().lower()
    if k in MAX_KINDS:
        return "max"
    if k in MIN_KINDS:
        return "min"
    raise ValueError(
        f"未知 accuracy_kind {accuracy_kind!r}：无法决定 pareto y 方向。"
        f"已知 max 方向 {sorted(MAX_KINDS)} / min 方向 {sorted(MIN_KINDS)}。"
        f"请在 results.jsonl 每行写明 accuracy_kind 字段。"
    )


def load_results(results_path: Path) -> list[dict[str, Any]]:
    """读 jsonl → list[dict]。空文件 → 空列表。每行必须合法 JSON，否则 fail loud。"""
    rows: list[dict[str, Any]] = []
    text = results_path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        if not s:
            continue
        try:
            row = json.loads(s)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"{results_path}:{lineno} 非 JSON：{e.msg}（原文：{s!r}）"
            ) from e
        if not isinstance(row, dict):
            raise ValueError(
                f"{results_path}:{lineno} 顶层非 dict（实际 {type(row).__name__}）"
            )
        rows.append(row)
    return rows


def build_payloads(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """构造 3 个 render_chart 关键字参数 dict（pareto / bar / table）。"""
    # 多 accuracy_kind fail loud（M1）：方向冲突时取首个会静默用错方向。
    # 空结果集 → accuracy_kind=None，y_direction 走 max 默认（无数据无影响）。
    kinds = {r.get("accuracy_kind") for r in rows if r.get("accuracy_kind")}
    if len(kinds) > 1:
        raise ValueError(
            f"results 含多种 accuracy_kind {sorted(kinds)}，方向冲突；"
            f"请按 accuracy_kind 分批推图（contracts §7：每批方向需一致）。"
        )
    accuracy_kind = next(iter(kinds)) if kinds else None
    y_direction = accuracy_direction(accuracy_kind) if rows else "max"

    return [
        {
            "chart_type": "pareto",
            "data": rows,
            "x": "latency_ms",
            "y": "accuracy",
            "pareto_x_direction": "min",
            "pareto_y_direction": y_direction,
            "label": LABEL,
            "title": TITLES["pareto"],
            "x_label": "延迟 (ms, 越低越好)",
            "y_label": f"accuracy ({accuracy_kind or '?'}, {y_direction})",
        },
        {
            "chart_type": "bar",
            "data": rows,
            "x": "variant",
            "y": "accuracy",
            "hue": "kd",
            "label": LABEL,
            "title": TITLES["bar"],
            "x_label": "variant",
            "y_label": "accuracy",
        },
        {
            "chart_type": "table",
            "data": rows,
            "columns": [
                "exp_id",
                "variant",
                "kd",
                "accuracy",
                "latency_ms",
                "status",
            ],
            "label": LABEL,
            "title": TITLES["table"],
        },
    ]


def try_push(payloads: list[dict[str, Any]], anchor_path: Path | None = None) -> bool:
    """尝试 lazy import orca.chart 并推 3 张图。

    任意张成功 → 返回 True（CHART_PUSHED）；全部失败 / 无 orca → 返回 False（fallback）。
    单张失败只 stderr，不阻断其它。

    skill 的 bash subprocess 可能没继承 run env（ORCA_RUN_ID/NODE/SESSION_ID/CHART_SOCK）
    → render_chart 会 fail loud。先从 anchor_path（= results.jsonl）向上找 orca_env.sh 把
    4 个身份键补回 os.environ（_env.load_run_env_from_artifacts，与 kd.trainer 的 env
    bootstrap 同源）。best-effort：失败走 render_chart 自己的 fail-soft。
    """
    if anchor_path is not None:
        try:
            from orca.chart._env import load_run_env_from_artifacts
            injected = load_run_env_from_artifacts(anchor_path)
            if injected:
                print(
                    f"[push_results] env 自加载补键：{sorted(injected)}",
                    file=sys.stderr,
                )
        except Exception as e:  # noqa: BLE001 —— env 自加载 best-effort
            print(
                f"[push_results] env 自加载跳过：{type(e).__name__}: {e}",
                file=sys.stderr,
            )
    try:
        from orca.chart import render_chart  # lazy import（用户工程可能没装 orca）
    except ImportError as e:
        print(
            f"[push_results] orca.chart 不可导入（{e}）→ 走 fallback JSON。",
            file=sys.stderr,
        )
        return False

    any_success = False
    for payload in payloads:
        title = payload.get("title", "<无 title>")
        try:
            seq = render_chart(**payload)
            print(f"[push_results] pushed {title!r} seq={seq}", file=sys.stderr)
            any_success = True
        except Exception as e:  # RuntimeError(env 缺/sock) / ValueError(校验) 等
            print(
                f"[push_results] 推图失败 {title!r}：{type(e).__name__}: {e}",
                file=sys.stderr,
            )
    return any_success


def write_fallback(payloads: list[dict[str, Any]], out_path: Path) -> None:
    """把 3 个 payload 写 <out> JSON（fail-soft 落地，供后续 / 离线渲染）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payloads, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="读 results.jsonl 推 3 张图（contracts §7），fail-soft 落 JSON。"
    )
    parser.add_argument(
        "--results", required=True, help="launch_sweep.py 生成的 results.jsonl。"
    )
    parser.add_argument(
        "--out",
        default="chart.json",
        help="fallback 输出 JSON 路径（默认 chart.json）。",
    )
    args = parser.parse_args(argv)

    results_path = Path(args.results)
    try:
        rows = load_results(results_path)
    except (OSError, ValueError) as e:
        print(f"[push_results] 错误：读 results 失败：{e}", file=sys.stderr)
        return 2

    try:
        payloads = build_payloads(rows)
    except ValueError as e:
        # 未知 accuracy_kind → fail loud（contracts §7 契约）。
        print(f"[push_results] 错误：{e}", file=sys.stderr)
        return 2

    if try_push(payloads, results_path):
        print("CHART_PUSHED")
        return 0

    out_path = Path(args.out)
    try:
        write_fallback(payloads, out_path)
    except OSError as e:
        print(
            f"[push_results] 错误：写 fallback JSON 失败 {out_path}: {e}",
            file=sys.stderr,
        )
        return 1
    print(f"CHART_FALLBACK_JSON: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
