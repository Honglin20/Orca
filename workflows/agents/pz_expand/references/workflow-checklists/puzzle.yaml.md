# Checklist: Puzzle Expand (pz_expand)

Companion to: `workflows/puzzle.yaml` node `pz_expand`

## How To Use

Each item below is a verifiable requirement extracted from the puzzle workflow's pz_expand contract. Verify items in order. For items marked `auto-fixable: yes`, fix the artifact directly. For items marked `auto-fixable: no`, report the issue for the caller.

## Items

### [CRITICAL] 1. block_map.json Exists And Has ≥1 Slot
**auto-fixable**: no
**Section**: Workflow / Step 1 (expand_model.py)
**Check**: `block_map.json` exists under `$ORCA_ARTIFACTS_DIR` and parses as JSON with at least one slot entry. An empty slot list means the model has no replaceable attention/ffn sub-block → `model_type_supported=false` → `terminate_unsupported`. The workflow-verifier only audits artifacts that exist; if the file is absent the caller's pz_expand run already failed loud.
**Verify**: Read `block_map.json`; confirm it is a JSON object/array containing ≥1 slot. Each slot must carry `{layer_idx, slot_type, in_dim, out_dim, ...}` (slot_type ∈ {attention, ffn}).
**Anti-pattern**: empty `[]` slot list emitted without `model_type_supported=false`; slot missing `slot_type` or `layer_idx`.

### [CRITICAL] 2. Flat Model Is Importable
**auto-fixable**: yes
**Section**: Workflow / Step 1 (expand_model.py)
**Check**: `<base>_flat.py` exists and is importable in isolation (standard library / third-party imports preserved, local dependencies inlined). A flat model that fails to import blocks every downstream node (pz_build_library loads it as the BLD teacher).
**Verify**: Run `python -m py_compile <base>_flat.py`; then `python -c "import importlib.util, sys; spec=importlib.util.spec_from_file_location('flat', '<path>'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)"` — must not raise.
**Anti-pattern**: `ModuleNotFoundError` for a local project module that should have been inlined; hardcoded absolute project path in import.
**Fix**: inline the missing local import / rewrite the import as a sibling plain import.

### [CRITICAL] 3. baseline_metrics.json Has acc + latency
**auto-fixable**: no
**Section**: Workflow / Step 1 (expand_model.py)
**Check**: `baseline_metrics.json` exists and contains both an accuracy-like field and a latency field (names may vary but both must be present), plus the `latency_unit`. These are the AC anchors for `pz_report` (|acc_delta| ≤ tolerance, latency_ratio ≤ 0.5).
**Verify**: Read `baseline_metrics.json`; confirm keys for accuracy (e.g. `acc` / `accuracy`) and latency (e.g. `latency_ms` / `latency`) both present and numeric. `latency_unit` field must match `inputs.latency_unit`.
**Anti-pattern**: only acc recorded (LAT AC has no baseline); latency recorded without unit.

### [MAJOR] 4. pathlib Used For Paths
**auto-fixable**: yes
**Section**: Path handling contract (puzzle workflow)
**Check**: `<base>_flat.py` and any pz_expand-generated `.py` artifact must construct paths with `pathlib.Path` or `os.path.*`, not raw string concatenation.
**Verify**: grep for `+ "`, `+ '/'`, f-string path patterns in generated `.py` files.
**Anti-pattern**: `path = d + "/file.py"`.
**Fix**: rewrite as `Path(d) / "file.py"`.

### [MAJOR] 5. project_manifest.md Has All 5 Sections
**auto-fixable**: yes
**Section**: Workflow / Pipeline Memory
**Check**: `project_manifest.md` contains all five section headings: **Project Overview**, **Model**, **Training And Evaluation**, **Data And Environment**, **Relevant Source Files**.
**Verify**: grep for each `## ` heading; any missing → add empty-body section.
**Anti-pattern**: sections collapsed into one prose block; missing **Training And Evaluation** (downstream fidelity-verifier depends on it).
**Fix**: append the missing `## <Section Name>` heading with a placeholder body.

### [MINOR] 6. No String Path Concatenation
**auto-fixable**: yes
**Section**: Path handling contract
**Check**: No string-concatenated paths anywhere in pz_expand-generated artifacts (covers both code and embedded examples).
**Verify**: grep for `os.path.join` misuse / `+ "/"` / `+ "\\"` patterns.
**Anti-pattern**: `dir + os.sep + name`.
**Fix**: replace with `Path` / `os.path.join`.
