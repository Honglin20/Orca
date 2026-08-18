"""Tests for the psu_train_script fixed-KD-paradigm gates: check_train_script.sh + check_launcher.sh.

Fixture-driven: synthesize minimal train_supernet.py / run_train_supernet.sh skeletons in a tmp
ORCA_ARTIFACTS_DIR and assert pass/fail branches end-to-end (real returncode, closest to how
agent.md invokes the gate). Mirrors tests/workflows/test_check_retrain_script.py's _REPO-path +
subprocess style.

Coverage focus: the PSU static gates ×6 (--pretrained_ckpt / freeze grouping / teacher frozen
forward / optimizer trainable-only / full-module save / startup assertions) accept a KD-paradigm
script and reject an old sandwich-with-KD-warmup script.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_AGENT = _REPO / "workflows" / "agents" / "psu_train_script"
_CHECK = _AGENT / "scripts" / "check_train_script.sh"
_LAUNCHER_CHECK = _AGENT / "scripts" / "check_launcher.sh"


def _run(script: Path, artifacts_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(artifacts_dir)
    env["ORCA_AGENT_RESOURCES"] = str(_AGENT)
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)


# Minimal KD-paradigm train_supernet.py: --pretrained_ckpt CLI + teacher built via
# load_pretrained.py + freeze grouping + trainable-only optimizer + startup assertions +
# full-module save via save_checkpoint_ddp + progress.jsonl chart feed.
_GOOD_KD_TRAIN_PY = '''\
import argparse
import json
import random

import torch


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output_dir", default="runs/train")
    parser.add_argument("--eval_interval", type=int, default=1)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--amp", action="store_true")
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--max_grad_norm", type=float, default=1.0)
    parser.add_argument("--pretrained_ckpt", required=True)
    parser.add_argument("--progress-every", type=int, default=50)
    parser.add_argument("--kd_hidden_weight", type=float, default=1.0)
    parser.add_argument("--kd_logits_weight", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=0)
    return parser.parse_args()


def is_distributed():
    return False


def sync_random_seed(device):
    if not is_distributed():
        return random.SystemRandom().randrange(0, 2**31)
    return 0


def sample_choice_path(search_space, rng):
    choices = {}
    for slot_name, branches in search_space.branch_choices.items():
        choices[slot_name] = rng.choice(list(branches))
    return choices


def build_teacher(device):
    from load_pretrained import build_pretrained_model

    teacher = build_pretrained_model(device=device)
    teacher.eval()
    for p in teacher.parameters():
        p.requires_grad_(False)
    return teacher


def startup_assertions(model, teacher):
    # Original-branch inheritance spot-check: original params == teacher (ckpt) params.
    teacher_params = dict(teacher.named_parameters())
    for name, p in list(model.named_parameters())[:4]:
        assert torch.allclose(p.detach().cpu(), teacher_params[name].detach().cpu()), name
    with torch.no_grad():
        teacher(torch.zeros(1, 8))


def main():
    args = parse_args()
    device = torch.device("cpu")
    model = torch.nn.Linear(8, 8)
    for p in model.parameters():
        p.requires_grad_(False)  # illustrative freeze grouping (non-slot modules frozen)
    trainable_params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(trainable_params, lr=args.lr)
    teacher = build_teacher(device)
    startup_assertions(model, teacher)
    arch_rng = random.Random(sync_random_seed(device))
    sample_choice_path({"branch_choices": {}}, arch_rng)
    with torch.no_grad():
        teacher_outputs = teacher(torch.zeros(1, 8))
    student_outputs = model(torch.zeros(1, 8))
    kd_loss = (student_outputs - teacher_outputs).abs().mean()
    kd_loss.backward()
    from nas_agent.train import save_checkpoint_ddp

    save_checkpoint_ddp("runs/train/supernet_latest.pth", model, optimizer=optimizer)
    global_step = 0
    # chart feed: every --progress-every steps + progress-unit end (contract §3(b))
    if global_step % args.progress_every == 0:
        with open("runs/train/progress.jsonl", "a") as f:
            f.write(json.dumps({"step": global_step, "metrics": {"kd_loss": float(kd_loss)}}) + "\\n")


if __name__ == "__main__":
    main()
'''

# Old-paradigm script (pre-PSU): sandwich sampling + KD warmup CLI + supervised criterion +
# optimizer over bare model.parameters(); no teacher / pretrained ckpt / freeze / startup
# assertions. Compiles fine and satisfies the v3-era structural gates — it must be rejected by
# the PSU gates.
_OLD_PARADIGM_TRAIN_PY = '''\
import argparse
import json
import random

import torch


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output_dir", default="runs/train")
    parser.add_argument("--sandwich_n_random", type=int, default=2)
    parser.add_argument("--kd_weight", type=float, default=1.0)
    parser.add_argument("--kd_warmup_start", type=int, default=25)
    parser.add_argument("--kd_warmup_length", type=int, default=25)
    return parser.parse_args()


def is_distributed():
    return False


def sync_random_seed(device):
    if not is_distributed():
        return random.SystemRandom().randrange(0, 2**31)
    return 0


def sample_sandwich_arch_configs(search_space, n_random, rng):
    max_depths = tuple(max(d) for d in search_space.stage_depth_candidates)
    min_depths = tuple(min(d) for d in search_space.stage_depth_candidates)
    return max_depths, min_depths, []


def main():
    args = parse_args()
    model = torch.nn.Linear(8, 8)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
    inputs = torch.zeros(2, 8)
    outputs = model(inputs)
    loss = outputs.sum()
    loss.backward()
    optimizer.step()
    with open("runs/train/progress.jsonl", "a") as f:
        f.write(json.dumps({"step": 1, "metrics": {"loss": float(loss)}}) + "\\n")


if __name__ == "__main__":
    main()
'''

# Minimal compliant launcher: AMP=false + NUM_WORKERS=0 + plain python3 entry +
# PRETRAINED_CKPT wiring.
_GOOD_LAUNCHER = (
    "#!/usr/bin/env bash\n"
    "DATA_DIR=/data\n"
    "OUTPUT_DIR=runs/train\n"
    "PRETRAINED_CKPT=/ckpt/model.pth\n"
    "KD_HIDDEN_WEIGHT=1.0\n"
    "KD_LOGITS_WEIGHT=1.0\n"
    "NUM_WORKERS=0\n"
    "AMP=false   # single-device default\n"
    "python3 train_supernet.py --data_dir \"$DATA_DIR\" --pretrained_ckpt \"$PRETRAINED_CKPT\" \\\n"
    "  --kd_hidden_weight \"$KD_HIDDEN_WEIGHT\" --kd_logits_weight \"$KD_LOGITS_WEIGHT\"\n"
)


class TestScriptSyntax:
    def test_check_script_bash_n(self):
        r = subprocess.run(["bash", "-n", str(_CHECK)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_check_launcher_bash_n(self):
        r = subprocess.run(["bash", "-n", str(_LAUNCHER_CHECK)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


class TestCheckTrainScript:
    def test_missing_train_py_skips(self, tmp_path):
        r = _run(_CHECK, tmp_path)
        assert r.returncode == 0
        assert "SKIP" in r.stdout

    def test_good_kd_fixture_passes(self, tmp_path):
        (tmp_path / "train_supernet.py").write_text(_GOOD_KD_TRAIN_PY, encoding="utf-8")
        (tmp_path / "run_train_supernet.sh").write_text(_GOOD_LAUNCHER, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "PASS: check_train_script" in r.stdout
        assert "PSU KD contract gates OK" in r.stdout

    def test_py_compile_error_fails(self, tmp_path):
        (tmp_path / "train_supernet.py").write_text("def (\n", encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0

    def test_old_sandwich_warmup_fixture_rejected(self, tmp_path):
        (tmp_path / "train_supernet.py").write_text(_OLD_PARADIGM_TRAIN_PY, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0
        # Rejected by the PSU gates specifically (not by py_compile / structural gates).
        assert "--pretrained_ckpt" in r.stdout
        assert "requires_grad_(False)" in r.stdout
        assert "teacher" in r.stdout
        assert "model.parameters()" in r.stdout
        assert "allclose" in r.stdout

    def test_missing_pretrained_ckpt_arg_fails(self, tmp_path):
        bad = _GOOD_KD_TRAIN_PY.replace(
            '    parser.add_argument("--pretrained_ckpt", required=True)\n', ""
        )
        (tmp_path / "train_supernet.py").write_text(bad, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0
        assert "--pretrained_ckpt" in r.stdout

    def test_missing_freeze_grouping_fails(self, tmp_path):
        bad = _GOOD_KD_TRAIN_PY.replace("p.requires_grad_(False)", "p.requires_grad_(True)")
        (tmp_path / "train_supernet.py").write_text(bad, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0
        assert "requires_grad_(False)" in r.stdout

    def test_missing_teacher_frozen_forward_fails(self, tmp_path):
        bad = _GOOD_KD_TRAIN_PY.replace("teacher", "advisor")
        (tmp_path / "train_supernet.py").write_text(bad, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0
        assert "teacher" in r.stdout

    def test_bare_model_parameters_optimizer_fails(self, tmp_path):
        bad = _GOOD_KD_TRAIN_PY.replace(
            "optimizer = torch.optim.AdamW(trainable_params, lr=args.lr)",
            "optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)",
        )
        (tmp_path / "train_supernet.py").write_text(bad, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0
        assert "model.parameters()" in r.stdout

    def test_requires_grad_filtered_state_dict_save_fails(self, tmp_path):
        bad = _GOOD_KD_TRAIN_PY.replace(
            '    save_checkpoint_ddp("runs/train/supernet_latest.pth", model, optimizer=optimizer)',
            "    frozen_sd = {k: v for k, v in model.state_dict().items() if v.requires_grad}\n"
            "    save_checkpoint_ddp(\"runs/train/supernet_latest.pth\", model, optimizer=optimizer)",
        )
        (tmp_path / "train_supernet.py").write_text(bad, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0
        assert "requires_grad" in r.stdout
        assert "state_dict" in r.stdout

    def test_missing_save_checkpoint_ddp_fails(self, tmp_path):
        # Remove BOTH the import and the call — the gate greps for the token anywhere.
        bad = _GOOD_KD_TRAIN_PY.replace(
            "    from nas_agent.train import save_checkpoint_ddp\n\n"
            '    save_checkpoint_ddp("runs/train/supernet_latest.pth", model, optimizer=optimizer)',
            '    torch.save(model.state_dict(), "runs/train/supernet_latest.pth")',
        )
        assert "save_checkpoint_ddp" not in bad  # fixture sanity: token fully removed
        (tmp_path / "train_supernet.py").write_text(bad, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0
        assert "save_checkpoint_ddp" in r.stdout

    def test_missing_startup_allclose_assertion_fails(self, tmp_path):
        bad = _GOOD_KD_TRAIN_PY.replace(
            "        assert torch.allclose(p.detach().cpu(), teacher_params[name].detach().cpu()), name",
            "        assert p.shape == teacher_params[name].shape, name",
        )
        (tmp_path / "train_supernet.py").write_text(bad, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0
        assert "allclose" in r.stdout

    def test_no_fail_loud_carrier_fails(self, tmp_path):
        # allclose comparison kept but assert/raise stripped → no fail-loud carrier (6f).
        bad = _GOOD_KD_TRAIN_PY.replace(
            "        assert torch.allclose(p.detach().cpu(), teacher_params[name].detach().cpu()), name",
            "        ok = torch.allclose(p.detach().cpu(), teacher_params[name].detach().cpu())",
        )
        # fixture sanity: no standalone assert/raise token left (word-boundary, as the gate greps)
        assert not re.search(r"\bassert\b|\braise\b", bad)
        (tmp_path / "train_supernet.py").write_text(bad, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0
        assert "assert / raise" in r.stdout

    def test_state_dict_contract_comment_not_flagged(self, tmp_path):
        # A comment line restating the full-module-save rule must NOT trip the 6e filter check.
        bad = _GOOD_KD_TRAIN_PY.replace(
            "    kd_loss.backward()",
            "    # Filtering the state_dict by requires_grad when saving is forbidden.\n"
            "    kd_loss.backward()",
        )
        (tmp_path / "train_supernet.py").write_text(bad, encoding="utf-8")
        (tmp_path / "run_train_supernet.sh").write_text(_GOOD_LAUNCHER, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_multiline_argparse_pretrained_ckpt_accepted(self, tmp_path):
        # ruff-format style add_argument( with the flag on its own line must pass 6a.
        bad = _GOOD_KD_TRAIN_PY.replace(
            '    parser.add_argument("--pretrained_ckpt", required=True)',
            "    parser.add_argument(\n"
            '        "--pretrained_ckpt",\n'
            "        required=True,\n"
            "    )",
        )
        (tmp_path / "train_supernet.py").write_text(bad, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_progress_jsonl_write_granularity_gate(self, tmp_path):
        """§5b：progress.jsonl 写粒度契约——无 --progress-every 也无 step-取模周期写 → FAIL。

        真实 E2E 事故：按 progress unit（每 epoch 一条）写 feed → 5 epoch 只有 5 个点，
        收敛曲线过稀。契约改为每 N 步（默认 50，可覆盖）+ unit 末必写。
        """
        # 1) per-unit-only 写（无 progress-every / 无取模）→ 拒。
        bad = _GOOD_KD_TRAIN_PY.replace(
            '    parser.add_argument("--progress-every", type=int, default=50)\n', ""
        ).replace(
            "    # chart feed: every --progress-every steps + progress-unit end (contract §3(b))\n"
            "    if global_step % args.progress_every == 0:\n"
            "        with open(\"runs/train/progress.jsonl\", \"a\") as f:\n"
            "            f.write(json.dumps({\"step\": global_step, \"metrics\": {\"kd_loss\": float(kd_loss)}}) + \"\\n\")",
            "    with open(\"runs/train/progress.jsonl\", \"a\") as f:\n"
            "        f.write(json.dumps({\"step\": 1, \"metrics\": {\"kd_loss\": float(kd_loss)}}) + \"\\n\")",
        )
        assert "progress_every" not in bad and "progress-every" not in bad  # fixture sanity
        assert "%" not in bad.split("progress.jsonl", 1)[1]  # 无取模写循环
        (tmp_path / "train_supernet.py").write_text(bad, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0
        assert "progress.jsonl 写粒度" in r.stdout

        # 2) 等价 step-取模周期写（无 CLI arg）→ 收（等价写循环标记）。
        equivalent = _GOOD_KD_TRAIN_PY.replace(
            '    parser.add_argument("--progress-every", type=int, default=50)\n', ""
        ).replace("args.progress_every", "50")
        (tmp_path / "train_supernet.py").write_text(equivalent, encoding="utf-8")
        (tmp_path / "run_train_supernet.sh").write_text(_GOOD_LAUNCHER, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr


class TestCheckLauncher:
    def test_good_passes(self, tmp_path):
        (tmp_path / "run_train_supernet.sh").write_text(_GOOD_LAUNCHER, encoding="utf-8")
        r = _run(_LAUNCHER_CHECK, tmp_path)
        assert r.returncode == 0, r.stdout
        assert "PASS" in r.stdout

    def test_missing_launcher_skips(self, tmp_path):
        r = _run(_LAUNCHER_CHECK, tmp_path)
        assert r.returncode == 0
        assert "SKIP" in r.stdout

    def test_missing_pretrained_ckpt_variable_fails(self, tmp_path):
        bad = _GOOD_LAUNCHER.replace("PRETRAINED_CKPT=/ckpt/model.pth\n", "")
        (tmp_path / "run_train_supernet.sh").write_text(bad, encoding="utf-8")
        r = _run(_LAUNCHER_CHECK, tmp_path)
        assert r.returncode != 0
        assert "PRETRAINED_CKPT" in r.stdout

    def test_missing_pretrained_ckpt_flag_pass_fails(self, tmp_path):
        bad = _GOOD_LAUNCHER.replace('--pretrained_ckpt "$PRETRAINED_CKPT" \\\n', "")
        (tmp_path / "run_train_supernet.sh").write_text(bad, encoding="utf-8")
        r = _run(_LAUNCHER_CHECK, tmp_path)
        assert r.returncode != 0
        assert "--pretrained_ckpt" in r.stdout

    def test_torchrun_present_fails(self, tmp_path):
        bad = _GOOD_LAUNCHER + "torchrun --nproc_per_node=2 train_supernet.py\n"
        (tmp_path / "run_train_supernet.sh").write_text(bad, encoding="utf-8")
        r = _run(_LAUNCHER_CHECK, tmp_path)
        assert r.returncode != 0
        assert "torchrun" in r.stdout

    def test_torchrun_in_comment_allowed(self, tmp_path):
        # Full-line comments (the multi-GPU switch hint) are excluded from the torchrun gate.
        good = _GOOD_LAUNCHER.replace(
            "AMP=false   # single-device default\n",
            "AMP=false   # single-device default\n# torchrun --nproc_per_node=2 train_supernet.py\n",
        )
        (tmp_path / "run_train_supernet.sh").write_text(good, encoding="utf-8")
        r = _run(_LAUNCHER_CHECK, tmp_path)
        assert r.returncode == 0, r.stdout

    def test_amp_not_false_fails(self, tmp_path):
        bad = _GOOD_LAUNCHER.replace("AMP=false   # single-device default", "AMP=true   # x")
        (tmp_path / "run_train_supernet.sh").write_text(bad, encoding="utf-8")
        r = _run(_LAUNCHER_CHECK, tmp_path)
        assert r.returncode != 0
        assert "AMP" in r.stdout

    def test_amp_false_bare_line_passes(self, tmp_path):
        # Bare `AMP=false` at end-of-line (no trailing space/comment) is a valid assignment.
        good = _GOOD_LAUNCHER.replace("AMP=false   # single-device default", "AMP=false")
        (tmp_path / "run_train_supernet.sh").write_text(good, encoding="utf-8")
        r = _run(_LAUNCHER_CHECK, tmp_path)
        assert r.returncode == 0, r.stdout

    def test_num_workers_nonzero_fails(self, tmp_path):
        bad = _GOOD_LAUNCHER.replace("NUM_WORKERS=0", "NUM_WORKERS=4")
        (tmp_path / "run_train_supernet.sh").write_text(bad, encoding="utf-8")
        r = _run(_LAUNCHER_CHECK, tmp_path)
        assert r.returncode != 0

    def test_warmup_zero_no_longer_gated(self, tmp_path):
        # The KD warmup nonzero gate is gone: a launcher with KD_WARMUP_START=0 is judged only
        # by the remaining (single-device + PRETRAINED_CKPT) contract.
        bad = _GOOD_LAUNCHER.replace(
            "AMP=false   # single-device default\n",
            "AMP=false   # single-device default\nKD_WARMUP_START=0\n",
        )
        (tmp_path / "run_train_supernet.sh").write_text(bad, encoding="utf-8")
        r = _run(_LAUNCHER_CHECK, tmp_path)
        assert r.returncode == 0, r.stdout
