#!/usr/bin/env python3
"""push_describe.py —— C1 基线模型表 + C2 超网/搜索空间表（viz_describe 调用）。

E1/E2 共识：
  - C2 不放 block_latency_ms（latency 运行时实测，inspect 里不可靠/常缺）。
  - params 直读 `elastic_num_params` / numel()（确定性 int），字符串解析仅兜底。
best-effort：supernet.py 由 LLM 生成，结构因 model_type 而异；import 失败或字段缺失 → 推 ERROR 表（F1），
绝不静默空图。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    from orca.chart import render_chart  # type: ignore
except Exception as e:  # pragma: no cover
    sys.stderr.write(f"[push_describe] 无法 import orca.chart：{e}\n")
    sys.exit(2)


def _err(label: str, title: str, msg: str) -> None:
    render_chart(
        chart_type="table",
        data=[{"key": "error", "value": msg[:300]}],
        label=label,
        title=f"⚠ {title}",
        columns=["key", "value"],
    )


def _fmt_num(n: float | int | None) -> str:
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    for unit, scale in (("G", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= scale:
            return f"{n / scale:.2f}{unit}"
    return f"{n:.0f}"


def _parse_summary(md_path: Path) -> dict[str, str]:
    """从 supernet_summary.md 抽取关键信息（宽松文本匹配）。"""
    info: dict[str, str] = {}
    if not md_path.is_file():
        return info
    text = md_path.read_text(encoding="utf-8", errors="replace")
    # 粗提取：Source Project / Model Type 行
    for key in ("Source Project", "Model Type", "Model type", "Evaluation Paradigm", "Training Viability"):
        m = re.search(rf"{re.escape(key)}[:：]\s*(.+)", text)
        if m:
            info[key] = m.group(1).strip()
    return info


def _import_supernet(output_dir: Path):
    sys.path.insert(0, str(output_dir))
    try:
        # 强制重导入（viz_describe 可能与 train 阶段同进程序列里复用）
        import importlib
        if "supernet" in sys.modules:
            importlib.reload(sys.modules["supernet"])
        import supernet as sn_mod  # type: ignore
        return sn_mod
    finally:
        pass


def _c1_baseline(output_dir: Path, summary: dict[str, str]) -> None:
    flat = next(output_dir.glob("*_flat.py"), None)
    rows = [
        {"aspect": "output_dir", "value": str(output_dir)},
        {"aspect": "baseline_file", "value": flat.name if flat else "(未找到 *_flat.py)"},
        {"aspect": "model_type", "value": summary.get("Model Type") or summary.get("Model type") or "(见 C2)"},
        {"aspect": "source_project", "value": summary.get("Source Project", "(见 supernet_summary.md)")},
        {"aspect": "eval_paradigm", "value": summary.get("Evaluation Paradigm", "—")},
        {"aspect": "training_viable", "value": summary.get("Training Viability", "—")},
    ]
    render_chart(
        chart_type="table",
        data=rows,
        label="nas/baseline",
        title="Baseline Model",
        columns=["aspect", "value"],
    )


def _c2_supernet(output_dir: Path) -> None:
    try:
        sn_mod = _import_supernet(output_dir)
        SearchSpace = getattr(sn_mod, "SearchSpace")
        ss = SearchSpace()
        d = dataclasses.asdict(ss)
    except Exception as e:
        _err("nas/supernet", "Supernet & Search Space", f"无法 import/解析 supernet.py：{type(e).__name__}: {e}")
        return

    # model_type 判定
    has_stages = "stage_names" in d and d["stage_names"]
    is_iso = "global_dim" in d or "layer_configs" in d and not has_stages

    total_params: float | None = None
    try:
        SuperNet = getattr(sn_mod, "SuperNet", None)
        if SuperNet is not None:
            net = SuperNet(ss)
            total_params = float(sum(p.numel() for p in net.parameters()))
    except Exception:
        pass

    rows: list[dict[str, Any]] = []
    if has_stages:
        names = d.get("stage_names", [])
        widths = d.get("stage_widths") or d.get("stage_emb_dims") or []
        depths = d.get("stage_depth_candidates", [])
        layer_cfgs = d.get("stage_layer_configs", [])
        for i, name in enumerate(names):
            w = widths[i] if i < len(widths) else ""
            dep = depths[i] if i < len(depths) else ""
            cfg = layer_cfgs[i] if i < len(layer_cfgs) else {}
            choices = ", ".join(sorted(cfg.keys())) if isinstance(cfg, dict) else str(cfg)
            rows.append(
                {
                    "stage": name,
                    "width/emb": str(w),
                    "depth_candidates": str(dep),
                    "block_choices": choices,
                }
            )
        cols = ["stage", "width/emb", "depth_candidates", "block_choices"]
    else:
        # isotropic
        lc = d.get("layer_configs", {})
        if isinstance(lc, dict):
            for name, cands in lc.items():
                rows.append({"block": name, "config_candidates": str(cands)})
        cols = ["block", "config_candidates"]
        if not rows:
            rows = [{"dim": k, "value": str(v)} for k, v in d.items()][:20]
            cols = ["dim", "value"]

    # 把超网总参数量放进第一行注释（不污染结构列）
    title = "Supernet & Search Space"
    if total_params is not None:
        title += f"  (supernet params ≈ {_fmt_num(total_params)})"
    render_chart(chart_type="table", data=rows, label="nas/supernet", title=title, columns=cols)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output_dir", required=True)
    args = ap.parse_args()
    output_dir = Path(args.output_dir)
    if not output_dir.is_dir():
        sys.stderr.write(f"[push_describe] output_dir 不存在：{output_dir}\n")
        return 1
    summary = _parse_summary(output_dir / "supernet_summary.md")
    try:
        _c1_baseline(output_dir, summary)
    except Exception as e:
        _err("nas/baseline", "Baseline Model", f"{type(e).__name__}: {e}")
    try:
        _c2_supernet(output_dir)
    except Exception as e:
        _err("nas/supernet", "Supernet & Search Space", f"{type(e).__name__}: {e}")
    print("[push_describe] C1/C2 pushed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
