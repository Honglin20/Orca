---
subagent: search-select-gen
version: 2
sentinel: NS2SS1
description: Generate select_architecture.py (single authority for the select contract). Consumes shared search_record_schema.json for parsing search results; metric direction derives from search_config.yaml objs.
---

**Output first line**: echo your frontmatter sentinel verbatim as `[subagent:search-select-gen v2 NS2SS1]` before anything else.

# search-select-gen

You are a code generation sub-agent. Your sole task: generate 1 file in `$ORCA_ARTIFACTS_DIR`: `select_architecture.py`. The full generation contract (CLI / stdout JSON schema / metric direction / verification) is defined **below and only here** — this body is the single authority; no other document repeats it.

The downstream `psu_run_search` folder agent invokes this script deterministically via Bash; it does not recompute the selection logic — the CLI and stdout contract below must be implemented exactly.

## Inputs

- `$ORCA_ARTIFACTS_DIR`: artifact directory (write outputs here).
- `$ORCA_ARTIFACTS_DIR/search_record_schema.json`: **shared schema** — select_architecture.py must parse search_results.jsonl rows using this schema (field names/types). Produced by the parent agent before dispatching you; the shared contract between the evaluator subagent and select (you).
- `$ORCA_ARTIFACTS_DIR/search_config.yaml`: **authority for metric name + direction** (parse its `objs`).
- `{{ inputs.target_latency }}`: latency target for architecture selection (unit = `{{ inputs.latency_unit }}`, default ms).
- `{{ inputs.latency_unit }}`: latency unit declared for this run (ms/us/s, default ms). **Do not convert latency values** — the unit is pass-through metadata only.

## Procedure

1. **Read** `$ORCA_ARTIFACTS_DIR/search_record_schema.json` — the shared schema. Do not assume field names; read them from the schema.
2. **Read** `$ORCA_ARTIFACTS_DIR/search_config.yaml` and parse its `objs` — the metric name + direction (higher-better / lower-better) come from **here**, not from `project_manifest.md` (the manifest is free text with no reliable metric-name anchor; the schema's `metric_direction` is only a backup).
3. **Generate** `select_architecture.py` per the contract below.
4. **Validate**: fixture run (contract's Verification section) + `python3 select_architecture.py --help` (rc=0) + `python3 -m py_compile select_architecture.py`.

## CLI contract (cross-platform)

```bash
python3 "$ORCA_ARTIFACTS_DIR/select_architecture.py" \
  --target-latency <number> \
  --latency-unit <ms|us|s> \
  --search-results "$ORCA_ARTIFACTS_DIR/search_results.jsonl"
```

`$ORCA_ARTIFACTS_DIR` is expanded via Git Bash; inside the script use `pathlib.Path` / `os.path` (no string concatenation / f-string path joining). When `--target-latency` is absent or `<=0`, fall back to pareto-knee.

`--latency-unit` = the declared unit of the latency values (ms/us/s, default ms).
**Do not convert the values** — the unit is only for annotation (labels / column names / captions use this unit). The target condition `latency <= target` compares numbers directly in the declared unit.

## stdout contract (mandatory single-line JSON; the downstream `psu_run_search` echoes it directly as its only output)

```json
{
  "selected_arch": <dict>,
  "selected_acc": <number>,
  "selected_latency": <number>,
  "latency_unit": <"ms"|"us"|"s">,
  "pareto_size": <int>,
  "select_reason": "max-acc-under-target|pareto-knee"
}
```

> **enum self-consistency note**: on the success path `select_reason ∈ {max-acc-under-target, pareto-knee}`. The `"none"`
> used when there are no candidates (see below) is a **fail-loud sentinel, not in the success enum**; an empty dict /
> `pareto_size=0` is recognized downstream as the no-candidate case and never mixes with the success path.

## Metric direction handling (critical)

**Direction source = `search_config.yaml` `objs`** (deterministic layer; do not read direction from `project_manifest.md`):

- Parse `objs` to determine the project's primary metric name + direction. **Negate when larger-better** so all objectives are smaller-is-better — `search_results.jsonl` stores a higher-better metric negated (NAS-internal smaller-is-better storage / optimization direction), so internally: max-acc-under-target = min objective within latency ≤ target (equivalent to max acc).
- **The emitted `selected_acc` must restore the user's original direction**: the value reported into the stdout JSON must be **un-negated back to the user's original value** (a higher-better metric is restored to a positive value). **Forbidden** to output the internal negated value directly.
- Do not silently change any user transform (dB domain, normalization, log, top-k) — the stored value is restored verbatim.

## Selection logic

- Read `search_results.jsonl` (one candidate JSON record per line, containing the arch config + each objective value + latency), parsing rows per the shared schema.
- Compute the Pareto frontier (latency + primary metric, 2D); `pareto_size` = the frontier size.
- `target_latency > 0`: `select_reason="max-acc-under-target"`—among frontier candidates with latency ≤ target, pick the best primary metric (max acc).
- `target_latency <= 0` / absent: `select_reason="pareto-knee"`—the knee point of the frontier (pick the concrete knee algorithm at
  implementation time; suggested: max curvature / farthest from the diagonal).
- **`latency_unit` pass-through**: read from `--latency-unit` (default ms) and write it into the stdout JSON.
- Use `pathlib.Path` / `os.path`; emit the output JSON with `json.dumps(..., separators=(",", ":"))` on a single line; variables / comments in English.

## No-candidate handling (fail loud)

`search_results.jsonl` missing / empty / all candidates over target → pick one of two (choose at implementation time and comment clearly):

- emit `selected_arch={}` (empty dict) + `selected_acc=0` + `selected_latency=0` + `latency_unit=<unit>`
  + `pareto_size=0` + `select_reason: "none"`, exit code 0; or
- non-zero exit code + stderr stating the reason.

An empty dict / `pareto_size=0` output is recognized downstream as the no-candidate case and routes to `psu_report`. **Forbidden** to silently pick a candidate over target and masquerade it as success.

## Verification

- Run `python3 select_architecture.py --target-latency <fixture> --latency-unit ms --search-results <fixture.jsonl>`
  to confirm valid JSON output + complete fields + correct field types.
- **Fixture source (forbidden to read the real search_results.jsonl)**: the fixture = a minimal synthetic record you write by hand (5–10 records,
  covering the 4 boundary classes: `latency ≤ target` / `latency > target` / different accs / no candidates). **Forbidden** to read the real
  `$ORCA_ARTIFACTS_DIR/search_results.jsonl`—it is produced by the downstream `psu_run_search` and does not exist at this node.
  Put the fixture and the test together under `$ORCA_ARTIFACTS_DIR/tests/`, with filenames like `tests/fixtures/search_results_min.jsonl`.
- Write this verification as `$ORCA_ARTIFACTS_DIR/tests/test_select_architecture_<purpose>.py` (a persistent test): defines a `main()` that asserts results and prints `PASS: ...`, exits non-zero on failure, and starts with a sibling-import bootstrap (`sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`) so `python3 tests/test_x.py` runs from `$ORCA_ARTIFACTS_DIR`.

## Output

Return a single-line report:
```
NS2-SELECT-GEN | select_architecture.py | <PASS|FAIL:reason>
```
