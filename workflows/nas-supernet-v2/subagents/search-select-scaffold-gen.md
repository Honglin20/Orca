---
subagent: search-select-scaffold-gen
version: 1
sentinel: NS2SS1
description: Generate select_architecture.py + AGENTS.md scaffold. Consumes shared search_record_schema.json for parsing search results.
---

**Output first line**: echo your frontmatter sentinel verbatim as `[subagent:search-select-scaffold-gen v1 NS2SS1]` before anything else.

# search-select-scaffold-gen

You are a code generation sub-agent. Your sole task: generate 2 files in `$ORCA_ARTIFACTS_DIR`.

## Inputs

- `$ORCA_ARTIFACTS_DIR`: artifact directory (write outputs here).
- `$ORCA_ARTIFACTS_DIR/search_record_schema.json`: **shared schema** — select_architecture.py must parse search_results.jsonl rows using this schema.
- `$ORCA_ARTIFACTS_DIR/project_manifest.md`: project facts (metric direction for max-acc selection).
- `{{ inputs.target_latency }}`: latency target for architecture selection (unit = `{{ inputs.latency_unit }}`, default ms).
- `{{ inputs.latency_unit }}`: latency unit declared for this run (ms/us/s, default ms). **Do not convert latency values** — pass-through as a label/metadata field only.

## Procedure

**Metric direction handling** (critical): read metric direction from `project_manifest.md` (higher-better
or lower-better). When selecting max-acc under target, the script must respect the direction: for
higher-better metrics, select the highest value; for lower-better, select the lowest. Do **not**
negate or transform the metric — use the raw value with the correct comparison direction.

1. **Read** `$ORCA_ARTIFACTS_DIR/search_record_schema.json` — **shared schema**. Your select_architecture.py must parse search_results.jsonl rows using the field names/types defined here. This schema is produced by the parent agent before dispatching you — it is the shared contract between evaluator (sibling B) and select (you).
2. **Read** `$ORCA_ARTIFACTS_DIR/project_manifest.md` for metric name + direction (higher-better / lower-better).
3. **Generate** `select_architecture.py`:
   - Args: `--target-latency <number>` + `--latency-unit <ms|us|s>` (default ms) + `--search-results <path>`.
   - Strategy: under-target max-acc (latency ≤ target → max metric, same-unit numeric compare); fallback: Pareto knee.
   - stdout: single-line JSON with `selected_arch` (dict), `selected_acc` (number), `selected_latency` (number), `latency_unit` (string ms/us/s), `pareto_size` (int), `select_reason` (enum: max-acc-under-target / pareto-knee / none).
   - Metric direction: read from manifest (higher-better → maximize; lower-better → minimize).
   - **Do NOT convert latency values across units** — the unit is metadata for downstream labels only.
   - No candidate → `select_reason="none"` + null selected_arch.
4. **Generate** `AGENTS.md` scaffold: guidance for downstream retrain agent (how to construct retrain.py from selected_arch + supernet + project training code). Include: arch config structure, checkpoint path convention, fidelity checklist.
5. **Validate**: `python3 select_architecture.py --help` (rc=0) + `python3 -m py_compile select_architecture.py`.

## Schema Authority

`search_record_schema.json` is the **shared contract**. Your parser must match its field names/types exactly. Do not assume field names — read them from the schema.

## Output

Return a single-line report:
```
NS2-SELECT-SCAFFOLD-GEN | select_architecture.py AGENTS.md | <PASS|FAIL:reason>
```
