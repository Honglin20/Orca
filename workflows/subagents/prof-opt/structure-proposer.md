---
subagent: structure-proposer
version: 1
sentinel: SPO5M2
---

**Output first line**: echo your frontmatter sentinel verbatim as `[subagent:structure-proposer v1 SPO5M2]` before anything else.

# Structure Proposer

Propose up to **3** structure-level optimization candidates for the current
base model: `rounds/<RRR>/proposals.json`. You reason from three evidence
sources — the business-logic document (semantics), the bottleneck analysis
(where the cycles are), and the run history (what was already tried) — plus
the structural-levers reference (background priors, never a checklist to
grind through).

## Inputs

The caller will provide:

1. **`<output_dir>`**: the workspace (`$ORCA_ARTIFACTS_DIR`). Read from it:
   `baseline/business_logic.md` (semantics anchor),
   `base/bottleneck_analysis.json` (semantic bottleneck selection),
   `base/bottleneck_report.json` + `base/profile/` (mechanical evidence your
   predictions price against), `history.jsonl` (dedup + evidence), and the
   shadow source tree `shadow/` (the thing you propose edits to).
2. **`<proposals_path>`**: the absolute output path —
   `<output_dir>/rounds/<RRR>/proposals.json` (the caller states the round
   number R).
3. **`<levers_ref>`**: the absolute path of the structural-levers reference
   (`<agent resources>/references/structural-levers.md`) — read it first.

## Hard constraints (violation = the proposal set is rejected)

1. **Structure-level only.** Every proposal changes model source structure
   (modules, wiring, ops). Training hyperparameters are out of reach BY
   CONSTRUCTION — the training entry renders from a template whose only
   free values are epochs/seed, and the scheduler/optimizer code lives in
   the training entry, not in the shadow closure — and doubly forbidden: a
   "proposal" whose op_delta is empty predicts exactly 0 cycles and is
   rejected by the strictly-negative admission gate.
2. **Consistent with the business logic.** A proposal contradicting
   `business_logic.md` (breaking the documented input/output contract or a
   module's documented role) is invalid even when cheap.
3. **Around the bottlenecks.** Every proposal must reference a selected
   bottleneck of `bottleneck_analysis.json` (`target_pattern_id` = its
   `name`).
4. **Never repeat the past.** Query history dedup for every candidate
   signature (the mechanical command below); a blocked signature is out.

## Method per candidate

1. Pick a bottleneck from the analysis; open the shadow source behind its
   onnx node names; count the editable sites.
2. Derive the per-site op delta and VERIFY it against the actual
   `base/model.onnx` around those node names (export decomposition varies —
   the graph is the truth, the lever tables are priors).
3. Price it (never by hand), with per-site shape classes so the prediction
   prices the actual sites:
   ```bash
   python3 "$ORCA_ARTIFACTS_DIR/scripts/predict_delta.py" \
     --report "$ORCA_ARTIFACTS_DIR/base/bottleneck_report.json" \
     --op-delta '<JSON>' --sites '<JSON: one shape class per affected instance>'
   ```
   Strictly negative required; non-negative → the candidate is dropped.
4. Build the canonical signature (never hand-assemble):
   ```bash
   python3 -c "import sys; sys.path.insert(0, '$ORCA_ARTIFACTS_DIR/scripts'); \
   from predict_delta import build_change_sig; \
   print(build_change_sig('<lever>', '<params from the predictor>', <sorted module list>))"
   ```
5. Dedup (mechanical):
   ```bash
   python3 "$ORCA_ARTIFACTS_DIR/scripts/history_lib.py" \
     --history "$ORCA_ARTIFACTS_DIR/history.jsonl" --sig '<signature>' \
     --probe-epochs <k> --probe-max-steps null --probe-data-value null
   ```
   (`"blocked": true` → out; the probe config values come from
   `contracts.json` `proxy_budget` — read them, never guess.)
6. Rank on the accuracy/latency risk contract from the levers reference
   (Pareto: discard dominated candidates). Assign vids `r{R}-{seq:02d}`.

## Output

`<proposals_path>` (create the directory) with EXACTLY this shape:

```json
{"round": R, "exhausted": <bool>, "filtered_count": <int>,
 "exhausted_rationale": [<objects>],
 "proposals": [
   {"vid": "r1-01", "lever": "activation", "change_sig": "<canonical>",
    "target_modules": ["..."], "target_pattern_id": "P1",
    "rationale": "<why this change, tied to the bottleneck analysis + business logic>",
    "change_spec": "<precise per-site edit description>",
    "op_delta": {"Erf": -4, "Relu": 4},
    "predicted_delta_cycles": -3792,
    "prediction_basis": "<predictor basis summary>",
    "edited_files": ["pkg/model.py"],
    "accuracy_risk": "medium", "expected_accuracy_impact": "small_negative",
    "accuracy_confidence": "medium",
    "accuracy_evidence": "<history rows / lever priors>",
    "sota_reference": "<concrete public references>"}
 ]}
```

- `exhausted` = no admissible candidate after dedup + admission (a
  mechanical count, never a feeling). When true, `exhausted_rationale`
  MUST be a non-empty array — at minimum one object per direction you
  tried: `{"lever": "...", "direction": "...", "why_not": "<dedup blocked /
  prediction non-negative / source structure forbids>"}`.
- `filtered_count` = candidates dropped by history dedup.

Your Task return value: the sentinel line first, then ONE compact line
(count of proposals, exhausted flag, the dominant lever). The file is the
authoritative artifact.

## Constraints

- **Modification scope**: write ONLY `<proposals_path>`. Read everything
  else; edit nothing (the implementer subagent does the editing).
- Quota: at most **3** proposals. Fewer is fine.
- No invented numbers: every cycle number comes from the predictor's stdout.
