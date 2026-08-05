"""tests/workflows/test_kd_engine_trainer.py — KDTrainer engine Phase 1 unit tests.

Covers the Phase 1 engine surface introduced in
``workflows/agents/_kd_scripts/kd/{trainer,_leaves,_resume}.py`` + the orphan
entry ``workflows/agents/_kd_scripts/train_pipeline.py``.  Per the plan §5
Phase 1 checklist:

* three modes (teacher / distill / eval) end-to-end on a synthetic tiny model
  (CPU, 2-3 epochs);
* resume: write a ``latest.pt`` then assert ``start_epoch == ckpt.epoch + 1``
  (Q14);
* early-stop: patience triggers break + ``best.pt`` carries the right epoch;
* kind direction coverage (nmse=min, snr=max) + mode/hash mismatch fail loud;
* live-push degrade (mock ``orca.chart`` missing);
* proxy_mse batch<3 graceful + ``.to(device)``;
* scheduler returns None — no crash (M3);
* R1 (§11): ckpt dict has no absolute path fields.

Fixture strategy: minimal leaf implementations written under
``tmp_path/user/`` (4 files: ``loss.py`` / ``data.py`` / ``eval.py`` /
``optim.py``) sharing the **exact signature contract** the skeletons in
``references/templates/leaves/*.py.skel`` declare (AST passes — M2/B10).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import torch
import torch.nn as nn

REPO = Path(__file__).resolve().parents[2]
KD_SCRIPTS_DIR = REPO / "workflows" / "agents" / "_kd_scripts"
if str(KD_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(KD_SCRIPTS_DIR))

from kd import _leaves, _resume
from kd.trainer import KDTrainer, TrainConfig

# Regex anchored on metrics_tail._LOSS_LINE_RE (metrics_tail.py:72-75) — used to
# assert the engine's stdout line literally hits the post-hoc tail scanner.
LOSS_LINE_RE = re.compile(
    r"\[train_pipeline:(?P<mode>teacher|distill)\]\s+epoch=(?P<epoch>\d+)\s+"
    r"(?P<key>loss_avg|kd_loss_avg)=(?P<val>[0-9.eE+-]+)"
)


# ---------------------------------------------------------------------------
# Fixture: minimal leaves + a synthetic model.
# ---------------------------------------------------------------------------
TINY_MODEL_PY = """
import torch
import torch.nn as nn


class TinyModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.fc = nn.Linear(4, 4)

    def forward(self, x):
        return self.fc(x)


def build_model(**cfg):
    return TinyModel()
"""


LOSS_LEAF_PY = """
import torch
import torch.nn.functional as F


def compute_loss(s_out, y):
    # Plain MSE — matches TinyModel's [-1,1]-ish output range.
    return F.mse_loss(s_out, y)
"""


DATA_LEAF_PY = """
import torch


class _ReIterable:
    '''Yields (x, y) batches; re-iterable so each epoch gets a fresh stream.'''

    def __init__(self, n_batches=4, batch_size=4, in_dim=4, seed=0):
        self.n_batches = n_batches
        self.batch_size = batch_size
        self.in_dim = in_dim
        g = torch.Generator().manual_seed(seed)
        # Pre-materialise so every epoch sees the same data (deterministic).
        self._x = torch.randn(n_batches * batch_size, in_dim, generator=g)
        self._y = torch.randn(n_batches * batch_size, in_dim, generator=g)

    def __iter__(self):
        for i in range(0, len(self._x), self.batch_size):
            yield self._x[i:i + self.batch_size], self._y[i:i + self.batch_size]


def build_dataloader(batch_size):
    return _ReIterable(n_batches=4, batch_size=batch_size, in_dim=4, seed=0)
"""


def _eval_leaf_py(kind: str = "nmse") -> str:
    return f"""
import torch


def eval_metric(student, device):
    # Deterministic synthetic metric so direction/early-stop tests are stable.
    x = torch.zeros(8, 4, device=device)
    with torch.no_grad():
        out = student(x)
    # Mean magnitude of the output bias-like shift — a stable scalar that
    # decreases as training reduces output variance.  Reported as ``kind``.
    value = float(out.abs().mean().detach().cpu())
    return value, {kind!r}
"""


OPTIM_LEAF_PY = """
import torch


def build_optimizer(params, lr):
    return torch.optim.SGD(params, lr=lr, momentum=0.0)


def build_scheduler(optimizer, epochs):
    return None  # engine M3 None-guard path must tolerate this.
"""


OPTIM_LEAF_WITH_SCHED_PY = """
import torch


def build_optimizer(params, lr):
    return torch.optim.SGD(params, lr=lr)


def build_scheduler(optimizer, epochs):
    return torch.optim.lr_scheduler.StepLR(optimizer, step_size=1, gamma=0.9)
