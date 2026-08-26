"""test_po_diff_check.py — two-layer declaration verification.

Covers: the graph layer's op_type count multiset diff, __pycache__/*.pyc
exclusion in the file layer, and the "reference is always the CURRENT base"
semantics (base that has already advanced past the original baseline).
Exit-code semantics: 0 match / 1 mismatch / >=2 hard error.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workflows" / "agents" / "_po_scripts"
sys.path.insert(0, str(_SCRIPTS))

import diff_check  # noqa: E402


# ── file layer ────────────────────────────────────────────────────────────────

def _mk_tree(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


BASE_FILES = {
    "demo_pkg/__init__.py": "init\n",
    "demo_pkg/model.py": "class Model: pass\n",
    "demo_pkg/layers.py": "L = 1\n",
}


def test_file_layer_excludes_pycache(tmp_path: Path):
    base = tmp_path / "base_shadow"
    variant = tmp_path / "variant_shadow"
    _mk_tree(base, BASE_FILES)
    _mk_tree(variant, {
        **BASE_FILES,
        "demo_pkg/model.py": "class Model: pass  # edited\n",   # real edit
        "demo_pkg/extra.py": "E = 1\n",                          # real addition
        "demo_pkg/__pycache__/model.cpython-311.pyc": "junk",
        "__pycache__/root.cpython-311.pyc": "junk",
    })
    ns = type("NS", (), {
        "base_shadow": str(base), "variant_shadow": str(variant),
        "edited_files": ["demo_pkg/model.py", "demo_pkg/extra.py"],
    })()
    result = diff_check.check_file_layer(ns)
    assert result["match"] is True, result
    assert sorted(result["diff_files"]) == ["demo_pkg/extra.py", "demo_pkg/model.py"]


def test_file_layer_mismatch_lists_both_sides(tmp_path: Path):
    base = tmp_path / "base_shadow"
    variant = tmp_path / "variant_shadow"
    _mk_tree(base, BASE_FILES)
    _mk_tree(variant, {**BASE_FILES, "demo_pkg/model.py": "edited\n"})
    ns = type("NS", (), {
        "base_shadow": str(base), "variant_shadow": str(variant),
        "edited_files": ["demo_pkg/layers.py"],  # declared the wrong file
    })()
    result = diff_check.check_file_layer(ns)
    assert result["match"] is False
    assert result["not_declared"] == ["demo_pkg/model.py"]
    assert result["declared_but_absent"] == ["demo_pkg/layers.py"]


def test_file_layer_reference_is_current_base_not_original_baseline(tmp_path: Path):
    """Round 2: the base shadow already carries round-1 edits (it advanced past
    the original baseline). A variant editing ONE file must show exactly that
    one file against the advanced base — inherited edits are not re-reported."""
    baseline_original = tmp_path / "baseline_shadow"          # round-0 original
    base_now = tmp_path / "base_shadow"                       # round-1 advanced base
    variant = tmp_path / "variant_shadow"
    _mk_tree(baseline_original, BASE_FILES)
    _mk_tree(base_now, {**BASE_FILES, "demo_pkg/layers.py": "L = 2\n"})  # round-1 edit
    _mk_tree(variant, {**BASE_FILES,
                       "demo_pkg/layers.py": "L = 2\n",        # inherited, identical
                       "demo_pkg/model.py": "edited round 2\n"})
    ns = type("NS", (), {
        "base_shadow": str(base_now), "variant_shadow": str(variant),
        "edited_files": ["demo_pkg/model.py"],
    })()
    result = diff_check.check_file_layer(ns)
    assert result["match"] is True, result


# ── graph layer ───────────────────────────────────────────────────────────────

def _mk_onnx(path: Path, op_types: list[str]) -> Path:
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper

    nodes = []
    cur = "x"
    for i, op in enumerate(op_types):
        out = f"t{i}"
        nodes.append(helper.make_node(op, [cur], [out], name=f"n{i}"))
        cur = out
    graph = helper.make_graph(
        nodes, "g",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 4])],
        [helper.make_tensor_value_info(cur, TensorProto.FLOAT, [1, 4])])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    onnx.save(model, str(path))
    return path


def test_graph_layer_multiset_diff(tmp_path: Path):
    base_onnx = _mk_onnx(tmp_path / "base.onnx", ["Transpose", "Relu", "Add"])
    variant_onnx = _mk_onnx(tmp_path / "variant.onnx", ["Relu", "Add", "Relu", "Relu"])
    ns = type("NS", (), {
        "base_onnx": str(base_onnx), "variant_onnx": str(variant_onnx),
        "op_delta": json.dumps({"Transpose": -1, "Relu": 2}),
    })()
    result = diff_check.check_graph_layer(ns)
    assert result["match"] is True, result
    assert result["actual_op_delta"] == {"Relu": 2, "Transpose": -1}

    ns_wrong = type("NS", (), {
        "base_onnx": str(base_onnx), "variant_onnx": str(variant_onnx),
        "op_delta": json.dumps({"Relu": 1}),
    })()
    wrong = diff_check.check_graph_layer(ns_wrong)
    assert wrong["match"] is False
    assert set(wrong["mismatched_ops"]) == {"Relu", "Transpose"}


def test_graph_layer_zero_entries_are_ignored(tmp_path: Path):
    base_onnx = _mk_onnx(tmp_path / "base.onnx", ["Relu"])
    variant_onnx = _mk_onnx(tmp_path / "variant.onnx", ["Relu", "Add"])
    ns = type("NS", (), {
        "base_onnx": str(base_onnx), "variant_onnx": str(variant_onnx),
        "op_delta": json.dumps({"Add": 1, "Squeeze": 0}),  # 0 entries are noise
    })()
    assert diff_check.check_graph_layer(ns)["match"] is True


# ── CLI exit-code contract ────────────────────────────────────────────────────

def test_cli_exit_codes(tmp_path: Path):
    base = tmp_path / "base_shadow"
    variant = tmp_path / "variant_shadow"
    _mk_tree(base, {"a.py": "1\n"})
    _mk_tree(variant, {"a.py": "1\n"})
    match = subprocess.run(
        [sys.executable, str(_SCRIPTS / "diff_check.py"), "--layer", "file",
         "--base-shadow", str(base), "--variant-shadow", str(variant),
         "--edited-files", "[]"],
        capture_output=True, text=True, timeout=60)
    assert match.returncode == 0
    assert json.loads(match.stdout)["match"] is True

    mismatch = subprocess.run(
        [sys.executable, str(_SCRIPTS / "diff_check.py"), "--layer", "file",
         "--base-shadow", str(base), "--variant-shadow", str(variant),
         "--edited-files", '["ghost.py"]'],
        capture_output=True, text=True, timeout=60)
    assert mismatch.returncode == 1
    assert json.loads(mismatch.stdout)["match"] is False

    missing = subprocess.run(
        [sys.executable, str(_SCRIPTS / "diff_check.py"), "--layer", "graph",
         "--base-onnx", str(tmp_path / "nope.onnx"),
         "--variant-onnx", str(tmp_path / "nope2.onnx"),
         "--op-delta", "{}"],
        capture_output=True, text=True, timeout=60)
    assert missing.returncode >= 2


def test_cli_rejects_retired_weights_layer():
    """The weights layer retired together with the weight-inheritance
    mechanism (train-from-scratch paradigm) — asking for it must fail loud,
    never silently pass."""
    retired = subprocess.run(
        [sys.executable, str(_SCRIPTS / "diff_check.py"), "--layer", "weights"],
        capture_output=True, text=True, timeout=60)
    assert retired.returncode != 0
    assert "invalid choice" in retired.stderr
