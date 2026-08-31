# Checklist: Train Supernet Script — Launcher And Budget

Companion to: `workflows/train_supernet_script_generation.md`

## How To Use

Each item below verifies the generated launcher script (`run_train_supernet.sh`) and budget/hyperparameter coherence. Verify items in order. For items marked `auto-fixable: yes`, fix the artifact directly. For items marked `auto-fixable: no`, report the issue for the caller.

## Items

### [CRITICAL] 1. Budget 3× Scaling
**auto-fixable**: no
**Section**: §6 Optimizer, Scheduler, AMP, And Gradient Clipping
**Check**: The training budget (total epochs or total optimizer steps) is approximately **3×** the user's original single-model training budget. This is the supernet sandwich training convention for convergence.
**Verify**: Compare the training budget in `run_train_supernet.sh` (or `train_supernet.py` defaults) with the original project's budget. The ratio should be approximately 3×.
**Anti-pattern**: Using the original budget without scaling; scaling by some other factor without justification.

### [CRITICAL] 2. Budget-Dependent Hyperparameters Coherent
**auto-fixable**: no
**Section**: §6, Validation (Budget-hyperparameter coherence)
**Check**: When the training budget was scaled (e.g. 3× epochs), all budget-dependent hyperparameters were adjusted accordingly:
- LR scheduler total steps/epochs
- Warmup steps/epochs
- Decay milestones
- Any other budget-coupled parameters
**Verify**: Read scheduler configuration in `train_supernet.py`. Confirm total steps/milestones are scaled proportionally to the training budget, not left at original values.
**Anti-pattern**: 3× epochs but scheduler milestones left at original values; warmup steps unchanged despite longer training.

### [MAJOR] 3. Launcher Editable Variables Complete
**auto-fixable**: no
**Section**: Run Launcher
**Check**: The launcher exposes key training parameters as editable shell variables at the top: `DATA_DIR`, `OUTPUT_DIR`, training budget, `BATCH_SIZE`, `LR`, `NUM_WORKERS`, `EVAL_INTERVAL`, `SEED`, `MAX_GRAD_NORM`, `SANDWICH_N_RANDOM`, `AMP`, `NNODES`, `NPROC_PER_NODE`. When KD is enabled, also: `KD_WEIGHT`, `KD_WARMUP_START`, `KD_WARMUP_LENGTH`.
**Verify**: Read the editable variables section of `run_train_supernet.sh`.
**Anti-pattern**: Hardcoding values in the torchrun command instead of using variables; missing key variables.

### [MAJOR] 4. Launcher Uses `torchrun`
**auto-fixable**: yes
**Section**: §2 Distributed Setup, Run Launcher
**Check**: Launcher uses `torchrun` for distributed launch. Even single-GPU uses `--nproc_per_node=1`. Default is single-node 8-device.
**Verify**: grep for `torchrun` in `run_train_supernet.sh`.
**Anti-pattern**: Using `python -m torch.distributed.launch` (deprecated); direct `python train_supernet.py` without torchrun.
**Fix**: Replace launch command with `torchrun`.

### [MAJOR] 5. Boolean Flag Handling
**auto-fixable**: yes
**Section**: Run Launcher
**Check**: Boolean flags like `--amp` are handled correctly in the launcher: only passed when the shell variable is true, omitted otherwise. Uses `store_true` pattern.
**Verify**: Check that `AMP_FLAG` (or equivalent) is conditionally set and appended.
**Anti-pattern**: Always passing `--amp true` or `--amp false` instead of presence/absence pattern.
**Fix**: Use conditional flag pattern: `AMP_FLAG=""; [ "$AMP" = true ] && AMP_FLAG="--amp"`.

### [CRITICAL] 6. Launcher CLI Flags Match Argparse
**auto-fixable**: yes
**Section**: Run Launcher, Validation (Launcher-script CLI consistency)
**Check**: Every `--flag` in the `torchrun` invocation inside `run_train_supernet.sh` corresponds to an argument that `train_supernet.py` actually accepts. No extra flags, no missing flags.
**Verify**: Extract all `--flag_name` from `run_train_supernet.sh` torchrun block. Extract all `add_argument('--flag_name')` from `train_supernet.py`. Compare the two lists.
**Anti-pattern**: Launcher passes `--learning_rate` but script defines `--lr`; launcher passes a flag the script doesn't define.
**Fix**: Rename the mismatched flags in `run_train_supernet.sh` to match `train_supernet.py` argparse definitions.

### [CRITICAL] 7. Launcher Shell Syntax Valid
**auto-fixable**: yes
**Section**: Run Launcher
**Check**: `run_train_supernet.sh` passes `bash -n` syntax check.
**Verify**: Run `bash -n run_train_supernet.sh` (syntax-only, no execution) to check shell syntax.
**Fix**: Fix shell syntax errors found by `bash -n`.

### [CRITICAL] 8. Launcher Is Executable
**auto-fixable**: yes
**Section**: Run Launcher
**Check**: `run_train_supernet.sh` has executable permission.
**Verify**: Check file permissions.
**Fix**: `chmod +x run_train_supernet.sh`.

