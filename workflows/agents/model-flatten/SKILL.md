---
name: model-flatten
description: Flatten any PyTorch model entry point into a validated KD-NAS variant contract (build_model + DUMMY_INPUT + KNOBS). LLM does flattening + knob identification (judgment); scripts do hard validation (deterministic).
---

# Model Flatten Skill

Use this skill to take an arbitrary PyTorch model entry point (any `.py` / `.yaml` / config
entry) from the user project and flatten it into a **KD-NAS variant contract** —— a single
standalone `.py` file exposing `DUMMY_INPUT` + `BUILD_FN="build_model"` + `KNOBS` +
`def build_model(**cfg) -> nn.Module`. The output is consumed verbatim by the downstream
`setup` / `gate` / `train` nodes.

This skill is the entry point of the kd-nas workflow: it flattens an arbitrary
PyTorch model entry into a KD-NAS variant contract (DUMMY_INPUT + BUILD_FN +
KNOBS + build_model). The LLM does flattening + KNOBS identification
(judgment); scripts do hard validation (deterministic).

Skill resource paths:

- `<skill_dir>`: The directory containing this `SKILL.md`. All `scripts/` paths are relative
  to `<skill_dir>`. Resolved by the agent entry (agent.md) to `$ORCA_AGENT_RESOURCES`.

## Lazy Loading

Do **not** read all project files upfront. Only read the materials that a specific step
requires **when you begin that step**. This keeps context focused.

## Working Directory and Path Conventions

- `<output_dir>`: `${PROJECT_ROOT}/artifacts/models/baseline/` —— cross-run persistent
  and **co-rooted with the downstream `setup` node's `kd_artifacts_dir`**. The
  baseline contract travels with the project across runs. `PROJECT_ROOT` is the **low-confidence
  suffix-stripped** value inferred in preparation; `agent.md` step 3 computes `<output_dir>` with
  a **deterministic python snippet** (`split(' (low-confidence')[0]` + `os.path.abspath`)
  **verbatim-aligned with `kd-setup/agent.md`** so both nodes derive the same root (deterministic
  logic in code, not prose). `mkdir -p <output_dir>` before writing. **Run `cd <output_dir>`
  once before executing any command**; the working directory persists. All artifacts are written
  under `<output_dir>`.
- `<user_project_root>`: The root of the user project (contains the model entry file).
  Inferred once in preparation (see agent.md), not a workflow input.
- Use `pathlib.Path` for path construction; never string concatenation.

## Workflow

Follow these 6 steps in order. Use the `todowrite` tool to track progress.

### Step 1: Collect Task Context

Read the user request and only the project files under `<user_project_root>` needed to
understand:

- Model entry point (the file with the model class or factory). Resolve ambiguous entries
  (e.g., `config.yaml` → the `.py` it references) by reading the referenced file.
- Real input I/O shape: batch size, channel count, sequence length, spatial dims. Mark
  unconfirmed dims as `Unknown` and ask the user (via stdout sentinel) before guessing.
- Training/inference flow, loss, metrics (briefly — only to inform KNOBS identification
  in Step 5; KD-NAS does not retrain the baseline).

Do not read NAS-specific reference files —— out of scope.

### Step 2: Flatten Local Dependencies

Starting from the model entry point:

1. Keep standard-library and third-party imports (`torch`, `torch.nn`, `numpy`, etc.) as
   imports at the top of the file.
2. **Inline only local project code required for `build_model` to run**, recursively
   resolving nested local imports and ordering definitions to avoid `NameError`.
3. Drop unrelated helpers (training loops, data loaders, logging setup) —— they bloat the
   contract and may pull unwanted deps.
4. Preserve the model constructor's real arguments (e.g., real `num_classes`,
   `in_channels`, `num_blocks`) —— Step 5 will turn the structural ones into KNOBS.

### Step 3: Add Runnable Test Block (Correctness + Latency) + Ensure Device Portability

