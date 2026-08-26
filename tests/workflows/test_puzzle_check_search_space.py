"""Unit tests for pz_search_space/scripts/check_search_space.py.

The script is the deterministic gate for the layer-granularity search-space
declaration. It delegates schema / kind / path-uniqueness / candidate-catalog /
identity-per-kind validation to ``search_space_io.load_search_space_yaml``, and
adds layer-granularity-specific ``must_extract`` placeholder rules on top.

Covered here (Rule 9: verify intent — each test constructs a violating input and
asserts the gate rejects it fail-loud, plus the legitimate-empty-slots branch):
  - _check_layer_fields: must_extract missing, max_seq_len hardcoded (LV-7 guard),
    mask_load_bearing declared true, in_dim/out_dim non-placeholder, layer_evidence
    missing / empty, non-transformer_layer kind skipped.
  - _check_contract: empty slots (legitimate unsupported branch, no failure),
    transformer_layer candidates key missing when slots present.
  - _load_pattern: missing file -> fail, missing must_extract -> fail.
  - main(): end-to-end via load_search_space_yaml (YAML malformed, schema fail,
    happy path with -1 placeholders + identity in catalog).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

_REPO = Path(__file__).resolve().parents[2]
_PUZZLE_SCRIPTS = _REPO / "workflows" / "agents" / "_puzzle_scripts"
sys.path.insert(0, str(_PUZZLE_SCRIPTS))

_GATE = (
    _REPO / "workflows" / "agents" / "pz_search_space" / "scripts" / "check_search_space.py"
)


def _load_gate():
    spec = importlib.util.spec_from_file_location("puzzle_check_search_space_under_test", str(_GATE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

_PATTERN = {
    "kind": "transformer_layer",
    "must_extract": [
        "num_heads", "head_dim", "original_intermediate", "activation",
        "norm_type", "max_seq_len", "mask_load_bearing",
    ],
    "structural_signature": {},
    "evidence_template": "forward = {attn} -> ...",
    "reject_when": [],
}


def _write_pattern(agent_resources: Path, pattern: dict | None = None) -> Path:
    refs = agent_resources / "references"
    refs.mkdir(parents=True, exist_ok=True)
    p = refs / "transformer_layer_pattern.json"
    p.write_text(json.dumps(pattern if pattern is not None else _PATTERN), encoding="utf-8")
    return p


def _slot(
    *,
    id: str = "L1",
    path: str = "encoder_layer1",
    kind: str = "transformer_layer",
    layer_idx: int = 1,
    layer_evidence: str = "forward = attn -> norm1(res+drop) -> ffn -> norm2(res+drop)",
    num_heads: int = 4,
    head_dim: int = 32,
    original_intermediate: int = 256,
    activation: str = "relu",
    norm_type: str = "layernorm",
    max_seq_len: int = -1,
    mask_load_bearing: bool = False,
    in_dim: int = -1,
    out_dim: int = -1,
    **extra,
) -> dict:
    d = {
        "id": id, "path": path, "kind": kind, "layer_idx": layer_idx,
        "num_heads": num_heads, "head_dim": head_dim,
        "original_intermediate": original_intermediate, "activation": activation,
        "norm_type": norm_type, "max_seq_len": max_seq_len,
        "mask_load_bearing": mask_load_bearing, "in_dim": in_dim, "out_dim": out_dim,
        "layer_evidence": layer_evidence,
    }
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# _check_layer_fields — placeholder / must_extract rules
# ---------------------------------------------------------------------------

class TestCheckLayerFields:
    def test_happy_path_all_placeholders(self):
        mod = _load_gate()
        errs = mod._check_layer_fields(_slot(), _PATTERN["must_extract"], 0)
        assert errs == []

    def test_missing_must_extract_field_blocker(self):
        mod = _load_gate()
        slot = _slot()
        del slot["num_heads"]
        errs = mod._check_layer_fields(slot, _PATTERN["must_extract"], 0)
        assert len(errs) == 1
        assert "missing must_extract field 'num_heads'" in errs[0]

    def test_max_seq_len_hardcoded_rejected(self):
        """spec-reviewer LV-7 guard: max_seq_len fallback over-parameterizes."""
        mod = _load_gate()
        errs = mod._check_layer_fields(_slot(max_seq_len=512), _PATTERN["must_extract"], 0)
        assert any("max_seq_len must be -1" in e and "512" in e for e in errs)

    def test_max_seq_len_negative_placeholder_allowed(self):
        mod = _load_gate()
        assert mod._check_layer_fields(_slot(max_seq_len=-1), _PATTERN["must_extract"], 0) == []

    def test_mask_load_bearing_true_rejected(self):
        """Signature-only claim is not load-bearing; baseline runtime-traces."""
        mod = _load_gate()
        errs = mod._check_layer_fields(_slot(mask_load_bearing=True), _PATTERN["must_extract"], 0)
        assert any("mask_load_bearing must be declared false" in e for e in errs)

    def test_in_dim_hardcoded_rejected(self):
        mod = _load_gate()
        errs = mod._check_layer_fields(_slot(in_dim=128), _PATTERN["must_extract"], 0)
        assert any("in_dim must be -1" in e and "128" in e for e in errs)

    def test_out_dim_hardcoded_rejected(self):
        mod = _load_gate()
        errs = mod._check_layer_fields(_slot(out_dim=128), _PATTERN["must_extract"], 0)
        assert any("out_dim must be -1" in e for e in errs)

    def test_layer_evidence_missing(self):
        mod = _load_gate()
        slot = _slot()
        del slot["layer_evidence"]
        errs = mod._check_layer_fields(slot, _PATTERN["must_extract"], 0)
        assert any("missing layer_evidence" in e for e in errs)

    def test_layer_evidence_empty(self):
        mod = _load_gate()
        errs = mod._check_layer_fields(_slot(layer_evidence="   "), _PATTERN["must_extract"], 0)
        assert any("layer_evidence is empty" in e for e in errs)

    def test_kind_evidence_legacy_key_accepted(self):
        """v2 schema used kind_evidence; gate tolerates it as fallback."""
        mod = _load_gate()
        slot = _slot()
        del slot["layer_evidence"]
        slot["kind_evidence"] = "forward = attn -> norm1 -> ffn -> norm2"
        assert mod._check_layer_fields(slot, _PATTERN["must_extract"], 0) == []

    def test_non_transformer_layer_kind_skipped(self):
        """Legacy block kinds (attention/ffn) are not this gate's concern."""
        mod = _load_gate()
        slot = _slot(kind="attention")
        # remove attention-specific fields that aren't in must_extract anyway
        slot.pop("num_heads", None)
        errs = mod._check_layer_fields(slot, _PATTERN["must_extract"], 0)
        assert errs == []