"""


@pytest.fixture
def leaves_dir(tmp_path: Path) -> Path:
    """Write the 4 minimal leaves + a model.py under ``tmp_path``."""
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "loss.py").write_text(LOSS_LEAF_PY)
    (user / "data.py").write_text(DATA_LEAF_PY)
    (user / "eval.py").write_text(_eval_leaf_py("nmse"))
    (user / "optim.py").write_text(OPTIM_LEAF_PY)
    (tmp_path / "model.py").write_text(TINY_MODEL_PY)
    return tmp_path


@pytest.fixture
def leaves_dir_sched(tmp_path: Path) -> Path:
    """Variant of :func:`leaves_dir` whose optim.py builds a real scheduler."""
    user = tmp_path / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "loss.py").write_text(LOSS_LEAF_PY)
    (user / "data.py").write_text(DATA_LEAF_PY)
    (user / "eval.py").write_text(_eval_leaf_py("nmse"))
    (user / "optim.py").write_text(OPTIM_LEAF_WITH_SCHED_PY)
    (tmp_path / "model.py").write_text(TINY_MODEL_PY)
    return tmp_path


def _basic_cfg(
    tmp_path: Path,
    mode: str,
    *,
    epochs: int = 2,
    eval_every: int = 1,
    early_stop_patience: int = 0,
    metric_kind: str | None = "nmse",
    metric_baseline: float | None = 1.0,
    resume_ckpt: Path | None = None,
    student_ckpt: Path | None = None,
    out_ckpt: Path | None = None,
) -> TrainConfig:
    # eval mode is read-only — no out_ckpt. For teacher/distill, default to a
    # path under tmp_path so the test doesn't have to pass one each time.
    if out_ckpt is None and mode != "eval":
        out = tmp_path / "out.pt"
    else:
        out = out_ckpt
    return TrainConfig(
        mode=mode,
        artifacts_dir=tmp_path,
        model_path=tmp_path / "model.py",
        student_model_path=tmp_path / "model.py",
        build_fn="build_model",
        build_cfg={},
        teacher_cache=None,
        kd_config={"kd_losses": ["mse"], "weights": {"mse": 1.0}},
        student_ckpt=student_ckpt,
        metric_baseline=metric_baseline,
        metric_kind=metric_kind,
        epochs=epochs,
        lr=1e-2,
        batch_size=4,
        device="cpu",
        seed=0,
        variant_id=f"test_{mode}",
        out_ckpt=out,
        resume_ckpt=resume_ckpt,
        eval_every=eval_every,
        early_stop_patience=early_stop_patience,
    )


# ---------------------------------------------------------------------------
# _leaves loader contract
# ---------------------------------------------------------------------------
def test_leaves_load_eager_validates_signatures(tmp_path: Path):
    user = tmp_path / "user"
    user.mkdir()
    (user / "loss.py").write_text("def compute_loss(a, b):\n    return a\n")  # wrong names
    (user / "data.py").write_text(DATA_LEAF_PY)
    (user / "eval.py").write_text(_eval_leaf_py())
    (user / "optim.py").write_text(OPTIM_LEAF_PY)
    with pytest.raises(_leaves.LeafContractError, match="required positional args"):
        _leaves.load(user)


def test_leaves_load_missing_file_raises(tmp_path: Path):
    user = tmp_path / "user"
    user.mkdir()
    (user / "loss.py").write_text(LOSS_LEAF_PY)
    (user / "data.py").write_text(DATA_LEAF_PY)
    (user / "eval.py").write_text(_eval_leaf_py())
    # optim.py missing
    with pytest.raises(FileNotFoundError, match="optim.py"):
        _leaves.load(user)


def test_leaves_self_contained_rejects_sibling_import(tmp_path: Path):
    user = tmp_path / "user"
    user.mkdir()
    (user / "loss.py").write_text(
        "import sibling_helper\n"  # not in whitelist → rejected
        "def compute_loss(s_out, y):\n    return s_out\n"
    )
    (user / "data.py").write_text(DATA_LEAF_PY)
    (user / "eval.py").write_text(_eval_leaf_py())
    (user / "optim.py").write_text(OPTIM_LEAF_PY)
    with pytest.raises(_leaves.LeafContractError, match="not in whitelist"):
        _leaves.load(user)


def test_leaves_lazy_exec_does_not_run_until_called(tmp_path: Path):
    """D9-c — at load() the leaf body is NOT executed; only on first call.

    A leaf whose module body raises must not fire during :func:`_leaves.load`;
    it must fire (wrapped in :class:`LeafExecError`) the first time the
    callable is accessed.  This is the *real* laziness invariant — distinct
    from ``test_leaves_exec_failure_wrapped_in_leaf_exec_error`` which only
    checks the wrapping.
    """
    user = tmp_path / "user"
    user.mkdir()
    (user / "loss.py").write_text(
        "raise RuntimeError('lazy exec violated: loss.py body ran at load')\n"
        "def compute_loss(s_out, y):\n    return s_out\n"
    )
    (user / "data.py").write_text(DATA_LEAF_PY)
    (user / "eval.py").write_text(_eval_leaf_py())
    (user / "optim.py").write_text(OPTIM_LEAF_PY)
    # Load must succeed even though loss.py's module body raises.
    leaves = _leaves.load(user)
    # First property access triggers exec → the raise fires, wrapped.
    with pytest.raises(_leaves.LeafExecError, match="lazy exec violated"):
        leaves.compute_loss(torch.zeros(1), torch.zeros(1))


def test_leaves_exec_failure_wrapped_in_leaf_exec_error(tmp_path: Path):
    """Module-body exec failure (not function-call failure) → LeafExecError wrap (B7).

    Function-call errors propagate naturally (Python already attaches the
    leaf's filename via ``__file__``); LeafExecError is specifically for the
    ``exec_module`` phase.
    """
    user = tmp_path / "user"
    user.mkdir()
    # Module-body raise — fires at exec_module time.
    (user / "loss.py").write_text(
        "raise RuntimeError('boom at module load')\n"
        "def compute_loss(s_out, y):\n    return s_out\n"
    )
    (user / "data.py").write_text(DATA_LEAF_PY)
    (user / "eval.py").write_text(_eval_leaf_py())
    (user / "optim.py").write_text(OPTIM_LEAF_PY)
    leaves = _leaves.load(user)
    with pytest.raises(_leaves.LeafExecError, match="leaf body exec failed"):
        leaves.compute_loss(torch.zeros(1), torch.zeros(1))


# ---------------------------------------------------------------------------
# Teacher mode
# ---------------------------------------------------------------------------
def test_teacher_mode_runs_and_emits_protocol(capsys, leaves_dir: Path):
    cfg = _basic_cfg(leaves_dir, "teacher")
    rc = KDTrainer(cfg).train()
    assert rc == 0
    out = capsys.readouterr().out
    assert "TEACHER_CKPT:" in out
    assert "TASK_LOSS_FINAL:" in out
    matches = LOSS_LINE_RE.findall(out)
    assert matches, f"loss line not found in stdout:\n{out}"
    # All lines must be teacher / loss_avg (B2 — eval mode emits none).
    for mode, _ep, key, _val in matches:
        assert mode == "teacher"
        assert key == "loss_avg"


def test_teacher_ckpt_schema_matches_legacy(leaves_dir: Path):
    cfg = _basic_cfg(leaves_dir, "teacher")
    KDTrainer(cfg).train()
    blob = torch.load(cfg.out_ckpt, map_location="cpu")
    assert set(blob.keys()) == {
        "state_dict", "build_cfg", "variant_id", "epochs", "final_loss", "mode"
    }
    assert blob["mode"] == "teacher"
    assert blob["variant_id"] == "test_teacher"


# ---------------------------------------------------------------------------
# Distill mode (requires teacher_cache.pt)
# ---------------------------------------------------------------------------
def _build_teacher_cache(leaves_dir: Path) -> Path:
    """Train a teacher, then persist a teacher_cache.pt via TeacherCache.save."""
    from kd.wrapper import TeacherCache

    teacher_ckpt = leaves_dir / "teacher.pt"
    cfg = _basic_cfg(leaves_dir, "teacher", out_ckpt=teacher_ckpt)
    KDTrainer(cfg).train()
    teacher_blob = torch.load(teacher_ckpt, map_location="cpu")

    teacher = TeacherCache.build(
        teacher_model_path=str(leaves_dir / "model.py"),
        teacher_state_dict=teacher_blob["state_dict"],
        hook_names=[],  # mse-only KD — no feature hooks needed.
        dummy_input_shape=[1, 4],
        build_fn="build_model",
    )
    cache_path = leaves_dir / "teacher_cache.pt"
    teacher.save(str(cache_path))
    return cache_path


def test_distill_mode_runs_q2_order(capsys, leaves_dir: Path):
    cache = _build_teacher_cache(leaves_dir)
    capsys.readouterr()  # drain teacher-mode stdout from the cache build
    cfg = _basic_cfg(leaves_dir, "distill")
    cfg.teacher_cache = cache
    rc = KDTrainer(cfg).train()
    assert rc == 0
    out = capsys.readouterr().out
    assert "STUDENT_CKPT:" in out
    assert "KD_LOSS_FINAL:" in out
    assert "KD_PROXY_MSE:" in out
    matches = LOSS_LINE_RE.findall(out)
    assert matches, "distill loss line missing"
    for mode, _ep, key, _val in matches:
        assert mode == "distill"
        assert key == "kd_loss_avg"


# ---------------------------------------------------------------------------
# Q2 distill hot-order — prepare MUST run before optimizer construction
# ---------------------------------------------------------------------------
TINY_MODEL_WITH_HOOKS_PY = """
import torch
import torch.nn as nn


class TinyModelWithHooks(nn.Module):
    def __init__(self):
        super().__init__()
        self.fc1 = nn.Linear(4, 8)
        self.fc2 = nn.Linear(8, 4)

    def forward(self, x):
        h = self.fc1(x)
        return self.fc2(h)

    def feature_hook_names(self):
        return ['fc1']


def build_model(**cfg):
    return TinyModelWithHooks()
"""


def _build_teacher_cache_with_hooks(tmp_path: Path) -> Path:
    """Teacher cache backed by a model with feature hooks (so OFD has stages)."""
    from kd.wrapper import TeacherCache

    model_path = tmp_path / "model_with_hooks.py"
    model_path.write_text(TINY_MODEL_WITH_HOOKS_PY)
    # Train the teacher first using the engine itself.
    user = tmp_path / "user"
    user.mkdir(exist_ok=True)
    (user / "loss.py").write_text(LOSS_LEAF_PY)
    (user / "data.py").write_text(DATA_LEAF_PY)
    (user / "eval.py").write_text(_eval_leaf_py())
    (user / "optim.py").write_text(OPTIM_LEAF_PY)
    teacher_ckpt = tmp_path / "teacher_hooks.pt"
    cfg = TrainConfig(
        mode="teacher", artifacts_dir=tmp_path,
        model_path=model_path, build_fn="build_model", build_cfg={},
        epochs=1, lr=1e-2, batch_size=4, device="cpu", seed=0,
        variant_id="teacher_hooks", out_ckpt=teacher_ckpt,
    )
    KDTrainer(cfg).train()
    blob = torch.load(teacher_ckpt, map_location="cpu")
    teacher = TeacherCache.build(
        teacher_model_path=str(model_path),
        teacher_state_dict=blob["state_dict"],
        hook_names=["fc1"],
        dummy_input_shape=[1, 4],
        build_fn="build_model",
    )
    cache_path = tmp_path / "teacher_cache_hooks.pt"
    teacher.save(str(cache_path))
    return cache_path


def test_distill_q2_prepare_before_optimizer(tmp_path: Path, monkeypatch, capsys):
    """Q2 hot-order invariant: ``kd_loss.prepare()`` MUST be called before
    ``leaves.build_optimizer()`` so OFD/FitNets adapter parameters land in
    the optimizer (plan §3.1, R8). Spies on the two calls and asserts order.
    """
    from kd.compose import KDComposite

    cache = _build_teacher_cache_with_hooks(tmp_path)
    capsys.readouterr()

    # Rewrite the model + leaves to the hooks variant.
    (tmp_path / "model.py").write_text(TINY_MODEL_WITH_HOOKS_PY)

    call_log: list[str] = []
    real_prepare = KDComposite.prepare
    real_kd_parameters = KDComposite.kd_parameters

    def spy_prepare(self, s_feats, t_feats):
        call_log.append("prepare")
        return real_prepare(self, s_feats, t_feats)

    def spy_kd_parameters(self):
        call_log.append("kd_parameters")
        return real_kd_parameters(self)

    monkeypatch.setattr(KDComposite, "prepare", spy_prepare)
    monkeypatch.setattr(KDComposite, "kd_parameters", spy_kd_parameters)

    cfg = TrainConfig(
        mode="distill", artifacts_dir=tmp_path,
        student_model_path=tmp_path / "model.py",
        teacher_cache=cache, build_fn="build_model", build_cfg={},
        kd_config={
            "kd_losses": ["mse", "ofd"],
            "weights": {"mse": 1.0, "ofd": 1.0},
        },
        epochs=1, lr=1e-2, batch_size=4, device="cpu", seed=0,
        variant_id="q2_distill",
        out_ckpt=tmp_path / "q2_student.pt",
    )
    rc = KDTrainer(cfg).train()
    assert rc == 0

    # The Q2 invariant — prepare must precede kd_parameters (and hence the
    # optimizer construction that consumes kd_parameters()).
    assert "prepare" in call_log, "prepare was never called"
    assert "kd_parameters" in call_log, "kd_parameters was never called"
    assert call_log.index("prepare") < call_log.index("kd_parameters"), (
        f"Q2 order violated: prepare must run before kd_parameters; got {call_log}"
    )


def test_distill_ckpt_schema_matches_legacy(leaves_dir: Path):
    cache = _build_teacher_cache(leaves_dir)
    cfg = _basic_cfg(leaves_dir, "distill")
    cfg.teacher_cache = cache
    KDTrainer(cfg).train()
    blob = torch.load(cfg.out_ckpt, map_location="cpu")
    assert set(blob.keys()) == {
        "student_state_dict", "variant_id", "student_cfg", "kd_config",
        "epochs", "proxy_mse", "mode",
    }
    assert blob["mode"] == "distill"


def test_distill_eval_mode_emits_no_loss_line(capsys, leaves_dir: Path):
    """B2 / E12 — eval mode must NOT emit the [train_pipeline:<mode>] loss line."""
    cache = _build_teacher_cache(leaves_dir)
    capsys.readouterr()  # drain teacher stdout
    distill_cfg = _basic_cfg(leaves_dir, "distill")
    distill_cfg.teacher_cache = cache
    KDTrainer(distill_cfg).train()
    capsys.readouterr()  # drain distill stdout

    eval_cfg = _basic_cfg(
        leaves_dir, "eval",
        student_ckpt=distill_cfg.out_ckpt,
        out_ckpt=None,  # eval mode is read-only — no ckpt written.
    )
    rc = KDTrainer(eval_cfg).train()
    assert rc == 0
    out = capsys.readouterr().out
    assert "STUDENT_ACCURACY:" in out
    assert "STUDENT_ACCURACY_KIND:" in out
    assert "MET_ACCURACY:" in out
    assert "ACCURACY_CONFIDENCE:" in out
    assert "[train_pipeline:" not in out, "eval mode must not emit loss line"


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------
def test_resume_start_epoch_matches_checkpoint(leaves_dir: Path):
    """Q14 — resume must continue at ``latest.pt.epoch + 1``."""
    cfg = _basic_cfg(leaves_dir, "teacher", epochs=2)
    trainer = KDTrainer(cfg)
    trainer.train()
    # The trainer wrote latest.pt at runs/test_teacher/latest.pt.
    latest = trainer.latest_path
    assert latest.is_file()
    blob = torch.load(latest, map_location="cpu")
    assert blob["epoch"] == 1  # 0-indexed, last epoch of 2

    cfg2 = _basic_cfg(
        leaves_dir, "teacher", epochs=5,
        resume_ckpt=latest,
        out_ckpt=leaves_dir / "teacher_resumed.pt",
    )
    trainer2 = KDTrainer(cfg2)
    start_epoch, best = trainer2._maybe_resume(
        _load_model(leaves_dir),
        torch.optim.SGD(_load_model(leaves_dir).parameters(), lr=1e-3),
        None,
        "teacher",
        _resume.config_hash(cfg2.build_cfg),
        "",
    )
    assert start_epoch == blob["epoch"] + 1


def test_resume_end_to_end_actually_continues(leaves_dir: Path, capsys):
    """F7 — resume must drive the remaining training loop, not just return
    the right ``start_epoch`` number.  Train 2 epochs → resume with epochs=4
    → assert the final ``latest.pt`` epoch == 3 and that exactly 2 fresh
    loss lines were emitted (epochs 2 and 3).
    """
    cfg = _basic_cfg(leaves_dir, "teacher", epochs=2, eval_every=0)
    KDTrainer(cfg).train()
    capsys.readouterr()

    latest = KDTrainer(cfg).latest_path  # same path; trainer is stateless re:cfg
    cfg2 = _basic_cfg(
        leaves_dir, "teacher", epochs=4, eval_every=0,
        resume_ckpt=latest,
        out_ckpt=leaves_dir / "teacher_resumed.pt",
    )
    trainer2 = KDTrainer(cfg2)
    rc = trainer2.train()
    assert rc == 0

    out = capsys.readouterr().out
    new_lines = LOSS_LINE_RE.findall(out)
    # Exactly 2 new epochs (start_epoch=2 → epochs 2, 3).
    assert len(new_lines) == 2, f"expected 2 fresh epochs, got {len(new_lines)}: {out!r}"
    # Epochs emitted must be 2 and 3 (0-indexed).
    epochs_seen = sorted(int(m[1]) for m in new_lines)
    assert epochs_seen == [2, 3], f"unexpected epochs: {epochs_seen}"

    final_blob = torch.load(trainer2.latest_path, map_location="cpu")
    assert final_blob["epoch"] == 3


def test_resume_mode_mismatch_fails_loud(leaves_dir: Path):
    """Q8 — cross-mode resume must fail loud, not silently restore wrong weights."""
    cfg = _basic_cfg(leaves_dir, "teacher", epochs=1)
    trainer = KDTrainer(cfg)
    trainer.train()
    with pytest.raises(_resume.ResumeMismatchError, match="mode"):
        _resume.load_latest(
            trainer.latest_path,
            expected_mode="distill",  # latest.pt is mode=teacher → mismatch.
            expected_build_cfg_hash=_resume.config_hash(cfg.build_cfg),
            expected_kd_config_hash="",
        )


def test_resume_build_cfg_hash_mismatch_fails_loud(leaves_dir: Path):
    cfg = _basic_cfg(leaves_dir, "teacher", epochs=1)
    trainer = KDTrainer(cfg)
    trainer.train()
    with pytest.raises(_resume.ResumeMismatchError, match="build_cfg_hash"):
        _resume.load_latest(
            trainer.latest_path,
            expected_mode="teacher",
            expected_build_cfg_hash=_resume.config_hash({"different": True}),
            expected_kd_config_hash="",
        )


def test_resume_kd_config_hash_mismatch_fails_loud(leaves_dir: Path, capsys):
    """Q8 — distill mode resume must reject a changed kd_config (F4)."""
    cache = _build_teacher_cache(leaves_dir)
    capsys.readouterr()
    cfg = _basic_cfg(leaves_dir, "distill")
    cfg.teacher_cache = cache
    trainer = KDTrainer(cfg)
    trainer.train()
    # latest.pt was written with kd_config = {"kd_losses":["mse"],...}
    # Now pretend the user changed kd_config between runs.
    new_hash = _resume.config_hash({"kd_losses": ["mse", "ofd"], "weights": {}})
    with pytest.raises(_resume.ResumeMismatchError, match="kd_config_hash"):
        _resume.load_latest(
            trainer.latest_path,
            expected_mode="distill",
            expected_build_cfg_hash=_resume.config_hash(cfg.build_cfg),
            expected_kd_config_hash=new_hash,
        )


def test_resume_scheduler_drop_warns_when_no_scheduler(leaves_dir_sched: Path, capsys):
    """B4 — persisted scheduler_state + current run has none → stderr WARN, not crash."""
    cfg = _basic_cfg(leaves_dir_sched, "teacher", epochs=1)
    trainer = KDTrainer(cfg)
    trainer.train()
    blob = torch.load(trainer.latest_path, map_location="cpu")
    assert blob["scheduler_state"] is not None  # setup: scheduler was present

    # Now resume with a no-scheduler optim leaf: drop the scheduler_state loudly.
    leaves_no_sched = leaves_dir_sched / "user" / "optim.py"
    leaves_no_sched.write_text(OPTIM_LEAF_PY)  # build_scheduler returns None

    cfg2 = _basic_cfg(
        leaves_dir_sched, "teacher", epochs=2,
        resume_ckpt=trainer.latest_path,
        out_ckpt=leaves_dir_sched / "r.pt",
    )
    rc = KDTrainer(cfg2).train()
    err = capsys.readouterr().err
    assert rc == 0
    assert "scheduler_state" in err and "WARN" in err.upper()


# ---------------------------------------------------------------------------
# Early stop
# ---------------------------------------------------------------------------
_CONSTANT_EVAL_LEAF_PY = """
def eval_metric(student, device):
    # Constant metric — strict _metric_improved returns False on equal values,
    # so best.epoch stays at 0 forever and early-stop must fire.
    return 1.0, 'nmse'
"""


def test_early_stop_breaks_at_patience(leaves_dir: Path, capsys):
    """D4 — patience epochs without improvement → break + best.pt correct (F5).

    A constant eval metric (strict comparison ⇒ never improves past epoch 0)
    plus patience=2 must stop training well before ``epochs=10``.
    """
    # Override eval.py with a constant-metric leaf for this test only.
    (leaves_dir / "user" / "eval.py").write_text(_CONSTANT_EVAL_LEAF_PY)

    cfg = _basic_cfg(
        leaves_dir, "teacher", epochs=10,
        eval_every=1, early_stop_patience=2,
        metric_kind="nmse", metric_baseline=0.0,
        out_ckpt=leaves_dir / "es.pt",
    )
    trainer = KDTrainer(cfg)
    rc = trainer.train()
    assert rc == 0

    out = capsys.readouterr().out
    n_loss_lines = len(LOSS_LINE_RE.findall(out))
    # Hard intent assertion: training must not have run all 10 epochs.
    assert n_loss_lines < cfg.epochs, (
        f"early-stop did not fire — saw {n_loss_lines} loss lines for "
        f"epochs={cfg.epochs}, patience={cfg.early_stop_patience}"
    )
    # And best.pt must exist (epoch 0 was the unbeatable best).
    assert trainer.best_path.is_file(), "best.pt must be written when early-stop fires"
    best_blob = torch.load(trainer.best_path, map_location="cpu")
    assert best_blob["best_metric"]["epoch"] == 0


# ---------------------------------------------------------------------------
# Kind direction coverage
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind,expected_dir", [
    ("nmse", "min"),
    ("mse", "min"),
    ("ber", "min"),
    ("db", "min"),
    ("snr", "max"),
    ("acc", "max"),
])
def test_metric_improved_covers_all_kinds(kind, expected_dir):
    # Strict comparison — equal values do NOT count as improvement (so a flat
    # metric doesn't ratchet best.epoch every epoch and disable early-stop).
    assert _run_metric_improved(kind, 1.0, 1.0) is False
    if expected_dir == "min":
        assert _run_metric_improved(kind, 0.5, 1.0) is True   # lower = better
        assert _run_metric_improved(kind, 1.5, 1.0) is False
    else:
        assert _run_metric_improved(kind, 1.5, 1.0) is True
        assert _run_metric_improved(kind, 0.5, 1.0) is False


def _run_metric_improved(kind: str, value: float, prev: float) -> bool:
    """Reach into kd.trainer._metric_improved without re-importing."""
    from kd.trainer import _metric_improved
    return _metric_improved(value, kind, {"epoch": 0, "value": prev, "kind": kind})


# ---------------------------------------------------------------------------
# Live push degrade (B5 / Q18)
# ---------------------------------------------------------------------------
def test_live_push_degrades_when_orca_chart_missing(capsys, monkeypatch, leaves_dir: Path):
    """orca.chart not importable → training continues, protocol keys still emit."""
    # Force the lazy import inside _make_live_push to fail.
    monkeypatch.setitem(sys.modules, "orca", None)
    monkeypatch.setitem(sys.modules, "orca.chart", None)
    cfg = _basic_cfg(leaves_dir, "teacher", epochs=1)
    rc = KDTrainer(cfg).train()
    out = capsys.readouterr().out
    assert rc == 0
    assert "TEACHER_CKPT:" in out  # protocol keys still emit (B5)


# ---------------------------------------------------------------------------
# proxy_mse: batch < max_batches graceful + .to(device)
# ---------------------------------------------------------------------------
def test_compute_proxy_mse_handles_fewer_batches():
    """B6 — dataloader with fewer than max_batches=3 batches returns gracefully."""
    from kd.trainer import _compute_proxy_mse

    class _SmallDL:
        def __init__(self):
            self._x = [torch.randn(2, 4) for _ in range(2)]  # only 2 batches

        def __iter__(self):
            for x in self._x:
                yield x, torch.zeros(2, 4)

    class _Wrapper(nn.Module):
        def __init__(self):
            super().__init__()
            self.fc = nn.Linear(4, 4)

        def forward(self, x):
            return self.fc(x), []

    class _Teacher(nn.Module):
        def __init__(self):
            super().__init__()
            self.fc = nn.Linear(4, 4)

        def forward(self, x):
            return self.fc(x), []

    device = torch.device("cpu")
    val = _compute_proxy_mse(_Wrapper(), _Teacher(), _SmallDL(), device, max_batches=3)
    assert val > 0 or val == 0  # finite
    assert torch.isfinite(torch.tensor(val))


def test_compute_proxy_mse_empty_dataloader_fails_loud(tmp_path: Path):
    from kd.trainer import _compute_proxy_mse

    class _Empty:
        def __iter__(self):
            return iter([])

    class _M(nn.Module):
        def forward(self, x):
            return x, []

    with pytest.raises(SystemExit, match="yielded no batch"):
        _compute_proxy_mse(_M(), _M(), _Empty(), torch.device("cpu"))


# ---------------------------------------------------------------------------
# Scheduler None guard (M3)
# ---------------------------------------------------------------------------
def test_scheduler_none_guard_does_not_crash(leaves_dir: Path):
    """leaves.build_scheduler returns None → engine skips sch.step() (M3)."""
    cfg = _basic_cfg(leaves_dir, "teacher", epochs=2)
    rc = KDTrainer(cfg).train()  # optim.py returns None scheduler
    assert rc == 0


def test_scheduler_step_actually_runs_when_present(leaves_dir_sched: Path):
    """Scheduler present — its state_dict is persisted in latest.pt."""
    cfg = _basic_cfg(leaves_dir_sched, "teacher", epochs=2)
    trainer = KDTrainer(cfg)
    rc = trainer.train()
    assert rc == 0
    blob = torch.load(trainer.latest_path, map_location="cpu")
    assert blob["scheduler_state"] is not None


# ---------------------------------------------------------------------------
# R1 — ckpt dict carries no absolute path fields
# ---------------------------------------------------------------------------
def test_latest_pt_has_no_abs_path_fields(leaves_dir: Path):
    """R1 (§11) — only resume-schema keys; no path leakage into .pt payload."""
    cfg = _basic_cfg(leaves_dir, "teacher", epochs=1)
    trainer = KDTrainer(cfg)
    trainer.train()
    blob = torch.load(trainer.latest_path, map_location="cpu")
    assert set(blob.keys()) <= _resume.RESUME_SCHEMA_KEYS, (
        f"unexpected keys in latest.pt: {set(blob.keys()) - _resume.RESUME_SCHEMA_KEYS}"
    )
    # Walk all string values; none should look like an absolute filesystem path.
    _assert_no_path_values(blob, ("state_dict", "optimizer_state", "scheduler_state"))


def _assert_no_path_values(d: dict, skip_keys: tuple[str, ...]) -> None:
    """Recursively assert no string value looks like an absolute path."""
    path_like = re.compile(r"^/|^[A-Za-z]:[\\/]")
    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and path_like.match(v):
                    raise AssertionError(
                        f"absolute-path-looking value leaked into ckpt: key={k!r} val={v!r}"
                    )
                _walk(v)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                _walk(item)
    # Skip tensor-bearing sub-dicts (their keys are layer names, not paths).
    for k, v in d.items():
        if k in skip_keys:
            continue
        _walk(v)


# ---------------------------------------------------------------------------
# Orphan entry: argparse + TrainConfig construction
# ---------------------------------------------------------------------------
def test_train_pipeline_entry_help_works():
    """The orphan entry exposes --help (the test invokes _resolve_cfg directly)."""
    from train_pipeline import parse_args, _resolve_cfg

    args = parse_args([
        "--mode", "teacher",
        "--artifacts_dir", "fake_user_root",
        "--experiment", "t1",
        "--epochs", "5",
        "--lr", "0.01",
    ])
    assert args.mode == "teacher"
    assert args.experiment == "t1"
    cfg = _resolve_cfg(args)
    assert cfg.mode == "teacher"
    assert cfg.epochs == 5
    assert cfg.variant_id == "t1"


def test_train_pipeline_entry_yaml_merge(tmp_path: Path):
    """``--config`` YAML provides defaults; CLI overrides."""
    from train_pipeline import parse_args, _resolve_cfg

    yaml_path = tmp_path / "rc.yaml"
    yaml_path.write_text(
        "epochs: 10\nlr: 0.05\neval_every: 2\nbuild_cfg:\n  foo: 1\n"
    )
    args = parse_args([
        "--mode", "distill",
        "--artifacts_dir", str(tmp_path),
        "--experiment", "r1",
        "--epochs", "3",  # override yaml
        "--config", str(yaml_path),
    ])
    cfg = _resolve_cfg(args)
    assert cfg.epochs == 3            # CLI overrode yaml
    assert cfg.lr == 0.05             # yaml filled in
    assert cfg.eval_every == 2
    assert cfg.build_cfg == {"foo": 1}


def test_train_pipeline_entry_runs_teacher_end_to_end(leaves_dir: Path, capsys):
    """Drive the orphan entry as a subprocess would: build cfg + KDTrainer.train."""
    from train_pipeline import main

    rc = main([
        "--mode", "teacher",
        "--artifacts_dir", str(leaves_dir),
        "--experiment", "from_entry",
        "--model_path", str(leaves_dir / "model.py"),
        "--out_ckpt", str(leaves_dir / "entry_out.pt"),
        "--epochs", "1",
        "--device", "cpu",
        "--build_cfg", "{}",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "TEACHER_CKPT:" in out
    assert LOSS_LINE_RE.search(out) is not None


# ---------------------------------------------------------------------------
# max_batches smoke cap (breaks the per-epoch inner loop after N batches;
# default 0 = unlimited = full-epoch training).  Verified by spying on
# ``compute_loss`` invocations — exactly N calls ⇒ exactly N batches ran.
# ---------------------------------------------------------------------------
def _patch_leaves_load_with_loss_counter(monkeypatch, call_count: dict):
    """Patch ``_leaves.load`` so the loaded leaves' compute_loss increments ``call_count['n']``.

    Used by the max_batches tests to assert the inner loop ran an exact number
    of batches.  We monkeypatch the module-level ``_leaves.load`` so KDTrainer
    — which calls ``_leaves.load(<artifacts_dir>/user)`` — picks up the spy.
    The spy is installed by mutating ``leaves.loss_module['compute_loss']``
    (the backing dict the ``compute_loss`` property reads from — the property
    itself has no setter).
    """
    real_load = _leaves.load

    def spying_load(user_dir):
        leaves = real_load(user_dir)
        # Force lazy exec, then override the function on the underlying module.
        loss_mod = leaves.loss_module._exec()
        real_compute_loss = loss_mod.compute_loss

        def spy(s_out, y):
            call_count["n"] += 1
            return real_compute_loss(s_out, y)

        loss_mod.compute_loss = spy
        return leaves

    monkeypatch.setattr(_leaves, "load", spying_load)


def test_max_batches_caps_teacher_inner_loop(leaves_dir: Path, capsys, monkeypatch):
    """``max_batches=3`` must break the teacher inner loop after exactly 3 batches.

    The default data leaf yields 4 batches/epoch — so capping at 3 must produce
    exactly 3 ``compute_loss`` invocations per epoch.  This is the smoke-cap
    guarantee that lets ``train_script_verify`` run sub-second on real MNIST.
    """
    call_count = {"n": 0}
    _patch_leaves_load_with_loss_counter(monkeypatch, call_count)

    cfg = _basic_cfg(leaves_dir, "teacher", epochs=1, eval_every=0)
    cfg.max_batches = 3
    rc = KDTrainer(cfg).train()
    assert rc == 0
    capsys.readouterr()  # drain
    assert call_count["n"] == 3, (
        f"max_batches=3 should yield exactly 3 compute_loss calls, got {call_count['n']}"
    )


def test_max_batches_zero_runs_full_epoch(leaves_dir: Path, capsys, monkeypatch):
    """``max_batches=0`` (default) must NOT cap — full 4 batches/epoch run.

    This guards the invariant that the smoke flag is opt-in and never silently
    truncates real training (Rule 12: real training must not be silently cut).
    """
    call_count = {"n": 0}
    _patch_leaves_load_with_loss_counter(monkeypatch, call_count)

    cfg = _basic_cfg(leaves_dir, "teacher", epochs=1, eval_every=0)
    assert cfg.max_batches == 0  # default
    rc = KDTrainer(cfg).train()
    assert rc == 0
    capsys.readouterr()
    # Default data leaf = 4 batches/epoch.
    assert call_count["n"] == 4, (
        f"max_batches=0 should yield all 4 batches, got {call_count['n']}"
    )


def test_max_batches_caps_distill_inner_loop(leaves_dir: Path, capsys, monkeypatch):
    """``max_batches=2`` caps the distill inner loop after exactly 2 batches.

    Symmetry with the teacher cap — both inner loops must honor the flag
    (the smoke cap is useless if distill silently ignores it).
    """
    cache = _build_teacher_cache(leaves_dir)
    call_count = {"n": 0}
    _patch_leaves_load_with_loss_counter(monkeypatch, call_count)

    cfg = _basic_cfg(leaves_dir, "distill", epochs=1, eval_every=0)
    cfg.teacher_cache = cache
    cfg.max_batches = 2
    rc = KDTrainer(cfg).train()
    assert rc == 0
    capsys.readouterr()
    # distill calls compute_loss indirectly via kd_loss; we spy on the leaf
    # callable which is closed over by build_kd_loss.  Expect 2 invocations.
    assert call_count["n"] == 2, (
        f"max_batches=2 should yield exactly 2 compute_loss calls, got {call_count['n']}"
    )


def test_train_pipeline_entry_max_batches_flag_parsed():
    """``--max_batches`` CLI flag flows through argparse → TrainConfig.max_batches."""
    from train_pipeline import parse_args, _resolve_cfg

    args = parse_args([
        "--mode", "teacher",
        "--artifacts_dir", "fake_user_root",
        "--experiment", "smoke",
        "--max_batches", "20",
    ])
    assert args.max_batches == 20
    cfg = _resolve_cfg(args)
    assert cfg.max_batches == 20


def test_train_pipeline_entry_max_batches_default_zero():
    """Omitting ``--max_batches`` must default to 0 (unlimited = full training)."""
    from train_pipeline import parse_args, _resolve_cfg

    args = parse_args([
        "--mode", "teacher",
        "--artifacts_dir", "fake_user_root",
        "--experiment", "full",
    ])
    assert args.max_batches is None
    cfg = _resolve_cfg(args)
    assert cfg.max_batches == 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _load_model(leaves_dir: Path) -> nn.Module:
    """Build the TinyModel fresh (used by resume unit tests)."""
    from kd.trainer import _load_model_by_path

    return _load_model_by_path(leaves_dir / "model.py", "build_model", {})