1. **Append `if __name__ == "__main__":` block** to the flattened file. The block does
   **two** jobs (unified contract: "run `__main__` = correctness + latency"):

   - **Correctness** (as before): instantiate the model with `KNOBS` defaults, create a
     dummy input tensor whose shape matches the user's real I/O (Step 1), run a forward
     pass, print readable shape info (`CORRECTNESS: OK | input=... output=...`).
   - **Latency**: measure default-cfg latency via the `measure_latency` helper. The helper
     is located via the `$ORCA_AGENT_RESOURCES` env var (injected by the flatten runtime =
     this skill's directory): `<resources>/scripts/measure_latency.py`. The block imports
     it dynamically inside `__main__` (so the contract top-level stays standalone —
     `validate_contract.py` and downstream `import build_model` are unaffected).

   Use inline device detection (do **not** depend on `_kd_scripts` or `nas_agent`):

   ```python
   import torch
   _device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
   ```

   **`__main__` block template** (adapt variable names to the model; the
   `<LATENCY_PROVIDER_DEFAULT>` placeholder MUST be replaced with the rendered value of
   `{{ inputs.latency_provider }}` — see Step 4):

   ```python
   if __name__ == "__main__":
       import argparse
       import os
       import sys

       import torch

       _device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
       _defaults = {k: v["default"] for k, v in KNOBS.items()}
       _model = build_model(**_defaults).to(_device).eval()
       _shape = list(DUMMY_INPUT["shape"])
       _dtype = getattr(torch, DUMMY_INPUT.get("dtype", "float32"))
       _dummy = torch.randn(*_shape, dtype=_dtype, device=_device)
       with torch.no_grad():
           _out = _model(_dummy)
       _out_shape = list(_out.shape)
       print(f"CORRECTNESS: OK | input={_shape} output={_out_shape}")
       # 显式 emit output shape 供 flatten agent 解析写进 OUTPUT_SHAPE（可选字段）
       import json as _json
       print(f"OUTPUT_SHAPE_OBSERVED: {_json.dumps(_out_shape)}")

       # latency 测量（默认 cfg）：跑 __main__ = 正确性 + latency（统一契约）
       # --latency_provider 默认值 = flatten 时用户给的 inputs.latency_provider（可 CLI 覆盖）
       _ap = argparse.ArgumentParser(add_help=False)
       _ap.add_argument(
           "--latency_provider",
           default="<LATENCY_PROVIDER_DEFAULT>",  # Step 4 替换为渲染后的 inputs.latency_provider
       )
       _ap.add_argument("--device", default="auto")
       _ap.add_argument("--seed", type=int, default=0)
       _ap.add_argument("--repeats", type=int, default=3)
       _ap.add_argument("--opset", type=int, default=17)
       _args, _ = _ap.parse_known_args()

       _resources = os.environ.get("ORCA_AGENT_RESOURCES", "")
       _helper = (
           os.path.join(_resources, "scripts", "measure_latency.py")
           if _resources
           else ""
       )
       if _helper and os.path.isfile(_helper):
           sys.path.insert(0, os.path.dirname(_helper))
           from measure_latency import measure_contract_latency  # noqa: E402

           _r = measure_contract_latency(
               contract_path=__file__,
               latency_provider=_args.latency_provider,
               device=_args.device,
               seed=_args.seed,
               opset=_args.opset,
               repeats=_args.repeats,
           )
           print(f"LATENCY_US: {_r['latency_us_median']:.6f}")
           print(f"LATENCY_STD: {_r['latency_us_std']:.6f}")
           print(f"LATENCY_SOURCE: {_r['source']}")
           print(f"LATENCY_CONFIDENCE: {_r['confidence']}")
       else:
           # helper 未找到（非 orca 编排上下文）：不伪造，明确告知。
           print(
               "LATENCY_SKIPPED: measure_latency helper 未找到"
               "（set ORCA_AGENT_RESOURCES 指向 model-flatten skill 目录可启用 latency 测量）"
           )
   ```

   **Important**: the `--latency_provider` default is the **rendered** value of
   `{{ inputs.latency_provider }}` (e.g. `/abs/latency_provider.py::measure`), NOT a Jinja
   template string. An empty `inputs.latency_provider` → default `""` → helper falls back
   to ONNXRT-CPU + WARN (flatten is a general agent; kd-nas workflow makes it required).

2. **Review every `nn.Module` class for device portability** before saving. Tensors stored
   as plain Python attributes (not via `register_buffer` or `nn.Parameter`) will not follow
   `.to(device)` and cause runtime device-mismatch errors. Convert them to
   `register_buffer` / `nn.Parameter` as appropriate. Tensors created dynamically in
   `forward()` must be placed on the model's device (use `param.device` or pass device
   through).

