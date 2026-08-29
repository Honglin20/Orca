# Profiler Contract (v1)

External contract for the profile function. `placeholder_profiler.py` is the
reference implementation; in mfu real-evaluation mode the deterministic
`mfu_adapter.py` converter produces these artifacts from the raw
`mfu_benchmark.py` products and MUST satisfy this document exactly. Field
sets are closed: consumers validate and fail loud on unknown keys, so do not
add keys without versioning `schema_version`.

## CLI

```
<profile_script> --onnx <path> --out-dir <dir> [--seed 0]
```

- Writes the four artifacts below into `<dir>` (created if missing).
- `rc=0` on success. Any unsupported op → `rc!=0` and stderr lists the
  offending `op_type` names (one `unsupported: <op_type>` line each).
  Silently skipping an op is FORBIDDEN — a dropped op corrupts every
  downstream delta comparison.
- `--seed` exists for run-to-run reproducibility hooks; placeholder output is
  fully deterministic without it.

## Cost unit: cycles

All latency fields are **integer machine cycles** (latency, makespan_cycles,
start/end cycle, cost_table cycles). The workflow's latency goal is expressed
RELATIVE to the baseline (user input `latency_reduction_min`, unit-free):
the derived absolute threshold is `baseline.makespan * (1 - ratio)`, computed
in the same unit. The per-variant latency gate uses fixed tuning constants
(`min_improvement_cycles`=100, `min_pred_actual_ratio`=0.5), not user inputs.

Gate convention tied to this unit: a variant passes L0 iff
`base.makespan - variant.makespan >= max(min_improvement_cycles, 1% * base.makespan)`
— so a real profiler must NOT rescale cycles between runs of the same
workflow (a rescaled model silently changes what min_improvement_cycles buys).

## taskgraph.json

```json
{
  "schema_version": 1,
  "onnx": "<absolute path of the profiled model>",
  "operators": [
    {
      "name": "<unique stable operator name>",
      "op_type": "Conv",
      "task_id": "<unique scheduler task id>",
      "pipeline": "<pipeline stage label>",
      "latency": 1204,
      "depends_on": ["<operator.name>"],
      "output_memory": 2048,
      "output_dimensions": [1, 128],
      "onnx_nodes": ["<onnx node name>"]
    }
  ]
}
```

- `latency`: integer cycles, >= 0, single execution of the operator.
- `depends_on`: data-dependency operator names (empty list when none).
- `output_memory`: bytes of the operator's primary output tensor.
- `output_dimensions`: static shape of that tensor (static-shape models only;
  a profiler that needs dynamic axes must resolve them before writing).
- `onnx_nodes`: the onnx node names the operator was derived from (an operator
  may fuse several nodes; a 1:1 profiler lists the single node name).
- `pipeline`: partition label used for the pipeline breakdown in analysis.
  Semantics: operators sharing a label belong to the same pipeline stage; the
  label set is profiler-defined and the analyzer groups by the actual values.

## ops.csv

One row per operator, exactly these columns in this order:

```
name,op_type,task_id,pipeline,latency,depends_on,output_memory,output_dimensions,onnx_nodes
```

- `depends_on` / `onnx_nodes`: `;`-joined (empty string when none).
- `output_dimensions`: `x`-joined (e.g. `1x128`).
- Rows carry no header comment lines; a single header row is present.

## schedule.json

```json
{
  "schema_version": 1,
  "makespan_cycles": 15288,
  "assignments": [
    {"task_id": "t0000", "operator": "<name>", "pipeline": "p000",
     "start_cycle": 0, "end_cycle": 1204}
  ]
}
```

- `assignments` covers every operator in taskgraph.json exactly once.
- `end_cycle - start_cycle == latency` of that operator.
- `makespan_cycles` = max `end_cycle`.

## profile_summary.json

```json
{"schema_version": 1, "onnx": "<abs path>", "makespan_cycles": 15288, "op_count": 87}
```

- `makespan_cycles` must equal the value in schedule.json.
- `op_count` must equal `len(taskgraph.operators)`.

## Fidelity expectations for a replacement profiler

The workflow only requires **delta-direction correctness**: for the same
model with one localized structural change (e.g. GELU chain → ReLU), the
profiler must report a makespan drop when the real hardware gets faster and a
rise when it gets slower. Absolute numbers, pipeline labels and the internal
scheduling model are the profiler's own. Under the placeholder profiler the
`min_pred_actual_ratio` gate is near-tautological (prediction and measurement
share the same heuristic) — that gate is meaningful only with a real profiler.