# ---------------------------------------------------------------------------
# _check_contract — empty slots + candidates key
# ---------------------------------------------------------------------------

class TestCheckContract:
    def test_empty_slots_legitimate_unsupported_branch(self):
        """Empty slots = model_type_supported=false (valid); gate must not fail."""
        mod = _load_gate()
        errs = mod._check_contract([], {}, _PATTERN["must_extract"])
        assert errs == []

    def test_slots_present_missing_transformer_layer_candidates_key(self):
        mod = _load_gate()
        errs = mod._check_contract([_slot()], {"attention": ["identity"]}, _PATTERN["must_extract"])
        assert any("missing 'transformer_layer' key" in e for e in errs)

    def test_slots_present_with_transformer_layer_candidates_no_err(self):
        mod = _load_gate()
        errs = mod._check_contract(
            [_slot()], {"transformer_layer": ["identity", "vanilla_layer"]},
            _PATTERN["must_extract"],
        )
        assert errs == []


# ---------------------------------------------------------------------------
# _load_pattern
# ---------------------------------------------------------------------------

class TestLoadPattern:
    def test_missing_file_raises(self, tmp_path):
        mod = _load_gate()
        with pytest.raises(FileNotFoundError):
            mod._load_pattern(tmp_path)

    def test_missing_must_extract_raises(self, tmp_path):
        mod = _load_gate()
        _write_pattern(tmp_path, {"kind": "transformer_layer"})  # no must_extract
        with pytest.raises(RuntimeError, match="must_extract"):
            mod._load_pattern(tmp_path)

    def test_happy_loads_must_extract(self, tmp_path):
        mod = _load_gate()
        _write_pattern(tmp_path)
        pat = mod._load_pattern(tmp_path)
        assert "num_heads" in pat["must_extract"]


# ---------------------------------------------------------------------------
# main() — end-to-end via the real search_space_io loader
# ---------------------------------------------------------------------------

def _write_search_space(tmp_path: Path, slots: list, candidates: dict | None) -> Path:
    import yaml
    p = tmp_path / "search_space.yaml"
    payload: dict = {"slots": slots}
    if candidates is not None:
        payload["candidates"] = candidates
    p.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


class TestMain:
    def test_main_pass_on_valid_declaration(self, tmp_path, monkeypatch):
        mod = _load_gate()
        _write_pattern(tmp_path)
        _write_search_space(
            tmp_path,
            [_slot()],
            {"transformer_layer": ["identity", "vanilla_layer"]},
        )
        rc = mod.main.__wrapped__(mod) if hasattr(mod.main, "__wrapped__") else _run_main(mod, tmp_path)
        assert rc == 0

    def test_main_fail_on_missing_must_extract_field(self, tmp_path):
        mod = _load_gate()
        _write_pattern(tmp_path)
        slot = _slot()
        del slot["activation"]
        _write_search_space(tmp_path, [slot], {"transformer_layer": ["identity"]})
        assert _run_main(mod, tmp_path) == 1

    def test_main_fail_on_max_seq_len_hardcoded(self, tmp_path):
        mod = _load_gate()
        _write_pattern(tmp_path)
        _write_search_space(
            tmp_path,
            [_slot(max_seq_len=512)],
            {"transformer_layer": ["identity"]},
        )
        assert _run_main(mod, tmp_path) == 1

    def test_main_pass_on_empty_slots_unsupported_branch(self, tmp_path):
        """Empty slots -> model_type_supported=false (valid); gate passes."""
        mod = _load_gate()
        _write_pattern(tmp_path)
        _write_search_space(tmp_path, [], None)
        assert _run_main(mod, tmp_path) == 0

    def test_main_fail_on_yaml_missing(self, tmp_path):
        mod = _load_gate()
        _write_pattern(tmp_path)
        # no search_space.yaml written
        assert _run_main(mod, tmp_path) == 1

    def test_main_fail_on_pattern_missing(self, tmp_path):
        mod = _load_gate()
        _write_search_space(
            tmp_path,
            [_slot()],
            {"transformer_layer": ["identity"]},
        )
        # no pattern.json written under tmp_path/references/
        assert _run_main(mod, tmp_path) == 1


def _run_main(mod, artifacts_dir: Path) -> int:
    """Invoke main() with sys.argv mocked; captures stderr; returns exit code."""
    import io, contextlib
    repo = Path(__file__).resolve().parents[2]
    argv = [
        "check_search_space.py",
        "--artifacts-dir", str(artifacts_dir),
        "--scripts-dir", str(_PUZZLE_SCRIPTS),
        "--agent-resources", str(artifacts_dir),  # references/transformer_layer_pattern.json lives under it
    ]
    saved = sys.argv
    sys.argv = argv
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            rc = mod.main()
    finally:
        sys.argv = saved
    return rc