### Step 4: Infer `<base_name>` and Write the Flat File

1. **Infer `<base_name>`** from semantic model type / architecture / project context when
   possible (e.g., `<user_model_name>`); otherwise use the primary model class
   name converted to snake_case. The `<base_name>` becomes the `variant_id` of the
   baseline contract.
2. Write `<output_dir>/<base_name>_flat.py`. **The file MUST expose, at module top-level:**

   ```python
   DUMMY_INPUT = {"shape": [<non-empty list>], "dtype": "float32"}  # input shape (user's real input dims)
   OUTPUT_SHAPE = [<non-empty int list>]                            # forward output shape (captured in Step 3)
   BUILD_FN = "build_model"
   KNOBS = {                                                  # ≥1 knob; step<0, leverage∈{high,medium,low}
       "<knob_a>": {"default": <num>, "min": <num>, "step": <-num>, "leverage": "high"},
       ...
   }
   def build_model(**cfg) -> nn.Module: ...                   # zero-arg uses KNOBS defaults; cfg overrides
   ```

   These five symbols are the KD variant contract verbatim. Downstream
   `kd_common.validate_variant` / `tune_latency.py` import them directly.

   `OUTPUT_SHAPE` is the model's forward output shape captured from the Step 3 correctness
   run (the `output=...` list printed by `CORRECTNESS: OK | input=... output=...`).
   - **Same-shape family** (autoencoder / receiver): `OUTPUT_SHAPE == DUMMY_INPUT["shape"]`.
   - **Classifier family** (output ≠ input, e.g. `[1,1,28,28]→[1,10]`): `OUTPUT_SHAPE` is the
     real output (`[1,10]`). KD does not require output==input; teacher/student must share
     output shape (the KD loss compares them) — declaring it here lets `validate_contract.py`
     check 7 hard-verify, and lets teacher-gen / gen-student copy it verbatim.

3. **Review and self-validate**: re-read the flat file and verify (a) definitions ordered
   correctly (no `NameError`), (b) constructor args consistent with how `build_model`
   forwards them, (c) `forward()` logically correct, (d) no unintended mutations from
   inlining or device-portability fixes, (e) the `--latency_provider` default in `__main__`
   is the **rendered** `{{ inputs.latency_provider }}` value (not a Jinja template string;
   empty input → `""`). Then run `python <base_name>_flat.py` —— the `__main__` block must
   complete without error and emit both `CORRECTNESS: OK` and `LATENCY_US: <number>`
   (or `LATENCY_SKIPPED` if run outside the orca context).

### Step 5: KNOBS Identification (LLM Judgment —— core difficulty)

This step is the **only LLM-judgment-heavy part**; the rest is mechanical flattening.
KNOBS drives downstream `tune_latency.py` shrink behavior, so bad knobs = broken latency
gate.

For each candidate tunable dimension in the flattened model, decide:

