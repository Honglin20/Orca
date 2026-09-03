#!/usr/bin/env python3
"""mfu_benchmark.py — PLACEHOLDER for the real MFU benchmark script.

>>> 用真脚本替换本文件时必须保持：CLI 形状 + 输出文件名/列结构（下方 CONTRACT）。
>>> 真机脚本必须保持 schedule_result.json 的 canonical parallel_cycles 字段；
>>> latency gate 直接读取该原始 JSON，不存在适配层。

CLI (drop-in compatible with the real script):

    python mfu_benchmark.py <onnx_path> [--chip 6613|1951] [--precision INT8|INT16|AMP]
                            [--core-num 1|2|4] [--dma-width F] [--max-time S]
                            [--latency-only] [-o/--output DIR] [--timeout S]

CONTRACT — files written into <output>/<onnx_stem>/:

  <stem>.log                 run log (human-readable, deterministic)
  <chip>_<stem>.csv          columns: name,op_type,cycles,mfu,delay_cycles
  <stem>.macs.csv            columns: name,op_type,macs
  subgraph_0_tasks.json      {"subgraph":0,"tasks":[{task_id,name,op_type,cycles,
                             delay_cycles,flops,memory,output_dimensions}]}
  schedule_result.json       {"schema_version":1,"chip":..,"precision":..,"core_num":..,
                             "serial_cycles":S,"parallel_cycles":M,"subgraph_count":1}
  <stem>_taskgraph.json      {"schema_version":1,"onnx":..,"operators":[{name,op_type,
                             task_id,pipeline,depends_on,output_memory,output_dimensions}]}

CANONICAL MAKESPAN = schedule_result.json "parallel_cycles" (integer cycles).

Placeholder behavior:
- Deterministic heuristic cost model (no randomness; --seed-free by design).
- --precision / --core-num / --dma-width / --max-time / --latency-only are accepted
  for CLI compatibility but do not change placeholder output.
- Unsupported op_type -> rc=2, one "unsupported: <op_type>" line per type on stderr.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import onnx
from onnx import TensorProto

SCHEMA_VERSION = 1

# heuristic cost model (integer cycles) — same flavor as the repo placeholder
MAC_FLOPS_PER_CYCLE = 32
ELEM_PER_CYCLE = 8
TRANSCENDENTAL_PER_CYCLE = 2
REDUCE_PER_CYCLE = 8
MOVE_BYTES_PER_CYCLE = 32
FIXED_OVERHEAD = 16

_ELEMENTWISE = frozenset({
    "Relu", "LeakyRelu", "PRelu", "Add", "Sub", "Mul", "Min", "Max", "Sum",
    "Mean", "Where", "Clip", "HardSigmoid", "HardSwish", "Not", "And", "Or",
    "Xor", "Equal", "Greater", "GreaterOrEqual", "Less", "LessOrEqual",
    "Cast", "CastLike", "Dropout",
})
_TRANSCENDENTAL = frozenset({
    "Erf", "Tanh", "Sigmoid", "Exp", "Log", "Sqrt", "Pow", "Abs", "Neg",
    "Reciprocal", "Floor", "Ceil", "Round", "Sign", "Div",
})
_REDUCE = frozenset({
    "ReduceMean", "ReduceSum", "ReduceMax", "ReduceMin", "ReduceProd",
    "ReduceL2", "GlobalAveragePool",
})
_MOVES = frozenset({
    "Transpose", "Reshape", "Flatten", "Concat", "Split", "Slice", "Gather",
    "GatherElements", "Expand", "Tile", "Pad", "Squeeze", "Unsqueeze",
    "ConstantOfShape",
})
SUPPORTED_OPS = (_ELEMENTWISE | _TRANSCENDENTAL | _REDUCE | _MOVES) | frozenset({
    "Conv", "MatMul", "Gemm", "Einsum", "Range", "Softmax", "LayerNormalization",
    "BatchNormalization", "InstanceNormalization",
    "MaxPool", "AveragePool", "Identity", "Constant", "Shape", "Size",
})

_DTYPE_BYTES = {
    TensorProto.DOUBLE: 8, TensorProto.INT64: 8, TensorProto.FLOAT: 4,
    TensorProto.INT32: 4, TensorProto.INT16: 2, TensorProto.INT8: 1,
    TensorProto.UINT8: 1, TensorProto.BOOL: 1,
}


class UnsupportedOpsError(RuntimeError):
    def __init__(self, op_types: list[str]):
        super().__init__("unsupported op types: " + ", ".join(op_types))
        self.op_types = op_types


def _prod(dims) -> int:
    out = 1
    for d in dims:
        out *= int(d)
    return out


def _tensor_dims_map(model: onnx.ModelProto) -> dict[str, tuple[list[int], int]]:
    inferred = onnx.shape_inference.infer_shapes(model)
    out: dict[str, tuple[list[int], int]] = {}
    for vi in list(inferred.graph.value_info) + list(inferred.graph.input) + list(inferred.graph.output):
        tt = vi.type.tensor_type
        if not tt.HasField("shape"):
            continue
        dims: list[int] = []
        ok = True
        for d in tt.shape.dim:
            which = d.WhichOneof("value")
            if which == "dim_value":
                dims.append(int(d.dim_value))
            elif which == "dim_param" and d.dim_param == "":
                dims.append(1)
            else:
                ok = False
                break
        if ok:
            out[vi.name] = (dims, int(tt.elem_type))
    return out


def _attr_ints(node: onnx.NodeProto, name: str) -> list[int] | None:
    for a in node.attribute:
        if a.name == name:
            return list(a.ints)
    return None


def _macs(op: str, node: onnx.NodeProto, out_elems: int,
          in_dims: list[list[int]]) -> int:
    if op == "Conv":
        kernel = _attr_ints(node, "kernel_shape") or [1]
        group = (_attr_ints(node, "group") or [1])[0]
        cin = in_dims[0][1] if len(in_dims) > 0 and len(in_dims[0]) > 1 else 1
        return out_elems * max(cin // group, 1) * _prod(kernel)
    if op == "MatMul":
        k = in_dims[0][-1] if len(in_dims) > 0 and in_dims[0] else 1
        return out_elems * k
    if op == "Gemm":
        k = in_dims[0][-1] if len(in_dims) > 0 and in_dims[0] else 1
        return out_elems * k
    if op == "Einsum":
        k = max((d[-1] for d in in_dims if d), default=1)
        return out_elems * k
    if op in ("MaxPool", "AveragePool"):
        kernel = _attr_ints(node, "kernel_shape") or [1]
        return out_elems * _prod(kernel)
    return 0


def _cost_cycles(op: str, node: onnx.NodeProto, out_dims: list[int],
                 in_dims: list[list[int]]) -> tuple[int, int, int]:
    """returns (cycles, delay_cycles, macs) — deterministic heuristic."""
    out_elems = max(_prod(out_dims), 1)
    macs = _macs(op, node, out_elems, in_dims)
    delay = FIXED_OVERHEAD // 4  # nominal DMA wait for compute ops

    if op == "Identity":
        return 1, 0, 0
    if op == "Constant":
        return 0, 0, 0
    if macs > 0:
        return 2 * macs // MAC_FLOPS_PER_CYCLE + FIXED_OVERHEAD, delay, macs
    if op in _REDUCE:
        in_elems = max(_prod(in_dims[0]), 1) if len(in_dims) > 0 and in_dims[0] else out_elems
        return in_elems // REDUCE_PER_CYCLE + FIXED_OVERHEAD, delay, 0
    if op in _MOVES:
        move = out_elems * 4 // MOVE_BYTES_PER_CYCLE + FIXED_OVERHEAD
        return move, move, 0  # move ops: cost IS the DMA wait
    if op == "Softmax":
        return out_elems // TRANSCENDENTAL_PER_CYCLE + FIXED_OVERHEAD, delay, 0
    if op == "LayerNormalization":
        return out_elems * 3 // ELEM_PER_CYCLE + FIXED_OVERHEAD, delay, 0
    if op in ("BatchNormalization", "InstanceNormalization"):
        return out_elems // ELEM_PER_CYCLE + FIXED_OVERHEAD, delay, 0
    if op in _TRANSCENDENTAL:
        return out_elems // TRANSCENDENTAL_PER_CYCLE + FIXED_OVERHEAD, delay, 0
    return out_elems // ELEM_PER_CYCLE + FIXED_OVERHEAD, delay, 0


def _mfu(cycles: int, macs: int) -> float:
    if macs <= 0 or cycles <= 0:
        return 0.0
    return round(min(1.0, (2 * macs // MAC_FLOPS_PER_CYCLE) / cycles), 4)


def _list_schedule(n: int, names: list[str], latencies: list[int],
                   deps: list[list[int]]) -> int:
    """single-machine list schedule, critical-path priority, deterministic."""
    succs: list[list[int]] = [[] for _ in range(n)]
    for i, dep in enumerate(deps):
        for d in dep:
            succs[d].append(i)
    downstream = [0] * n
    for i in reversed(range(n)):
        downstream[i] = latencies[i] + max((downstream[s] for s in succs[i]), default=0)
    end_at = [0] * n
    done = [False] * n
    for _ in range(n):
        ready = [i for i in range(n) if not done[i] and all(done[d] for d in deps[i])]
        if not ready:
            raise RuntimeError("dependency cycle — not a DAG")
        pick = min(ready, key=lambda i: (-downstream[i], names[i]))
        start = max((end_at[d] for d in deps[pick]), default=0)
        end_at[pick] = start + latencies[pick]
        done[pick] = True
    return max(end_at) if end_at else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="MFU benchmark (PLACEHOLDER)")
    ap.add_argument("onnx_path")
    ap.add_argument("--chip", choices=["6613", "1951"], default="6613")
    ap.add_argument("--precision", choices=["INT8", "INT16", "AMP"], default="INT8")
    ap.add_argument("--core-num", type=int, choices=[1, 2, 4], default=1)
    ap.add_argument("--dma-width", type=float, default=542.72)
    ap.add_argument("--max-time", type=float, default=15)
    ap.add_argument("--latency-only", action="store_true")
    ap.add_argument("--output", "-o", default=None)
    ap.add_argument("--timeout", type=int, default=600)
    ns = ap.parse_args()

    t0 = time.time()
    onnx_path = Path(ns.onnx_path)
    out_dir = (Path(ns.output) if ns.output else onnx_path.parent) / onnx_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = onnx_path.stem
    log_lines: list[str] = [
        f"[mfu-placeholder] onnx={onnx_path}",
        f"[mfu-placeholder] chip={ns.chip} precision={ns.precision} core_num={ns.core_num}",
    ]

    try:
        model = onnx.load(str(onnx_path))
        unsupported = sorted({nd.op_type for nd in model.graph.node
                              if nd.op_type not in SUPPORTED_OPS})
        if unsupported:
            for op in unsupported:
                print(f"unsupported: {op}", file=sys.stderr)
            return 2

        dims_map = _tensor_dims_map(model)
        initializer_names = {i.name for i in model.graph.initializer}
        producer: dict[str, int] = {}
        for idx, nd in enumerate(model.graph.node):
            for out in nd.output:
                producer[out] = idx

        n = len(model.graph.node)
        names, op_types, task_ids = [], [], []
        latencies, delays, macs_list = [], [], []
        deps: list[list[int]] = []
        out_mem, out_dims_list = [], []

        for idx, nd in enumerate(model.graph.node):
            name = nd.name or f"{nd.op_type}_{idx}"
            names.append(name)
            op_types.append(nd.op_type)
            task_ids.append(f"t{idx:04d}")
            in_dims = [dims_map[i][0] for i in nd.input if i in dims_map]
            primary = nd.output[0] if nd.output else ""
            if primary not in dims_map:
                raise RuntimeError(
                    f"static shape unavailable for {primary!r} (node {name!r})")
            tdims, elem = dims_map[primary]
            out_dims_list.append(list(tdims))
            out_mem.append(_prod(tdims) * _DTYPE_BYTES.get(elem, 4))
            cyc, dly, mcs = _cost_cycles(nd.op_type, nd, list(tdims), in_dims)
            latencies.append(int(cyc))
            delays.append(int(dly))
            macs_list.append(int(mcs))
            deps.append(sorted({producer[i] for i in nd.input
                                if i in producer and i not in initializer_names}))

        serial = sum(latencies)
        parallel = _list_schedule(n, names, latencies, deps)

        # ── write artifacts (CONTRACT: names/columns are the adapter's input) ──
        with open(out_dir / f"{ns.chip}_{stem}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["name", "op_type", "cycles", "mfu", "delay_cycles"])
            for i in range(n):
                w.writerow([names[i], op_types[i], latencies[i],
                            _mfu(latencies[i], macs_list[i]), delays[i]])

        with open(out_dir / f"{stem}.macs.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["name", "op_type", "macs"])
            for i in range(n):
                w.writerow([names[i], op_types[i], macs_list[i]])

        (out_dir / "subgraph_0_tasks.json").write_text(json.dumps({
            "subgraph": 0,
            "tasks": [
                {"task_id": task_ids[i], "name": names[i], "op_type": op_types[i],
                 "cycles": latencies[i], "delay_cycles": delays[i],
                 "flops": 2 * macs_list[i], "memory": out_mem[i],
                 "output_dimensions": out_dims_list[i]}
                for i in range(n)
            ],
        }, indent=2), encoding="utf-8")

        (out_dir / "schedule_result.json").write_text(json.dumps({
            "schema_version": SCHEMA_VERSION,
            "chip": ns.chip, "precision": ns.precision, "core_num": ns.core_num,
            "serial_cycles": serial, "parallel_cycles": parallel,
            "subgraph_count": 1,
        }, indent=2), encoding="utf-8")

        (out_dir / f"{stem}_taskgraph.json").write_text(json.dumps({
            "schema_version": SCHEMA_VERSION,
            "onnx": str(onnx_path.resolve()),
            "operators": [
                {"name": names[i], "op_type": op_types[i], "task_id": task_ids[i],
                 "pipeline": "subgraph_0",
                 "depends_on": [names[d] for d in deps[i]],
                 "output_memory": out_mem[i],
                 "output_dimensions": out_dims_list[i]}
                for i in range(n)
            ],
        }, indent=2), encoding="utf-8")

        log_lines += [
            f"[mfu-placeholder] nodes={n} serial_cycles={serial} parallel_cycles={parallel}",
            f"[mfu-placeholder] done in {time.time() - t0:.2f}s -> {out_dir}",
        ]
        (out_dir / f"{stem}.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        print(json.dumps({"output_dir": str(out_dir.resolve()),
                          "serial_cycles": serial, "parallel_cycles": parallel,
                          "op_count": n}))
        return 0
    except Exception as exc:  # noqa: BLE001 — fail loud with a clear cause
        print(f"mfu_benchmark(placeholder): FAIL {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
