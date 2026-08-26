"""Unit tests for pz_ingest/scripts/check_ingest.sh.

The script is the deterministic gate for the project ingest artifacts:
  - flat + adapters + manifest present
  - py_compile flat + adapters
  - manifest five-section schema + bridge fields (adapters_entry / metric.direction
    / forward_calling_convention / eval_noise_atol / model.build_entry)
  - forward-convention consistency (manifest vs adapters.FORWARD_CALLING_CONVENTION
    / METRIC_DIRECTION / EVAL_NOISE_ATOL)
  - flat __main__ runs and prints an output shape

Covered here (Rule 9: each test constructs a violating fixture and asserts the
gate rejects it fail-loud; the happy path passes):
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("yaml")

_REPO = Path(__file__).resolve().parents[2]
_GATE = _REPO / "workflows" / "agents" / "pz_ingest" / "scripts" / "check_ingest.sh"


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------

_FLAT = textwrap.dedent(
    """
    import sys

    DUMMY_INPUT = {"shape": [1, 3], "dtype": "float32"}

    def build_model():
        class M:
            def forward(self, x):
                return x
        return M()

    if __name__ == "__main__":
        print("output shape:", (1, 3))
    """
).strip()

_ADAPTERS = textwrap.dedent(
    """
    FORWARD_CALLING_CONVENTION = "positional"
    METRIC_DIRECTION = "higher-better"
    EVAL_NOISE_ATOL = 1e-9
    DUMMY_INPUT = {"shape": [1, 3], "dtype": "float32"}

    def build_model():
        class M:
            pass
        return M()

    def forward_model(model, batch):
        return model(*batch)

    def calib_iter(device=None):
        return iter([])

    def train_iter(device=None):
        return iter([])

    def extract_labels(batch):
        return None

    def kd_loss(s_out, t_out, labels=None):
        return s_out

    def task_loss(s_out, labels):
        return None

    def evaluate(model):
        return 0.0

    def load_pretrained(model):
        class _R:
            pass
        return _R()
    """
).strip()


def _manifest(
    *,
    forward_calling_convention: str = "positional",
    metric_direction: str = "higher-better",
    eval_noise_atol: float = 1e-9,
    drop_sections: tuple[str, ...] = (),
    omit_adapters_entry: bool = False,
    omit_build_entry: bool = False,
    extra_retired: bool = False,
) -> str:
    import yaml
    doc = {
        "project_overview": {"task_type": "classification"},
        "model": {"location": "model.py", "build_entry": "build_model"},
        "training_and_evaluation": {
            "paradigm": "cross-entropy",
            "loss": "CE",
            "metric": {"name": "acc", "direction": metric_direction},
            "epochs": 1,
            "adapters_entry": "puzzle_adapters.py",
            "forward_calling_convention": forward_calling_convention,
            "eval_noise_atol": eval_noise_atol,
            "pretrained_ckpt": "ckpt.pt",
        },
        "data_and_environment": {"dataset": "ds"},
        "relevant_source_files": [],
    }
    if omit_adapters_entry:
        del doc["training_and_evaluation"]["adapters_entry"]
    if omit_build_entry:
        del doc["model"]["build_entry"]
    if extra_retired:
        doc["training_and_evaluation"]["eval_kind"] = "stale"
    for sec in drop_sections:
        doc.pop(sec, None)
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


def _setup_artifacts(
    tmp_path: Path,
    *,
    flat: str | None = _FLAT,
    adapters: str | None = _ADAPTERS,
    manifest: str | None = None,
) -> Path:
    if flat is not None:
        (tmp_path / "demo_flat.py").write_text(flat, encoding="utf-8")
    if adapters is not None:
        (tmp_path / "puzzle_adapters.py").write_text(adapters, encoding="utf-8")
    if manifest is not None:
        (tmp_path / "manifest.yaml").write_text(manifest, encoding="utf-8")
    return tmp_path


def _run_gate(artifacts_dir: Path) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["ORCA_ARTIFACTS_DIR"] = str(artifacts_dir)
    # bash should be available on the test host (Windows: Git Bash; Linux/macOS: native)
    proc = subprocess.run(
        ["bash", str(_GATE)], capture_output=True, text=True, env=env, cwd=str(artifacts_dir),
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_all_artifacts_valid_passes(self, tmp_path):
        _setup_artifacts(tmp_path, manifest=_manifest())
        rc, out, err = _run_gate(tmp_path)
        assert rc == 0, f"expected PASS, got rc={rc}\nstdout={out}\nstderr={err}"
        assert "[check_ingest] result: PASS" in out


# ---------------------------------------------------------------------------
# presence + py_compile
# ---------------------------------------------------------------------------

class TestPresence:
    def test_flat_missing_exits_nonzero(self, tmp_path):
        _setup_artifacts(tmp_path, flat=None, manifest=_manifest())
        rc, out, _ = _run_gate(tmp_path)
        assert rc != 0
        assert "FAIL" in out

    def test_adapters_missing_fails(self, tmp_path):
        _setup_artifacts(tmp_path, adapters=None, manifest=_manifest())
        rc, out, _ = _run_gate(tmp_path)
        assert rc != 0
        assert "puzzle_adapters.py missing" in out

    def test_manifest_missing_fails(self, tmp_path):
        _setup_artifacts(tmp_path)  # no manifest
        rc, out, _ = _run_gate(tmp_path)
        assert rc != 0
        assert "manifest.yaml missing" in out

    def test_flat_syntax_error_fails(self, tmp_path):
        _setup_artifacts(tmp_path, flat="def broken(:\n  pass\n", manifest=_manifest())
        rc, _, _ = _run_gate(tmp_path)
        assert rc != 0

    def test_adapters_syntax_error_fails(self, tmp_path):
        _setup_artifacts(tmp_path, adapters="def broken(:\n  pass\n", manifest=_manifest())
        rc, _, _ = _run_gate(tmp_path)
        assert rc != 0


# ---------------------------------------------------------------------------
# manifest schema
# ---------------------------------------------------------------------------

class TestManifestSchema:
    def test_missing_section_fails(self, tmp_path):
        _setup_artifacts(
            tmp_path,
            manifest=_manifest(drop_sections=("data_and_environment",)),
        )
        rc, out, _ = _run_gate(tmp_path)
        assert rc != 0
        assert "manifest schema" in out

    def test_adapters_entry_missing_fails(self, tmp_path):
        _setup_artifacts(tmp_path, manifest=_manifest(omit_adapters_entry=True))
        rc, _, _ = _run_gate(tmp_path)
        assert rc != 0

    def test_build_entry_missing_fails(self, tmp_path):
        _setup_artifacts(tmp_path, manifest=_manifest(omit_build_entry=True))
        rc, _, _ = _run_gate(tmp_path)
        assert rc != 0

    def test_retired_field_eval_kind_rejected(self, tmp_path):
        _setup_artifacts(tmp_path, manifest=_manifest(extra_retired=True))
        rc, _, _ = _run_gate(tmp_path)
        assert rc != 0


# ---------------------------------------------------------------------------
# forward-convention consistency
# ---------------------------------------------------------------------------

class TestForwardConventionConsistency:
    def test_convention_mismatch_fails(self, tmp_path):
        _setup_artifacts(
            tmp_path,
            manifest=_manifest(forward_calling_convention="dict"),  # adapters says positional
        )
        rc, out, _ = _run_gate(tmp_path)
        assert rc != 0
        assert "forward-convention consistency" in out

    def test_metric_direction_mismatch_fails(self, tmp_path):
        _setup_artifacts(
            tmp_path,
            manifest=_manifest(metric_direction="lower-better"),  # adapters says higher-better
        )
        rc, _, _ = _run_gate(tmp_path)
        assert rc != 0

    def test_atol_mismatch_fails(self, tmp_path):
        _setup_artifacts(
            tmp_path,
            manifest=_manifest(eval_noise_atol=1e-2),  # adapters says 1e-9
        )
        rc, _, _ = _run_gate(tmp_path)
        assert rc != 0


# ---------------------------------------------------------------------------
# flat __main__ smoke
# ---------------------------------------------------------------------------

class TestFlatMain:
    def test_flat_main_no_output_shape_fails(self, tmp_path):
        # __main__ runs but produces no digit in stdout
        bad_flat = "DUMMY_INPUT = {'shape': [1,3]}\n\ndef build_model():\n    pass\n\nif __name__ == '__main__':\n    print('done')\n"
        _setup_artifacts(tmp_path, flat=bad_flat, manifest=_manifest())
        rc, out, _ = _run_gate(tmp_path)
        # Either the gate's grep for digit fails, or python exits fine and we
        # still see "done" containing no digit string is unlikely — but a
        # no-shape print is caught by the grep. Make sure rc reflects schema
        # outcome (either PASS because 'done' has no digit → wait, 'done' has none).
        # The grep is for [0-9]+ so "done" fails the grep.
        assert rc != 0
