# Profiler Contract (v1)

External contract for the profile four-piece. The ONE profiling path is
the mfu chain — the `mfu-analyzer` subagent drives the user's in-network
`mfu_benchmark.py` (the deployed `mfu_benchmark.py` is the CLI-shape stand-in
for local runs; the real machine swaps in the real script, same CLI), and
the deterministic `mfu_adapter.py` converter produces these artifacts from
the raw products and MUST satisfy this document exactly. There is no
estimator path and no fallback. Field sets are closed: consumers validate
and fail loud on unknown keys, so do not add keys without versioning
`schema_version`.

## CLI

```
mfu_benchmark.py <onnx> --chip <6613|1951> --precision <INT8|INT16|AMP> \
  --core-num <1|2|4> -o <profile_dir> [--timeout 600]
```

- The raw products land under `<profile_dir>/<onnx_stem>/`; the adapter
  writes the four artifacts below into `<profile_dir>` (created if missing).
- `rc=0` on success; failures fail loud (the analyzer's report discloses
  what happened — there is never a silent fallback to an estimator).
- Full parameter semantics: `mfu_benchmark.py --help` (single source).

## Cost unit: cycles

All latency fields are **integer machine cycles** (latency, makespan_cycles,
start/end cycle, cost_table cycles). The workflow's latency goal is expressed
RELATIVE to the baseline (user input `latency_reduction_min`, unit-free):
the frozen absolute threshold is
`int(baseline.makespan * (1 - ratio)) + 1` (the origin anchor's
`target_cycles`), computed in the same unit. The latency gate is exactly
`variant.makespan <= target_cycles` (inclusive), implemented once in
`check_verdict.py` — so the profiler must NOT rescale cycles between runs
of the same workflow.

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

## Fidelity expectations

The workflow only requires **delta-direction correctness**: for the same
model with one localized structural change (e.g. GELU chain → ReLU), the
measured makespan must drop when the real hardware gets faster and rise when
it gets slower. Absolute numbers, pipeline labels and the internal
scheduling model are the evaluation tool's own. The predicted-vs-actual
ratio is a calibration DISCLOSURE (round analysis), never a gate.