| Aspect | Guidance |
|---|---|
| **Which dims are knobs?** | Structural parameters that change compute/latency when scaled: block count (`num_blocks`), embedding/channel dim (`embed_dim`, `channels`), layer depth (`num_layers`), head count (`num_heads`), expansion ratio (`expansion`). **Not** knobs: tensor shapes fixed by I/O (batch, seq_len), optimizer hyperparams (lr), dropout rate (no latency impact). |
| **`default`** | The value `build_model` is currently instantiated with (the as-shipped architecture). Integer preferred. |
| **`min`** | Structural floor: the smallest value that still produces a valid forward (no shape mismatch, no `RuntimeError`). For `num_blocks`: 1. For `embed_dim`: a round number that keeps layer shapes integral (often 8 or 16). **Never** set `min` so low it breaks the forward (Step 6 hard-validation will catch this — but aim to get it right here). |
| **`step`** | **Must be negative** (shrink direction; downstream `kd_common.validate_variant` rejects `step>=0`). Magnitude = single-shrink delta: `-1` for block/layer counts, `-4` or `-8` for channel dims (keep even/round), `-2` for head count. |
| **`leverage`** | Impact rank on latency/compute when shrunk: `high` = near-linear compute scaling (block count, layer depth); `medium` = quadratic-but-tunable (embed_dim, channels); `low` = mild (head count with fixed embed_dim, expansion ratio). `tune_latency.py` shrinks high-leverage knobs first. |

Write the `KNOBS` dict at module top-level (above `build_model`). Each knob MUST have all
four fields. Re-run `python <base_name>_flat.py` after editing to confirm `build_model`
still works with the chosen defaults.

If the model genuinely has no tunable structural dims (rare —— e.g., a single hardcoded
linear layer), emit a single best-effort knob and document it as a low-leverage candidate
in Step 6's verifier output. An empty `KNOBS={}` is a contract violation
(`validate_contract.py` fails loud).

### Step 6: Hard Validation + `flatten-verifier` Iteration

This step has two complementary layers (user-decided design: deterministic script +
LLM reviewer):

#### 6a. 执行：Script Hard Validation (deterministic, fail loud)

Run the contract validator (emits `KEY: value` lines, exit 0 = PASS, exit 2 = FAIL):

```bash
python3 "$ORCA_AGENT_RESOURCES/scripts/validate_contract.py" \
  --contract "<output_dir>/<base_name>_flat.py" \
  --device "auto" --seed 0
```

`validate_contract.py` checks: import success, `BUILD_FN == "build_model"`, callable
`build_model`, `DUMMY_INPUT.shape` non-empty list, `KNOBS` non-empty dict with every field
present and valid (`step<0`, `leverage∈{high,medium,low}`, `default/min` numeric),
`build_model(**defaults)` instantiates, and forward output shape is **deterministic**
(same input twice → same output shape) — and, if the optional `OUTPUT_SHAPE` is declared,
matches the declared list. It no longer requires `output == DUMMY_INPUT["shape"]`
(KD does not require same-shape I/O; classifier families with output ≠ input are valid).

If exit code != 0 → read `FAIL_REASON:` line, fix the flat file, re-run. **Do not proceed
to 6b until the script PASSes** —— the LLM verifier cannot catch import / shape errors.

#### 6b. `flatten-verifier` Subagent Iteration (LLM judgment, scaffold)

Invoke the `flatten-verifier` subagent with the prompt framework below. The verifier
checks two things scripts cannot: **flatten fidelity** (no logic silently dropped /
mutated during inlining) and **KNOBS coverage** (do the knobs capture the model's main
tunable dims, with sane `min` / `step` / `leverage`?).

**`flatten-verifier` invocation prompt (scaffold —— adapt per model):**

