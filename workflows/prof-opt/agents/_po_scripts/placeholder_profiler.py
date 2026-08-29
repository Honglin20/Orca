#!/usr/bin/env python3
"""placeholder_profiler.py — reference implementation of PROFILER_CONTRACT.md.

onnx load + shape inference -> heuristic cost table -> pipeline mapping ->
deterministic list schedule -> the four contract artifacts. Fidelity target is
DELTA-DIRECTION correctness only (see PROFILER_CONTRACT.md): a structural
change that removes work must lower makespan_cycles.

Deterministic: no randomness anywhere (--seed is accepted for CLI
compatibility and unused). Unsupported op -> rc=2 + stderr `unsupported: <op>`.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import onnx
from onnx import TensorProto

SCHEMA_VERSION = 1

# ── heuristic cost model (integer cycles; unit pinned by the contract) ────────
MAC_FLOPS_PER_CYCLE = 32        # conv / matmul arithmetic throughput
ELEM_PER_CYCLE = 8              # simple elementwise rate
TRANSCENDENTAL_PER_CYCLE = 2    # erf/tanh/exp/sqrt/div-class rate
REDUCE_PER_CYCLE = 8            # reduction element ingest rate
MOVE_BYTES_PER_CYCLE = 32       # data-movement ops (transpose/copy/concat/...)
FIXED_OVERHEAD = 16             # per-op fixed cost
IDENTITY_CYCLES = 1

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
    """tensor name -> (static dims, elem_type) from graph IO + shape inference.
    Symbolic (dynamic) dims fail loudly: the contract is static-shape only."""
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
                dims.append(1)  # anonymous empty dim: degenerate, treat as 1
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


def _cost_cycles(op: str, node: onnx.NodeProto, out_dims: list[int],
                 in_dims: list[list[int]]) -> int:
    """Heuristic cycle cost of one operator execution (deterministic)."""
    out_elems = max(_prod(out_dims), 1)

    if op == "Identity":
        return IDENTITY_CYCLES
    if op == "Constant":
        return 0
    if op == "Conv":
        # MACs = out_elems * (Cin/group) * kernel_elems  (NCHW layout)
        kernel = _attr_ints(node, "kernel_shape") or [1]
        group = (_attr_ints(node, "group") or [1])[0]
        cin = in_dims[0][1] if len(in_dims) > 0 and len(in_dims[0]) > 1 else 1
        macs = out_elems * max(cin // group, 1) * _prod(kernel)
        return 2 * macs // MAC_FLOPS_PER_CYCLE + FIXED_OVERHEAD
    if op == "MatMul":
        # MACs = out_elems * K, K = inner dim of the first input
        k = in_dims[0][-1] if len(in_dims) > 0 and in_dims[0] else 1
        macs = out_elems * k
        return 2 * macs // MAC_FLOPS_PER_CYCLE + FIXED_OVERHEAD
    if op == "Gemm":
        m = out_dims[0] if out_dims else 1
        n_ = out_dims[1] if len(out_dims) > 1 else 1
        k = in_dims[0][-1] if len(in_dims) > 0 and in_dims[0] else 1
        macs = m * n_ * k
        return 2 * macs // MAC_FLOPS_PER_CYCLE + FIXED_OVERHEAD
    if op == "Einsum":
        # no equation parsing in the placeholder: cost as a matmul over the
        # largest input extent (direction-correct for structural deltas)
        k = max((d[-1] for d in in_dims if d), default=1)
        macs = out_elems * k
        return 2 * macs // MAC_FLOPS_PER_CYCLE + FIXED_OVERHEAD
    if op == "Range":
        return out_elems // ELEM_PER_CYCLE + FIXED_OVERHEAD
    if op in ("MaxPool", "AveragePool"):
        kernel = _attr_ints(node, "kernel_shape") or [1]
        macs = out_elems * _prod(kernel)
        return macs // MAC_FLOPS_PER_CYCLE + FIXED_OVERHEAD
    if op in _REDUCE:
        in_elems = max(_prod(in_dims[0]), 1) if len(in_dims) > 0 and in_dims[0] else out_elems
        return in_elems // REDUCE_PER_CYCLE + FIXED_OVERHEAD
    if op in _MOVES:
        return out_elems * 4 // MOVE_BYTES_PER_CYCLE + FIXED_OVERHEAD  # float32 assumed
    if op == "Softmax":
        return out_elems // TRANSCENDENTAL_PER_CYCLE + FIXED_OVERHEAD
    if op == "LayerNormalization":
        return out_elems * 3 // ELEM_PER_CYCLE + FIXED_OVERHEAD
    if op in ("BatchNormalization", "InstanceNormalization"):
        return out_elems // ELEM_PER_CYCLE + FIXED_OVERHEAD
    if op in _TRANSCENDENTAL:
        return out_elems // TRANSCENDENTAL_PER_CYCLE + FIXED_OVERHEAD
    # remaining _ELEMENTWISE + Shape/Size
    return out_elems // ELEM_PER_CYCLE + FIXED_OVERHEAD


def profile(onnx_path: Path, out_dir: Path) -> dict:
    model = onnx.load(str(onnx_path))

    # unsupported check FIRST: an unknown-domain op also breaks shape inference,
    # and the contract requires the unsupported-op diagnosis to win
    unsupported = sorted({node.op_type for node in model.graph.node
                          if node.op_type not in SUPPORTED_OPS})
    if unsupported:
        raise UnsupportedOpsError(unsupported)

    dims_map = _tensor_dims_map(model)

    initializer_names = {init.name for init in model.graph.initializer}
    producer: dict[str, int] = {}
    for idx, node in enumerate(model.graph.node):
        for out in node.output:
            producer[out] = idx

    n = len(model.graph.node)
    names, op_types, task_ids, latencies = [], [], [], []
    deps: list[list[int]] = []
    out_mem: list[int] = []
    out_dims_list: list[list[int]] = []

    for idx, node in enumerate(model.graph.node):
        name = node.name or f"{node.op_type}_{idx}"
        names.append(name)
        op_types.append(node.op_type)
        task_ids.append(f"t{idx:04d}")

        in_dims = [dims_map[i][0] for i in node.input if i in dims_map]
        primary = node.output[0] if node.output else ""
        if primary not in dims_map:
            raise RuntimeError(
                f"static shape unavailable for tensor {primary!r} (node {name!r}) — "
                f"the contract requires statically-shaped exports")
        tdims, elem_type = dims_map[primary]
        out_dims_list.append(list(tdims))
        out_mem.append(_prod(tdims) * _DTYPE_BYTES.get(elem_type, 4))
        latencies.append(int(_cost_cycles(node.op_type, node, list(tdims), in_dims)))
        deps.append(sorted({producer[i] for i in node.input
                            if i in producer and i not in initializer_names}))

    # pipeline stage = topological level (deterministic, data-derived)
    levels = [0] * n
    for idx in range(n):
        levels[idx] = 1 + max((levels[d] for d in deps[idx]), default=0)
    pipelines = [f"p{lvl:03d}" for lvl in levels]

    # list schedule on one machine; priority = longest downstream latency path
    # (critical-path heuristic), deterministic tie-break by operator name.
    succs: list[list[int]] = [[] for _ in range(n)]
    for idx, dep in enumerate(deps):
        for d in dep:
            succs[d].append(idx)
    downstream = [0] * n
    for idx in reversed(range(n)):
        downstream[idx] = latencies[idx] + max((downstream[s] for s in succs[idx]), default=0)

    assignments = []
    end_at = [0] * n
    done = [False] * n
    clock = 0
    for _ in range(n):
        ready = [i for i in range(n) if not done[i] and all(done[d] for d in deps[i])]
        if not ready:
            raise RuntimeError("graph has a dependency cycle — not a DAG")
        pick = min(ready, key=lambda i: (-downstream[i], names[i]))
        start = max(clock, max((end_at[d] for d in deps[pick]), default=0))
        end_at[pick] = start + latencies[pick]
        clock = end_at[pick]
        done[pick] = True
        assignments.append({
            "task_id": task_ids[pick], "operator": names[pick],
            "pipeline": pipelines[pick],
            "start_cycle": int(start), "end_cycle": int(end_at[pick]),
        })
    makespan = max(end_at) if end_at else 0

    _write_artifacts(out_dir, onnx_path, names, op_types, task_ids, latencies,
                     deps, out_mem, out_dims_list, pipelines, assignments,
                     makespan)
    return {"makespan_cycles": makespan, "op_count": n}


def _write_artifacts(out_dir: Path, onnx_path: Path, names, op_types, task_ids,
                     latencies, deps, out_mem, out_dims_list, pipelines,
                     assignments, makespan: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    n = len(names)

    taskgraph = {
        "schema_version": SCHEMA_VERSION,
        "onnx": str(onnx_path.resolve()),
        "operators": [
            {
                "name": names[i], "op_type": op_types[i],
                "task_id": task_ids[i], "pipeline": pipelines[i],
                "latency": latencies[i],
                "depends_on": [names[d] for d in deps[i]],
                "output_memory": out_mem[i],
                "output_dimensions": out_dims_list[i],
                "onnx_nodes": [names[i]],
            }
            for i in range(n)
        ],
    }
    (out_dir / "taskgraph.json").write_text(
        json.dumps(taskgraph, indent=2), encoding="utf-8")

    with open(out_dir / "ops.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "op_type", "task_id", "pipeline", "latency",
                    "depends_on", "output_memory", "output_dimensions", "onnx_nodes"])
        for i in range(n):
            w.writerow([
                names[i], op_types[i], task_ids[i], pipelines[i], latencies[i],
                ";".join(names[d] for d in deps[i]),
                out_mem[i],
                "x".join(str(d) for d in out_dims_list[i]),
                ";".join([names[i]]),
            ])

    schedule = {"schema_version": SCHEMA_VERSION, "makespan_cycles": makespan,
                "assignments": assignments}
    (out_dir / "schedule.json").write_text(
        json.dumps(schedule, indent=2), encoding="utf-8")

    summary = {"schema_version": SCHEMA_VERSION, "onnx": str(onnx_path.resolve()),
               "makespan_cycles": makespan, "op_count": n}
    (out_dir / "profile_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--onnx", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--seed", type=int, default=0,
                    help="accepted for CLI compatibility; output is deterministic")
    ns = ap.parse_args()
    try:
        result = profile(Path(ns.onnx), Path(ns.out_dir))
    except UnsupportedOpsError as exc:
        for op in exc.op_types:
            print(f"unsupported: {op}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — fail loud with a clear cause
        print(f"placeholder_profiler: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"profile_dir": str(Path(ns.out_dir).resolve()),
                      **result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
