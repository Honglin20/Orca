"""profile_onnx.py —— ONNX 算子级 profiling（契约 §4 逐字实现）。

对齐 `workflows/agents/_kd_scripts/CONTRACTS.md` §4 `profile_onnx`。

做的事（确定性，LLM 永不猜）：
    1. onnxruntime.InferenceSession + enable_profiling=True 跑 warmup+measure 次；
    2. end_profiling() 拿 chrome-tracing JSON；
    3. 解析每个 Node 事件的 op_name + dur → 聚合：
       - op_histogram   : 按 op_type 总耗时（μs）
       - hotspots       : topk node（按 dur）
       - transpose_count: Transpose 节点数
       - conv1d_count   : 1D Conv 节点数（kernel_shape 长度 1，从 ONNX 图直接读）
       - ascend_hints   : 启发式迁移建议（Transpose 多 / Conv1D / Softmax+MatMul 链）

CLI（契约 §4）::

    python3 profile_onnx.py --onnx <teacher.onnx> --out <profile_report.json> --topk 5

stdout（结构化 key=value，供 agent 节点 grep）::

    PROFILE_REPORT: <绝对路径>

fail loud：ONNX 不存在 / onnxruntime 加载失败 / profiling JSON 解析失败 → 非零退出 + stderr。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any


# ── ONNX graph inspection: count Conv1D（kernel_shape 长度为 1）──────────────────
def _count_convs(onnx_path: str) -> tuple[int, int]:
    """返回 (conv_total, conv1d)。读 ONNX 图 attribute.kernel_shape。

    任何异常向上抛（caller fail loud）。
    """
    import onnx

    m = onnx.load(onnx_path)
    conv_total = 0
    conv1d = 0
    for node in m.graph.node:
        if node.op_type != "Conv":
            continue
        conv_total += 1
        for attr in node.attribute:
            if attr.name == "kernel_shape":
                ks = list(attr.ints)
                if len(ks) == 1:
                    conv1d += 1
                break
    return conv_total, conv1d


# ── chrome tracing JSON 解析 ────────────────────────────────────────────────────
def _parse_trace_events(trace_path: str) -> list[dict[str, Any]]:
    """读 chrome tracing JSON，返回 Node 事件列表（含 op_name/dur/name）。"""
    with open(trace_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    # ORT 版本兼容：chrome-tracing JSON 顶层可能是 dict({"traceEvents":[...]}) 或直接 list([...])
    events = data.get("traceEvents", data) if isinstance(data, dict) else data
    node_events = []
    for ev in events:
        if ev.get("cat") != "Node":
            continue
        args = ev.get("args", {}) or {}
        op = args.get("op_name") or args.get("op") or ev.get("name", "?")
        dur = ev.get("dur")
        if dur is None:
            continue
        node_events.append(
            {
                "node": ev.get("name", ""),
                "op_type": str(op),
                "dur_us": float(dur),
            }
        )
    return node_events


def _build_hints(
    op_histogram: dict[str, float],
    transpose_count: int,
    conv1d_count: int,
) -> list[str]:
    """启发式 Ascend 迁移建议（确定性规则，非 LLM 生成）。"""
    hints: list[str] = []

    if transpose_count >= 5:
        hints.append(
            f"transpose_count={transpose_count}≥5 → 建议改 channel-last 布局 / "
            "减少 Permute（Ascend NPU 对连续 transpose 不友好）"
        )
    if conv1d_count > 0:
        hints.append(
            f"conv1d_count={conv1d_count} > 0 → 建议 reshape 成 Conv2D "
            "(在 H/W 维补 1，复用 2D 算子)"
        )
    # Softmax + MatMul 链 → attention-like，提示 fuse 或砍
    soft_us = op_histogram.get("Softmax", 0.0)
    mm_us = op_histogram.get("MatMul", 0.0)
    if soft_us > 0 and mm_us > 0 and (soft_us + mm_us) > sum(op_histogram.values()) * 0.2:
        hints.append(
            f"Softmax+MatMul 占比≈{(soft_us + mm_us) / max(sum(op_histogram.values()), 1.0) * 100:.1f}% "
            "→ 建议 fuse softmax+matmul 或砍 attention 头数"
        )
    if not hints:
        hints.append("无明显迁移瓶颈（transpose/conv1d/attention 占比均低）")
    return hints


def profile_onnx(onnx_path: str, out: str, topk: int = 5,
                 runs: int = 10, warmup: int = 3) -> dict[str, Any]:
    """跑 profiling 并聚合报告。返回 dict（同时写入 --out）。"""
    onnx_path = os.path.abspath(onnx_path)
    if not os.path.isfile(onnx_path):
        raise FileNotFoundError(f"ONNX 文件不存在: {onnx_path}")

    import onnxruntime as ort

    sess_opts = ort.SessionOptions()
    # profiling 前缀放到临时目录，避免污染 cwd
    tmp_dir = tempfile.mkdtemp(prefix="ort_profile_")
    prefix = os.path.join(tmp_dir, "ort_trace")
    sess_opts.enable_profiling = True
    sess_opts.profile_file_prefix = prefix

    sess = ort.InferenceSession(
        onnx_path,
        sess_options=sess_opts,
        providers=["CPUExecutionProvider"],  # profiling 只看算子耗时，CPU 确定性最好
    )

    # 构造 dummy 输入（动态维度 → 1）
    import numpy as np
    inp: dict[str, Any] = {}
    for i in sess.get_inputs():
        shape = [d if isinstance(d, int) else 1 for d in i.shape]
        inp[i.name] = np.random.randn(*shape).astype(np.float32)

    for _ in range(warmup):
        sess.run(None, inp)
    for _ in range(runs):
        sess.run(None, inp)

    trace_file = sess.end_profiling()  # 返回实际写入的路径
    if not trace_file or not os.path.isfile(trace_file):
        raise RuntimeError(
            f"end_profiling() 未产生文件（返回 {trace_file!r}）"
        )

    node_events = _parse_trace_events(trace_file)
    if not node_events:
        raise RuntimeError(f"trace {trace_file} 中无 Node 事件")

    # ── 聚合 ────────────────────────────────────────────────────────────────────
    op_histogram: dict[str, float] = {}
    for ev in node_events:
        op_histogram[ev["op_type"]] = op_histogram.get(ev["op_type"], 0.0) + ev["dur_us"]

    # topk：按单节点 dur 排序
    sorted_nodes = sorted(node_events, key=lambda e: e["dur_us"], reverse=True)
    # 聚合同 node 的多次 run（runs 次）：取中位数代表
    per_node: dict[str, list[float]] = {}
    per_node_op: dict[str, str] = {}
    for ev in node_events:
        per_node.setdefault(ev["node"], []).append(ev["dur_us"])
        per_node_op[ev["node"]] = ev["op_type"]
    hotspot_list = [
        {"node": n, "op_type": per_node_op[n],
         "dur_us": statistics.median(per_node[n])}
        for n in per_node
    ]
    hotspot_list.sort(key=lambda e: e["dur_us"], reverse=True)
    hotspots = hotspot_list[: max(0, int(topk))]

    transpose_count = int(sum(1 for ev in node_events if ev["op_type"] == "Transpose"))
    conv_total, conv1d_count = _count_convs(onnx_path)

    ascend_hints = _build_hints(op_histogram, transpose_count, conv1d_count)

    report = {
        "onnx": onnx_path,
        "runs": runs,
        "warmup": warmup,
        "op_histogram": op_histogram,
        "hotspots": hotspots,
        "transpose_count": transpose_count,
        "conv_count": conv_total,
        "conv1d_count": conv1d_count,
        "ascend_hints": ascend_hints,
    }

    out = os.path.abspath(out)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    return report


def _main() -> int:
    p = argparse.ArgumentParser(
        description="ONNX 算子级 profiling → profile_report.json（契约 §4）"
    )
    p.add_argument("--onnx", required=True, help="teacher.onnx 路径")
    p.add_argument("--out", required=True, help="输出 profile_report.json 路径")
    p.add_argument("--topk", type=int, default=5, help="hotspots 取前 K（默认 5）")
    p.add_argument("--runs", type=int, default=10, help="正式计时跑次数")
    p.add_argument("--warmup", type=int, default=3, help="预热次数")
    args = p.parse_args()

    try:
        report = profile_onnx(
            onnx_path=args.onnx,
            out=args.out,
            topk=args.topk,
            runs=args.runs,
            warmup=args.warmup,
        )
    except Exception as e:
        print(f"[profile_onnx] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    print(f"PROFILE_REPORT: {os.path.abspath(args.out)}")
    # 简要摘要到 stderr（不污染 agent 解析），主信号 stdout 已打。
    op_top = sorted(report["op_histogram"].items(), key=lambda kv: kv[1], reverse=True)[:5]
    print(f"# op top5 (μs): {op_top}", file=sys.stderr)
    print(f"# hotspots[0]: {report['hotspots'][0] if report['hotspots'] else None}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
