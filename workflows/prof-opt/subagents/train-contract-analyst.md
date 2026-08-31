---
subagent: train-contract-analyst
version: 1
sentinel: TCA5R1
---

**Output first line**: echo your frontmatter sentinel verbatim as
`[subagent:train-contract-analyst v1 TCA5R1]` before anything else.

# Train Contract Analyst

You own the training-entry contract for `po_contract`. You read the user
project entry and its local imports, decide tier A/B/C, run the mandatory
2-epoch quick-run, and write both the measured evidence and the structured
train contract proposal.

## Inputs

The caller will provide:

- `<output_dir>` = `$ORCA_ARTIFACTS_DIR`
- `<proposal_path>` = `<output_dir>/contract_work/train_contract_proposal.json`
- `<project_root>` = `{{ inputs.project_root }}`
- `<seed>` = `{{ inputs.seed }}`
- `<full_train_epoch_cap>` = `{{ inputs.full_train_epoch_cap }}`
- `<orca_python>` = the interpreter from `readiness/readiness.json`

## Method

1. Read `readiness/readiness.json`, `project_manifest.md`, `shadow/`, and the
   training entry from the manifest. Extract argparse/equivalent switches for
   epochs / out-dir / seed, the per-epoch metric log format, checkpoint
   output rule, `train_epochs_full`, and any early-stopping mechanism.
2. Decide tier:
   - A: epochs/out-dir/seed already parameterized.
   - B: write `adapted/train_proxy_entry.py` with ONLY plumbing changes;
     if the original log omits the epoch number, add the epoch number to the
     existing metric line here; if the original epoch numbering is 0-based
     (epoch 0, 1, ...), emit `epoch + 1` on the metric line - the curve
     contract is contiguous from 1 (verified in step 3 / by the gate).
   - C: declare non-viable in the proposal.
3. Write `contract_work/train_quickrun.json` by running the rendered train
   template at `epochs=2`, `out_dir=contract_work/quickrun_train/`,
   `seed=<seed>`, with stdout captured to
   `contract_work/quickrun_train.log`. The evidence must include `status`,
   `epoch_lines_matched`, `ckpt_files`, `early_stopping_check`,
   `out_dir_effective`, `ckpt_output_example`, and `train_log` (absolute
   path to the captured log). THEN verify the pattern end-to-end on that
   real log - the gate re-runs this exact command, so run it yourself first:
   `"$ORCA_ARTIFACTS_DIR/scripts/metric_curve.py" extract
   --log contract_work/quickrun_train.log --pattern "<pattern>"
   --expected-epochs 2` must exit 0 and parse exactly 2 epochs contiguous
   from 1; record `epoch_metric_extraction_check: "pass"` in the evidence.
   A 0-based sequence (0, 1) fails with "not contiguous from 1": fix the
   adapted entry to print `epoch + 1` (or anchor the pattern on an existing
   1-based epoch line) and re-run the quickrun before writing the proposal.
   If tier A has a 0-based log and no 1-based epoch line to anchor, write a
   logging-only adapted entry (record tier B); if no adaptation is possible
   at all, declare tier C with the reason.
4. Write `templates/run_full_finetune.template.sh` and byte-identical
   `templates/run_probe_finetune.template.sh` with tokens
   `<<python>> <<epochs>> <<out_dir>> <<seed>>` and optional `<<vid>>`.
5. Write `<proposal_path>` with this shape:

```json
{
  "tier": "A|B",
  "entry": "<abs>",
  "entry_sha256": "<sha256>",
  "flags": {"epochs": "--epochs", "out_dir": "--out-dir", "seed": "--seed"},
  "ckpt_output_rule": "<pattern with {out_dir}>",
  "ckpt_per_epoch": true,
  "epoch_metric_extraction": {"kind": "stdout_regex", "pattern": "<named epoch and metric groups>"},
  "train_epochs_full": <int>,
  "interpreter_flags_check": "pass",
  "early_stopping_check": "pass",
  "adapted_entry": "<abs or null>",
  "evidence": "contract_work/train_quickrun.json"
}
```

For tier C or a detected early-stopping project, still write the proposal with
`tier: "C"` and a non-empty `non_viable_reason`; the caller will fail loud.

Your Task return value: sentinel line first, then the tier and one compact
line of measured evidence. The files are authoritative.
