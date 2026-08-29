"""Tests for the ns3_retrain_script hard gates: check_retrain_script.sh + check_launcher.sh.

Fixture-driven: synthesize minimal retrain.py / finetune.py / run_retrain.sh skeletons in a
tmp ORCA_ARTIFACTS_DIR and assert pass/fail branches end-to-end (real returncode, closest to
how agent.md invokes the gate). Mirrors test_check_progress_contract.py's _REPO-path +
subprocess style.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_AGENT = _REPO / "workflows" / "nas-supernet-v3" / "agents" / "ns3_retrain_script"
_CHECK = _AGENT / "scripts" / "check_retrain_script.sh"
_LAUNCHER = _AGENT / "scripts" / "check_launcher.sh"


def _run(script: Path, artifacts_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(artifacts_dir)
    env["ORCA_AGENT_RESOURCES"] = str(_AGENT)
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)


# Minimal compliant retrain.py: is_distributed() guard + guarded sync_random_seed +
# progress.jsonl chart feed via json.dumps.
_GOOD_RETRAIN_PY = (
    "import json\n"
    "\n\n"
    "def is_distributed():\n"
    "    return False\n"
    "\n\n"
    "def sync_random_seed(device):\n"
    "    if not is_distributed():\n"
    "        return 0\n"
    "\n\n"
    "def main():\n"
    '    with open("progress.jsonl", "a") as f:\n'
    '        f.write(json.dumps({"step": 1, "metrics": {"loss": 0.5}}) + "\\n")\n'
    "\n\n"
    'if __name__ == "__main__":\n'
    "    main()\n"
)

# retrain.py WITHOUT an is_distributed() guard but still writing progress.jsonl.
_NO_GUARD_RETRAIN_PY = (
    "import json\n"
    "\n\n"
    "def main():\n"
    '    with open("progress.jsonl", "a") as f:\n'
    '        f.write(json.dumps({"step": 1, "metrics": {"loss": 0.5}}) + "\\n")\n'
    "\n\n"
    'if __name__ == "__main__":\n'
    "    main()\n"
)

# retrain.py with an UNGUARDED DistributedDataParallel (no is_distributed in the 5 lines above it).
_UNGUARDED_DDP_RETRAIN_PY = (
    "import json\n"
    "import torch\n"
    "\n\n"
    "def make_model():\n"
    "    return torch.nn.parallel.DistributedDataParallel(torch.nn.Linear(2, 2))\n"
    "\n\n"
    "def is_distributed():\n"
    "    return False\n"
    "\n\n"
    "def main():\n"
    '    with open("progress.jsonl", "a") as f:\n'
    '        f.write(json.dumps({"step": 1}) + "\\n")\n'
)

# retrain.py with an UNGUARDED sync_random_seed (no is_distributed in the 3 lines after the def).
_UNGUARDED_SEED_RETRAIN_PY = (
    "import json\n"
    "\n\n"
    "def is_distributed():\n"
    "    return False\n"
    "\n\n"
    "def sync_random_seed(device):\n"
    "    seed = 1234\n"
    "    return seed\n"
    "\n\n"
    "def main():\n"
    '    with open("progress.jsonl", "a") as f:\n'
    '        f.write(json.dumps({"step": 1}) + "\\n")\n'
)

# Minimal compliant launcher: AMP=false + NUM_WORKERS=0 + plain python3 entry.
_GOOD_RUN_RETRAIN_SH = (
    "#!/usr/bin/env bash\n"
    "AMP=false              # single-device default\n"
    "NUM_WORKERS=0          # DataLoader Launch Hygiene\n"
    "python3 retrain.py\n"
)


class TestStaticChecks:
    def test_check_script_bash_n(self):
        r = subprocess.run(["bash", "-n", str(_CHECK)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_check_launcher_bash_n(self):
        r = subprocess.run(["bash", "-n", str(_LAUNCHER)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


class TestCheckRetrainScript:
    def test_missing_retrain_py_fails(self, tmp_path):
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0
        assert "retrain.py missing" in r.stdout

    def test_good_passes(self, tmp_path):
        (tmp_path / "retrain.py").write_text(_GOOD_RETRAIN_PY, encoding="utf-8")
        (tmp_path / "run_retrain.sh").write_text(_GOOD_RUN_RETRAIN_SH, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "PASS: check_retrain_script" in r.stdout

    def test_py_compile_error_fails(self, tmp_path):
        (tmp_path / "retrain.py").write_text("def (\n", encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0

    def test_missing_progress_jsonl_fails(self, tmp_path):
        bad = _GOOD_RETRAIN_PY.replace('progress.jsonl', 'nope.jsonl')
        (tmp_path / "retrain.py").write_text(bad, encoding="utf-8")
        (tmp_path / "run_retrain.sh").write_text(_GOOD_RUN_RETRAIN_SH, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0
        assert "progress.jsonl" in r.stdout

    def test_missing_is_distributed_guard_fails(self, tmp_path):
        (tmp_path / "retrain.py").write_text(_NO_GUARD_RETRAIN_PY, encoding="utf-8")
        (tmp_path / "run_retrain.sh").write_text(_GOOD_RUN_RETRAIN_SH, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0
        assert "is_distributed" in r.stdout

    def test_finetune_py_optional_and_passes(self, tmp_path):
        # finetune.py present but trivial (no DDP symbols) — must NOT be forced to have
        # is_distributed(); retrain.py covers the chart feed.
        (tmp_path / "retrain.py").write_text(_GOOD_RETRAIN_PY, encoding="utf-8")
        (tmp_path / "finetune.py").write_text("import json\n", encoding="utf-8")
        (tmp_path / "run_retrain.sh").write_text(_GOOD_RUN_RETRAIN_SH, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_finetune_py_compile_error_fails(self, tmp_path):
        (tmp_path / "retrain.py").write_text(_GOOD_RETRAIN_PY, encoding="utf-8")
        (tmp_path / "finetune.py").write_text("def (\n", encoding="utf-8")
        (tmp_path / "run_retrain.sh").write_text(_GOOD_RUN_RETRAIN_SH, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0

    def test_finetune_py_can_be_charte_writer(self, tmp_path):
        # OR-semantics: finetune.py (not retrain.py) writes progress.jsonl → still passes.
        retrain_no_feed = (
            "def is_distributed():\n"
            "    return False\n"
            "\n\n"
            "def main():\n"
            "    pass\n"
        )
        finetune_feed = (
            "import json\n"
            "\n\n"
            "def main():\n"
            '    with open("progress.jsonl", "a") as f:\n'
            '        f.write(json.dumps({"step": 1}) + "\\n")\n'
        )
        (tmp_path / "retrain.py").write_text(retrain_no_feed, encoding="utf-8")
        (tmp_path / "finetune.py").write_text(finetune_feed, encoding="utf-8")
        (tmp_path / "run_retrain.sh").write_text(_GOOD_RUN_RETRAIN_SH, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr


    def test_unguarded_ddp_fails(self, tmp_path):
        # DistributedDataParallel present but NOT preceded (within 5 lines) by is_distributed.
        (tmp_path / "retrain.py").write_text(_UNGUARDED_DDP_RETRAIN_PY, encoding="utf-8")
        (tmp_path / "run_retrain.sh").write_text(_GOOD_RUN_RETRAIN_SH, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0
        assert "DistributedDataParallel" in r.stdout

    def test_unguarded_sync_random_seed_fails(self, tmp_path):
        # def sync_random_seed present but NOT followed (within 3 lines) by is_distributed.
        (tmp_path / "retrain.py").write_text(_UNGUARDED_SEED_RETRAIN_PY, encoding="utf-8")
        (tmp_path / "run_retrain.sh").write_text(_GOOD_RUN_RETRAIN_SH, encoding="utf-8")
        r = _run(_CHECK, tmp_path)
        assert r.returncode != 0
        assert "sync_random_seed" in r.stdout


class TestCheckLauncher:
    def test_good_passes(self, tmp_path):
        (tmp_path / "run_retrain.sh").write_text(_GOOD_RUN_RETRAIN_SH, encoding="utf-8")
        r = _run(_LAUNCHER, tmp_path)
        assert r.returncode == 0, r.stdout
        assert "PASS" in r.stdout

    def test_missing_launcher_skips(self, tmp_path):
        r = _run(_LAUNCHER, tmp_path)
        assert r.returncode == 0
        assert "SKIP" in r.stdout

    def test_amp_not_false_fails(self, tmp_path):
        bad = _GOOD_RUN_RETRAIN_SH.replace("AMP=false", "AMP=true")
        (tmp_path / "run_retrain.sh").write_text(bad, encoding="utf-8")
        r = _run(_LAUNCHER, tmp_path)
        assert r.returncode != 0
        assert "AMP" in r.stdout

    def test_torchrun_present_fails(self, tmp_path):
        bad = _GOOD_RUN_RETRAIN_SH + "torchrun --nproc_per_node=2 retrain.py\n"
        (tmp_path / "run_retrain.sh").write_text(bad, encoding="utf-8")
        r = _run(_LAUNCHER, tmp_path)
        assert r.returncode != 0
        assert "torchrun" in r.stdout

    def test_num_workers_nonzero_fails(self, tmp_path):
        bad = _GOOD_RUN_RETRAIN_SH.replace("NUM_WORKERS=0", "NUM_WORKERS=4")
        (tmp_path / "run_retrain.sh").write_text(bad, encoding="utf-8")
        r = _run(_LAUNCHER, tmp_path)
        assert r.returncode != 0

    def test_no_python_entry_fails(self, tmp_path):
        bad = "AMP=false   # x\nNUM_WORKERS=0   # x\n"  # no python3 retrain.py call
        (tmp_path / "run_retrain.sh").write_text(bad, encoding="utf-8")
        r = _run(_LAUNCHER, tmp_path)
        assert r.returncode != 0
        assert "retrain.py" in r.stdout
