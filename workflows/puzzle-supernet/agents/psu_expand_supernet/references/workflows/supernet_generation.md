# Supernet Generation Workflow

This workflow generates `<output_dir>/supernet.py` — a single executable file containing the choice-only NAS supernet (`SearchSpace`, `ArchConfig`, `SuperNet`, and all branch modules) derived from the user's prepared model and its pretrained checkpoint.

**Inputs:**

- **`<prepared_model>`**: the flattened or optimized model file (e.g., `<base_name>_flat.py` or `<base_name>_llm-optimized.py`) that serves as the reference architecture for supernet construction.
- **`model_type`**: the classified architecture label (`transformer_layer`) that determines which model-family spec to use.
- **`{{ inputs.pretrained_ckpt }}`**: the pretrained original model checkpoint — the single weight source for branch inheritance and the equivalence reference.
- **`load_pretrained.py`**: the flatten node's deterministic loader beside `<prepared_model>` (exposes `build_pretrained_model()` / `build_probe_inputs()`). The generated supernet inherits weights through the `state_dict` it produces; never load the checkpoint with ad-hoc parsing of your own.

## Procedure

### 1. Read Specifications and the Variant Snapshot

Read `references/supernet_specs/general_specs.md` — it contains the core supernet constraints, the branch adapter contract, the `SuperNet` API (weight inheritance + freeze groups), and output requirements. It directs you to the model-family reference `references/supernet_specs/transformer_layer/spec.md` and the canonical `transformer_layer/search_space.py`.

Read the slot facts from `<prepared_model>` (and the manifest for the input spec): residual stream width, `num_heads`, `head_dim`, `ffn_dim`, `max_seq_len`, `activation` — all as measured values, uniform across the stack. Missing or non-uniform facts mean the model should have been classified unsupported; stop and report instead of guessing.

Read the variant implementations:

- `nas_agent/blocks/choice_layer.py` — `ChoiceLayer`
- `$ORCA_AGENT_RESOURCES/assets/layer_variants/transformer_layer_variants.py` — the variant layer factories (`make_vanilla_layer`, `make_random_synthesizer_layer`, `make_relu_attention_layer`, `make_fnet_layer`, `make_softs_star_layer`) and their support classes. The generated `supernet.py` **embeds** the needed code from this snapshot (it never imports across directories).

Read `nas_agent/blocks/primitive_blocks.py` only if the original model's non-slot fixed components need an elastic primitive — normally they are plain `nn` modules copied from the original model.

### 2. Analyze User Model and Generate `supernet.py`

Analyze `<prepared_model>` and determine the supernet boundary per the "Full-Model Scope & Component Boundary" rules in `general_specs.md`. Use this boundary to guide code generation.

When porting non-searchable logic (iterative/fixed-point loops, `self.training` branches, gradient boundaries, runtime weight manipulation, solution/state initialization) from `<prepared_model>` into `SuperNet` and the exported subnet class, also apply the "Non-Searchable Model Logic" rules in `general_specs.md`: method completeness, semantic equivalence, attribute preservation, structural reference consistency, and `SuperNet`/subnet forward consistency.

Then follow `general_specs.md` and `transformer_layer/spec.md` to:

1. Build the `original` branch: a deep copy of the original model's layer for each slot, wrapped per the Branch Adapter Contract.
2. Build the variant branches: embed the factories from the variant snapshot, constructed with the measured slot facts (never defaults or guesses).
3. Wire weight inheritance (complete key mapping, fail loud on unmatched keys) and freeze groups (`original` + fixed modules frozen, variants trainable), per the `SuperNet.__init__` contract.
4. Generate `<output_dir>/supernet.py` as a complete, executable single file per the Output Content Requirements in `general_specs.md`, including the module-level `build_supernet(pretrained_state=None)` helper.

### 3. Validate

Run validation commands from inside `<output_dir>`:

**Diagnostic check** (does not modify files — catches undefined names and missing imports):

```bash
ruff check --no-fix --config <nas_agent_root>/nas_agent/internal_ruff_check.toml supernet.py
```

If diagnostic errors are reported, fix the code and re-run the diagnostic check until it passes.

**Runtime validation**:

```bash
python supernet.py
```

If `python supernet.py` fails, inspect the error, fix the code, and re-validate from the diagnostic check.

After `python supernet.py` passes, run these additional checks once:

1. **Multi-sample consistency**: sample 3–5 different `ArchConfig`s, run `set_sample_config` + `forward` + `get_active_subnet` + subnet `forward` on each, and verify supernet vs subnet output consistency for every sample.
2. **`elastic_num_params` sanity**: check that `elastic_num_params` returns different values for paths whose slots resolve to different branches (branch families have different parameter counts).
3. **Buffer registration / device portability**: registered buffers and parameters live in `model._buffers` / `model._parameters`, not in `vars(model)`, so any `torch.Tensor` found as a plain attribute is a device-portability bug. Iterate over every submodule (`model.named_modules()`) and assert no tensor remains in `vars(mod)`; any that does must be moved to `register_buffer` / `nn.Parameter` (or stored as a Python scalar).

**Equivalence gate** (the deterministic check the node's Validation step runs — run it once here to catch problems early):

```bash
python3 "$ORCA_AGENT_RESOURCES/scripts/check_equivalence.py" --artifacts-dir "$ORCA_ARTIFACTS_DIR"
```

It builds the pretrained original model via `load_pretrained.py`, sets the supernet to the all-original path, and asserts the materialized-key contract, tensor-by-tensor forward equivalence, and freeze groups; it writes `.equivalence.json` (pass or fail). On failure, fix `supernet.py` (weight mapping, freeze groups, or choice wiring) and re-run.

If any check fails, inspect the failure, fix `supernet.py`, re-run from the diagnostic check, re-run `python supernet.py`, and re-run the additional checks.

**Format cleanup** (run once after all validation checks pass):

```bash
ruff check --fix --quiet --config <nas_agent_root>/nas_agent/internal_ruff.toml supernet.py
ruff format --quiet --config <nas_agent_root>/nas_agent/internal_ruff.toml supernet.py
```

Treat the format cleanup as silent final formatting only. Do not surface Ruff's format-only output, and do not use formatting-only output as a reason for additional manual edits.
