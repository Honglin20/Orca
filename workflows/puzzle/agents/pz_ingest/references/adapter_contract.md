# adapter_contract.md — Puzzle adapter 13-API + manifest schema + flatten rules

> Authoritative reference for `pz_ingest/agent.md`. The agent reads this file at
> the start of Step 1 and produces `<base>_flat.py` + `puzzle_adapters.py` +
> `manifest.yaml` per the contracts below. Downstream scripts (measure_baseline /
> bld / score / latency_table / build_selected / gkd_retrain / gate_report)
> consume `puzzle_adapters.py` via `--adapters <path>` and `manifest.yaml` via
> `--manifest <path>`.

## 1. `puzzle_adapters.py` — 13-API contract

The agent produces a single `$ORCA_ARTIFACTS_DIR/puzzle_adapters.py` exposing
exactly these 13 symbols (stable signatures; implementation = faithful port of
the user's source). No project-specificity lives in any kernel script — it all
converges here.

```python
def build_model() -> nn.Module: ...                  # zero-arg instantiation (config baked in; network ctor lifted to zero-arg)
FORWARD_CALLING_CONVENTION: str = "positional"        # "positional" | "dict" | "single"
def forward_model(model, batch) -> output:            # call model(...) per convention; handle multi-input / dict batch
def calib_iter(device=None) -> Iterator[batch]: ...   # calibration data (faithful port of user Dataset construction + collate)
def train_iter(device=None) -> Iterator[batch]: ...   # training data (same shape; includes labels)
def extract_labels(batch) -> torch.Tensor | None: ... # extract labels from native batch (unsupervised -> None)
def kd_loss(s_out, t_out, labels=None) -> Tensor: ... # faithful port of user task KD (cosine / KL / MSE / task loss — never hardcoded)
def task_loss(s_out, labels) -> Tensor | None: ...    # hard-label supervision (port user task loss; non-classification -> None)
def evaluate(model) -> float: ...                     # faithful port of user eval protocol (device / retrieval / metric / direction)
METRIC_DIRECTION: str = "higher-better"               # "higher-better" | "lower-better" (judged from user metric semantics)
EVAL_NOISE_ATOL: float = 1e-9                         # eval-stability tolerance (sampling/retrieval eval >= 1e-2; pure deterministic 1e-9)
def load_pretrained(model) -> "_LoadResult": ...      # ckpt load (strip module./_orig_mod./ema./multi-field dict prefixes + train_from_scratch fallback)
DUMMY_INPUT: dict = {"shape": [...], "dtype": "float32"}  # real I/O dims (multi-input uses list of shapes + convention key)
```

### Faithful-port rules (align with `project-porter.md` "faithful mover")

- **Preserve**: user formulas / constants / signs / feature indices / control
  flow / randomness semantics; KD / task loss formulas verbatim.
- **Allowed mechanical adaptation**: rewrite intra-project imports as same-
  level imports; parameterize hardcoded paths; use `resolve_device` or passed
  `device`; strip DDP / rank / barrier preserving computation; lift the network
  constructor to zero-arg `build_model()`.
- **Forbidden**: simplify, approximate, swap a similar utility, drop "looks
  unimportant" items, hardcode `cross_entropy` / `cosine` in place of the
  user's loss, splice/drop multi-input to force single-tensor forward.

### Per-API port notes

- `forward_model(model, batch)` dispatches per `FORWARD_CALLING_CONVENTION`:
  `positional` -> `model(*batch_inputs)` (multi-input preserves the original
  signature order); `dict` -> `model(**batch_dict)`; `single` ->
  `model(batch)`. The batch unpack logic is your port (preserve how the user's
  original forward extracts multi-input / dict keys verbatim).
- `kd_loss` / `task_loss` port the user's actual loss: metric learning -> the
  user's contrastive / similarity loss; classification -> the user's KL / CE;
  regression -> the user's MSE. **Never hardcode**. `task_loss` returns `None`
  for unsupervised tasks.
- `load_pretrained` strips `module.` / `_orig_mod.` / `ema.` / multi-field dict
  prefixes and returns `_LoadResult(missing, unexpected, from_scratch)`. The
  script side no longer hard-asserts double-zero; non-double-zero only WARNs +
  records `ckpt_from_scratch`. Prefix stripping / multi-field dict is the
  adapter's responsibility.
- `EVAL_NOISE_ATOL`: derived from the eval protocol's noise. Sampling /
  retrieval / unseeded paths have std ≈ √(p(1−p)/N) (N≈1000 -> ~1e-2); atol
  must cover that magnitude. Pure-deterministic eval only uses 1e-9.
- `METRIC_DIRECTION`: judged from user metric semantics (accuracy / top-k /
  recall -> higher-better; loss / error / perplexity -> lower-better).
- `DUMMY_INPUT`: multi-input uses
  `{"shapes": [shape1, shape2, ...], "dtype": "float32", "convention": "positional|dict|single"}`
  aligned with `FORWARD_CALLING_CONVENTION`.
- Data paths use absolute paths or `pathlib`-resolved paths relative to
  `project_root` (pathlib iron rule).

### Vectorize loop metrics (important)

`evaluate` must be vectorized when the user's eval is a per-sample Python loop
(e.g. k-NN per-sample `topk + .item` / per-sample forward) and a **semantically
identical** batched form exists (e.g. k=1 k-NN's `cdist + argmin` == loop
`topk(k=1)`). Otherwise BLD / score / GKD / gate will scale from minutes to
hours calling `evaluate` repeatedly. Faithful means semantically identical, not
verbatim preservation of the loop.

## 2. `manifest.yaml` — five-section schema

```yaml
project_overview:
  task_type: image classification | metric learning | regression | ...
  purpose: one-sentence task goal
  entry_points: {train: <...>, eval: <...>}
model:
  location: <model file>
  build_entry: build_model            # zero-arg instantiation fn name in flat.py (passed as --build_fn)
  forward_signature: "forward(self, <...>)"   # user's original forward signature (multi-input verbatim)
  inputs: "[<...>,<...>]"             # real input shape (multi-input list form)
  outputs: "[<...>]"                  # real output shape
  state_dict_schema_note: <prefix explanation, if reparenting>
training_and_evaluation:
  paradigm: <cross-entropy classification | metric learning | MSE regression | ...>
  loss: <user's original loss semantic description>
  metric: {name: <user's metric real name>, direction: higher-better|lower-better}
  epochs: <int>                          # baseline training epochs (discovered from user train code, e.g. Config.NUM_EPOCHS / argparse default)
  adapters_entry: puzzle_adapters.py     # the generated adapter file (consumed via --adapters)
  forward_calling_convention: positional|dict|single   # matches adapters.FORWARD_CALLING_CONVENTION
  eval_noise_atol: <float>               # matches adapters.EVAL_NOISE_ATOL (sampling/retrieval eval >= 1e-2)
  pretrained_ckpt: <path relative to project_root>  # father weights (read via adapters.load_pretrained)
data_and_environment:
  dataset: <name/location>
  preprocessing: <normalization / sampling / packing>
relevant_source_files:
  - {path: <...>, symbol: <...>, purpose: <...>}
```

### Schema notes

- **Removed fields** (do not emit): `evaluation_entry` / `data_loader_entry` /
  `eval_kind` (user-interface semantics live in adapters). `eval_nondeterministic`
  is superseded by `eval_noise_atol` (a numeric magnitude field).
- **Added fields**: `adapters_entry` / `metric.direction` /
  `forward_calling_convention` / `eval_noise_atol`.
- `model.inputs` / `outputs` support multi-input list form.
- `training_and_evaluation.forward_calling_convention` must equal
  `puzzle_adapters.FORWARD_CALLING_CONVENTION`. Same for `eval_noise_atol` /
  `EVAL_NOISE_ATOL` and `metric.direction` / `METRIC_DIRECTION`. Mismatch →
  fail loud (check_ingest.sh greps this consistency).

## 3. `<base>_flat.py` — flatten self-adaptation rules

After reading `{{ inputs.model_path }}`, produce a self-contained
`$ORCA_ARTIFACTS_DIR/<base>_flat.py` (`<base_name>` derived from the semantic
model type / main class name, snake_case). Two common self-adaptations:

### 3.1 Multi-input forward stays unpacked

The flat `forward` **preserves the original signature** — if the original model
is `forward(self, x1, x2, ...)`, the flat keeps that many inputs. **Forbidden**
to splice multi-input into a 1-D vector hack (it breaks forward semantics and
the fidelity smoke cannot catch it). Multi-input batch unpacking is handled by
`puzzle_adapters.forward_model` per `FORWARD_CALLING_CONVENTION`; the flat does
not participate. The flat must expose `build_model() -> nn.Module` (zero-arg).
`DUMMY_INPUT` for multi-input uses
`{"shapes": [shape1, shape2, ...], "dtype": "float32", "convention": "positional|dict|single"}`
(aligned with `FORWARD_CALLING_CONVENTION`).

### 3.2 state_dict prefix alignment

If the pretrained ckpt has bare-model keys (e.g.
`encoder_layer1.self_attn.W.weight`, no `net.` prefix) but
`self.net = OriginalModel()` would add a `net.` prefix → strict-load fails. The
fix (reparenting): mount each top-level child of the original model under the
wrapper by its original name (`for name, mod in original.named_children():
setattr(self, name, mod)`); state_dict keys match the original model exactly.
`module.` / `_orig_mod.` / `ema.` / multi-field dict prefix stripping is handled
by `adapters.load_pretrained`.

### 3.3 Flat required contents

The flat must contain: `build_model()` (zero-arg, returns the wrapper);
`DUMMY_INPUT` (real I/O dimension declaration; multi-input uses shapes list +
convention); `__main__` block (instantiate + forward + print output shape).
Standard library / third-party imports stay as imports; local project code is
inlined.
