# mnist_kd_adversarial — adversarial test fixture for the kd-nas fidelity audit

This is a **test fixture**, not a normal project. It exists to verify that
the kd-nas fidelity audit catches a semantic deviation that the
deterministic Layer-3 check (`fidelity_check.py`) is known to miss. Do
**not** use it as a template for real projects — use `examples/mnist_kd/`
for that.

## What's here

- `train.py`, `eval.py`, `model.py`, `latency_provider.py`,
  `requirements.txt` — copied verbatim from `examples/mnist_kd/`. These are
  the "user original" inputs to kd-nas.
- `user/{loss,data,eval,optim}.py` — hand-authored leaves that mimic what
  `kd-train-script` would normally generate. Three of them (`loss.py`,
  `data.py`, `eval.py`) are faithful ports. **`optim.py` is not.**

## The planted deviation

`train.py::build_optimizer` constructs:

```python
torch.optim.Adam(params, lr=lr)          # weight_decay defaults to 0
```

The planted leaf `user/optim.py::build_optimizer` constructs:

```python
torch.optim.Adam(params, lr=lr, weight_decay=1e-3)
```

- **Same optimizer class name** (`Adam`) → the deterministic L3 check
  `OPT_TYPE_OK` only compares the class name, so it **PASSES**.
- **Different `weight_decay` kwarg** → the semantic fidelity audit
  (`project-fidelity-verifier-kd`) compares optimizer kwargs, not just the
  class name, and is expected to flag this as a `Static Fidelity` finding.

## Expected behavior when consumed by the audit

1. `fidelity_check.py` (L3) prints `OPT_TYPE_OK: true` and `FIDELITY: PASS`.
2. `project-fidelity-verifier-kd` (L4-semantic) flags the `weight_decay`
   drift as a Static Fidelity finding and returns a non-`all-pass` report.
3. The L4-semantic convergence loop in `kd-train-script` either closes the
   finding by fixing the leaf (restoring `weight_decay=0`) within
   `MAX_TURNS = 3`, or fails loud with an ask-user sentinel.

## Why a fixture with hand-authored leaves

The point is to make the L3-blind / B1-caught gap **CI-reproducible**: a
fixed on-disk leaf directory whose only deviation is exactly the one the
semantic audit is supposed to catch. Running the audit against this fixture
must produce the same verdict every time.

## Running the audit manually

```bash
# L3 should PASS
python workflows/agents/kd-train-script/scripts/fidelity_check.py \
  --leaves_dir examples/mnist_kd_adversarial/user \
  --user_train examples/mnist_kd_adversarial/train.py \
  --dummy_input '<DUMMY_INPUT dict from baseline contract>' \
  --model_path <baseline_contract.py> --build_fn build_model --build_cfg '{}' \
  --accuracy_baseline_kind acc \
  --project_root examples/mnist_kd_adversarial

# Then drive the L4-semantic loop (first-run spawn) per
# workflows/agents/kd-train-script/SKILL.md.
```
