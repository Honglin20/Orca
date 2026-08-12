# Checklist: Puzzle Expand (pz_expand)

Companion to: `workflows/puzzle.yaml` node `pz_expand`

## How To Use

Each item below is a verifiable requirement extracted from the puzzle workflow's pz_expand contract. Verify items in order. For items marked `auto-fixable: yes`, fix the artifact directly. For items marked `auto-fixable: no`, report the issue for the caller.

## Items

### [CRITICAL] 1. block_map.json Exists And Has ≥1 Slot
**auto-fixable**: no
**Section**: Workflow / Step 2 (measure_baseline.py)
**Check**: `block_map.json` exists under `$ORCA_ARTIFACTS_DIR` and parses as JSON with at least one slot entry. An empty slot list means the model has no replaceable attention/ffn sub-block → `model_type_supported=false` → `terminate_unsupported`. The workflow-verifier only audits artifacts that exist; if the file is absent the caller's pz_expand run already failed loud.
**Verify**: Read `block_map.json`; confirm it is a JSON object with a `slots` array containing ≥1 slot. Each slot must carry `{layer_idx, kind, in_dim, out_dim, parent_module_path, ...}` (`kind` ∈ {attention, ffn, conv, moe, custom}).
**Anti-pattern**: empty `[]` slot list emitted without `model_type_supported=false`; slot missing `kind` or `layer_idx`; legacy `slot_type` field instead of `kind`.

### [CRITICAL] 2. Flat Model Is Importable And Self-Contained
**auto-fixable**: yes
**Section**: Workflow / Step 1 (LLM flatten)
**Check**: `<base>_flat.py` exists and is importable in isolation (standard library / third-party imports preserved, local dependencies inlined). A flat model that fails to import blocks every downstream node (pz_build_library loads it as the BLD teacher). It must expose a zero-arg `build_model() -> nn.Module` and a `DUMMY_INPUT = {"shape": [...], "dtype": "float32"}` declaration.
**Verify**: Run `python -m py_compile <base>_flat.py`; then `python <base>_flat.py` (the `__main__` block must forward a dummy tensor and print the output shape without raising).
**Anti-pattern**: `ModuleNotFoundError` for a local project module that should have been inlined; hardcoded absolute project path in import; multi-input `forward` not wrapped to single-input; missing `build_model` / `DUMMY_INPUT`.
**Fix**: inline the missing local import / wrap multi-input forward via a single-input packing facade / add `build_model` + `DUMMY_INPUT`.

### [CRITICAL] 3. baseline_metrics.json Has acc + latency + smokes_passed
**auto-fixable**: no
**Section**: Workflow / Step 2 (measure_baseline.py)
**Check**: `baseline_metrics.json` exists and contains `baseline_acc` + `baseline_latency` + `latency_unit` + `smokes_passed`. The 4 fidelity smokes (strict-load / forward-determinism / per-slot-identity-allclose / eval-stability) are the engineering gate; `smokes_passed` lists all four on success. These are the AC anchors for `pz_report` (|acc_delta| ≤ tolerance, latency_ratio ≤ 0.5).
**Verify**: Read `baseline_metrics.json`; confirm `baseline_acc` and `baseline_latency` numeric; `latency_unit` matches `inputs.latency_unit`; `smokes_passed` is the 4-element list.
**Anti-pattern**: only acc recorded (LAT AC has no baseline); latency recorded without unit; any smoke missing from `smokes_passed`.

### [CRITICAL] 4. search_space.yaml Valid And Slots Carry kind_evidence
**auto-fixable**: no
**Section**: Workflow / Step 1 (LLM slot identification)
**Check**: `search_space.yaml` parses as YAML with a `slots` list and a `candidates` mapping. Each slot carries `id` / `path` / `kind` / `layer_idx` / `kind_evidence` (deterministic evidence string supporting the kind). attention kind must cite QK^T scaling; ffn kind must cite Linear→Act→Linear. `candidates` is `{kind: [...]}` with `identity` in every kind list.
**Verify**: Read `search_space.yaml`; for each slot confirm `kind_evidence` is non-empty and consistent with `kind`; confirm `identity` ∈ each candidates list.
**Anti-pattern**: slot without `kind_evidence` (kind claim unverified); attention slot whose evidence does not mention matmul/scaling; ffn slot without `activation` / `ffn_struct`; candidates list missing `identity`.

### [CRITICAL] 5. manifest.yaml Has All 5 Sections And eval_kind Matches
**auto-fixable**: yes
**Section**: Workflow / Pipeline Memory
**Check**: `manifest.yaml` parses as YAML with the 5 top-level keys: `project_overview` / `model` / `training_and_evaluation` / `data_and_environment` / `relevant_source_files`. `training_and_evaluation.eval_kind` must equal `inputs.eval_kind`. The `training_and_evaluation.evaluation_entry`, `training_and_evaluation.pretrained_ckpt`, and `model.build_entry` are the discovered bridges measure_baseline consumes.
**Verify**: `python3 -c "import yaml; d=yaml.safe_load(open('manifest.yaml')); assert all(k in d for k in ['project_overview','model','training_and_evaluation','data_and_environment','relevant_source_files'])"`.
**Anti-pattern**: section collapsed/missing; `eval_kind` conflicts with `inputs.eval_kind`; `evaluation_entry` / `pretrained_ckpt` / `build_entry` blank.
**Fix**: append the missing section / reconcile `eval_kind` / fill the discovered entry fields.

### [MAJOR] 6. pathlib Used For Paths
**auto-fixable**: yes
**Section**: Path handling contract (puzzle workflow)
**Check**: `<base>_flat.py` and any pz_expand-generated `.py` artifact must construct paths with `pathlib.Path` or `os.path.*`, not raw string concatenation.
**Verify**: grep for `+ "`, `+ '/'`, f-string path patterns in generated `.py` files.
**Anti-pattern**: `path = d + "/file.py"`.
**Fix**: rewrite as `Path(d) / "file.py"`.

### [MAJOR] 7. project_manifest.md Has All 5 Sections
**auto-fixable**: yes
**Section**: Workflow / Pipeline Memory (human-readable companion to manifest.yaml)
**Check**: `project_manifest.md` contains all five section headings: **Project Overview**, **Model**, **Training And Evaluation**, **Data And Environment**, **Relevant Source Files**.
**Verify**: grep for each `## ` heading; any missing → add empty-body section.
**Anti-pattern**: sections collapsed into one prose block; missing **Training And Evaluation** (downstream fidelity-verifier depends on it).
**Fix**: append the missing `## <Section Name>` heading with a placeholder body.