```
You are the flatten-verifier subagent. Review the flattened contract for KD-NAS.

Inputs:
- User project root: <user_project_root>
- Original model entry (user-supplied): <baseline_model_path>
- Flattened contract: <output_dir>/<base_name>_flat.py
- Validator status: validate_contract.py PASS (already enforced; do not re-run)

Check three dimensions and report issues with severity tags:

1. Flatten fidelity (severity [BLOCKER] if violated):
   - Every nn.Module class used by build_model is present in the flat file (no local
     import silently dropped).
   - forward() computation matches the original entry point's logic (no mutated math,
     no reordered ops, no missing normalization/activation).
   - Constructor arguments of build_model match the real model (no fabricated defaults,
     no dropped kwargs).

2. KNOBS coverage (severity [MAJOR] / [MINOR]):
   - Every structural tunable dim in the model is represented as a knob (missing high-
     leverage dims like num_blocks / embed_dim → [MAJOR]).
   - `min` is a true structural floor (would `build_model` with this min still forward?
     If unsure, flag [MAJOR]).
   - `step` magnitude is sane for the dim type (-1 for counts, -4/-8 for channels;
     absurdly large steps → [MINOR]).
   - `leverage` rank roughly matches compute scaling (block count = high, embed_dim =
     medium, etc.; misranks → [MINOR]).

3. Latency `__main__` wiring (severity [BLOCKER] if violated):
   - The `__main__` block measures latency via `measure_contract_latency` (not just
     correctness). Missing latency step → [BLOCKER] ("跑 `__main__` = 正确性 + latency"
     is the unified contract).
   - **When `{{ inputs.latency_provider }}` is non-empty**: the `--latency_provider`
     default in `__main__` MUST be the rendered value of `inputs.latency_provider`
     (e.g. `/abs/path.py::measure`), NOT an empty string. Giving a latency_provider in
     the input but leaving the default empty (→ ONNXRT-CPU fallback) violates the
     "latency 必用用户脚本" 铁律 → [BLOCKER].
   - The default is a **rendered path string**, not a Jinja template (`{{ ... }}`
     literally in the .py → [BLOCKER]).

Output format:
- If all checks pass: emit exactly `PASS` (or `LGTM`) and nothing else.
- Otherwise: one issue per line, format:
  `[<SEVERITY>] <symptom> | reason: <reason> | fix: <guidance>`

Prioritize [BLOCKER] > [MAJOR] > [MINOR] in your review.
```

**Iteration loop:**

1. Spawn `flatten-verifier` with the prompt above (use the `task` tool / subagent
   invocation mechanism provided by the runtime).
2. If output is `PASS` / `LGTM`: Step 6 complete; proceed to agent.md output JSON.
3. If output contains issues: apply targeted fixes (highest severity first), re-run
   `validate_contract.py` (6a) to confirm script still PASSes, then re-spawn verifier.
4. **Cap iterations at 3** —— if still not PASS after 3 rounds, emit the flat file as-is
   + add the verifier's most recent `[BLOCKER]` / `[MAJOR]` issue to the agent.md final
   JSON `model_name` field as a suffix ` (low-confidence: <one-line issue>)`. Do not hide
   the unresolved issue.

## Validation Summary

- Step 1-5 each complete only when the `__main__` block of `<base_name>_flat.py` runs
  successfully (no import / shape / dtype / device / runtime errors). The `__main__`
  block must emit `CORRECTNESS: OK` AND `LATENCY_US: <number>` (the unified "run `__main__`
  = correctness + latency" contract; `LATENCY_SKIPPED` only when run outside the orca
  context without `ORCA_AGENT_RESOURCES`).
- Step 6a (`validate_contract.py`) is the deterministic gate for contract format; 6b is
  judgment-driven (flatten fidelity + KNOBS + latency `__main__` wiring).
- The final artifact is `<output_dir>/<base_name>_flat.py` —— agent.md reads its path
  into the `baseline_contract_path` output field and its `LATENCY_US:` into
  `baseline_latency_us`.

## Guidelines

- Preserve all generated artifacts unless the user explicitly asks for cleanup.
- Keep the flat file free of `ModuleNotFoundError` for local project code (all local deps
  inlined; only stdlib + pip-installable third-party remain as imports).
- Variable / function / class / string-literal / comment names in English.
- Conservative `min` / `step` choices when model structure is uncertain —— Step 6 will
  catch over-aggressive values.
