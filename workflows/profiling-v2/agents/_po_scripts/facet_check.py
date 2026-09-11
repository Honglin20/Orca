#!/usr/bin/env python3
"""facet_check.py — the welded-interface gate for every variant (v2 §3 Step 3).

Runs inside po_propose AFTER variant-implementer returns and BEFORE the impl
row / DONE marker are written; a failure routes to the existing
``append_impl_row.py --not-implemented --outcome structural_mismatch``
channel (no implemented=True→False snapshot rollback exists).

Duty ① (UNCONDITIONAL — structure-only variants included): the variant's
exported ONNX (variants/<vid>/onnx/model.onnx) must have EXACTLY the origin
base/model.onnx output signature — same output names in order, same dtype,
same static dims. The output is welded: metrics are computed on it, so an
output change makes every number incomparable, facets or not.

Duty ② (only when contracts.json ``facets.features`` is true): construct
input tensors ON the VARIANT feature pipeline (variants/<vid>/facets/) from
the eval contract's sample spec and compare them with the variant ONNX
inputs dim-by-dim (shape + dtype) — the input side MAY change with a
feature design, and this is the mechanical check that the pipeline and the
model still meet (eval feeds the same pipeline; train/eval comparability
depends on it).

Eval-contract sample spec consumed here (written by po_contract, §1):
  ``eval.sample_inputs``  [{"name", "shape", "dtype"}] — the RAW eval sample
                           (pre-pipeline), dtype ∈ float32|float64|int64|int32|bool
  ``eval.facet_builder``  {"module", "factory"} — a callable INSIDE the
                           facets dir receiving the raw tensors BY NAME
                           (kwargs) and returning the model-input tensors:
                           a sequence (positional) or a dict keyed by input
                           name.

Fail loud (exit 2, stderr names the root cause): a missing facets block in
contracts.json (torn workspace); a missing/unparseable contracts.json; a
missing variant/origin ONNX; an output signature mismatch (①); a features
workspace without the sample spec / builder; a builder that raises, returns
a wrong arity, or tensors whose shape/dtype disagree with the ONNX inputs.

Usage:
    facet_check.py --artifacts <ws> --vid <vid>
exit 0 = pass; exit 2 = fail loud.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# torch dtype name (lowercase) -> ONNX TensorProto elem_type
_ELEM_BY_DTYPE = {"float32": 1, "uint8": 2, "int8": 3, "uint16": 4,
                  "int16": 5, "int32": 6, "int64": 7, "bool": 9,
                  "float16": 10, "float64": 11}
_TORCH_BY_DTYPE = {"float32": "torch.float32", "float64": "torch.float64",
                   "int64": "torch.int64", "int32": "torch.int32",
                   "bool": "torch.bool"}


def _load_contracts(art: Path) -> dict:
    path = art / "contracts.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"contracts.json missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"contracts.json unparseable: {path} ({exc})") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"contracts.json is not a JSON object: {path}")
    if not isinstance(doc.get("facets"), dict):
        raise ValueError(
            "contracts.json carries no 'facets' block — a v2 workspace "
            "always has one (torn or pre-v2 workspace; the contract stage "
            "writes it, SPEC §1)")
    return doc


def _graph_signature(model_path: Path, io: str) -> list[dict[str, Any]]:
    """Ordered per-input/output signatures: {name, dtype(elem_type), dims}."""
    import onnx

    if not model_path.is_file():
        raise ValueError(f"ONNX model missing: {model_path}")
    try:
        model = onnx.load(str(model_path))
    except Exception as exc:  # onnx raises assorted types on torn files
        raise ValueError(f"ONNX model unparseable: {model_path} ({exc})") from exc
    values = getattr(model.graph, io)
    return [{"name": v.name,
             "elem_type": v.type.tensor_type.elem_type,
             "dims": [d.dim_value if d.HasField("dim_value") else d.dim_param
                      for d in v.type.tensor_type.shape.dim]}
            for v in values]


def _check_outputs(art: Path, vid: str) -> None:
    """Duty ① — the welded output signature (every variant, every round)."""
    origin = _graph_signature(art / "base" / "model.onnx", "output")
    variant = _graph_signature(art / "variants" / vid / "onnx" / "model.onnx",
                               "output")
    if origin != variant:
        raise ValueError(
            f"duty ① FAIL: variant {vid} ONNX output signature differs from "
            f"the origin base/model.onnx (origin={origin}, variant={variant}) "
            "— the output shape/semantics are welded (SPEC §0); adapt the "
            "design, never the check")


def _torch_dtype(name: str):
    import torch

    try:
        return getattr(torch, name)
    except AttributeError as exc:
        raise ValueError(
            f"eval.sample_inputs dtype {name!r} is not a torch dtype "
            f"(supported: {sorted(_TORCH_BY_DTYPE)})") from exc


def _make_raw(spec: dict, index: int) -> Any:
    """One raw sample tensor, fixed-seed deterministic (the export mold's
    make_dummy semantics — this gate never depends on values, only on
    shape/dtype)."""
    import torch

    name = spec.get("name")
    shape = spec.get("shape")
    dtype_name = str(spec.get("dtype", "float32"))
    if not isinstance(name, str) or not name:
        raise ValueError(f"eval.sample_inputs[{index}] lacks a name: {spec!r}")
    if (not isinstance(shape, list)
            or not all(isinstance(d, int) and not isinstance(d, bool)
                       for d in shape)):
        raise ValueError(
            f"eval.sample_inputs[{name!r}] shape must be a list of ints: "
            f"{shape!r}")
    if dtype_name not in _TORCH_BY_DTYPE:
        raise ValueError(
            f"eval.sample_inputs[{name!r}] dtype {dtype_name!r} is outside "
            f"the supported set {sorted(_TORCH_BY_DTYPE)}")
    dtype = _torch_dtype(dtype_name)
    torch.manual_seed(index)
    if dtype.is_floating_point:
        return torch.randn(shape, dtype=dtype)
    return torch.zeros(shape, dtype=dtype)


def _check_inputs(art: Path, vid: str, contracts: dict) -> None:
    """Duty ② — construct on the VARIANT feature pipeline, compare with the
    variant ONNX inputs dim-by-dim (shape + dtype)."""
    import torch

    eval_c = contracts.get("eval")
    if not isinstance(eval_c, dict):
        raise ValueError("contracts.json carries no 'eval' object")
    specs = eval_c.get("sample_inputs")
    builder_spec = eval_c.get("facet_builder")
    if not isinstance(specs, list) or not specs:
        raise ValueError(
            "features facet is available but contracts.json eval.sample_inputs "
            "is missing/empty — the eval contract must pin the raw sample "
            "spec for the input-interface check (SPEC §3 Step 3)")
    if (not isinstance(builder_spec, dict)
            or not isinstance(builder_spec.get("module"), str)
            or not isinstance(builder_spec.get("factory"), str)):
        raise ValueError(
            "features facet is available but contracts.json eval.facet_builder "
            "is missing/malformed ({module, factory} inside the facets dir)")
    facets_dir = art / "variants" / vid / "facets"
    if not facets_dir.is_dir():
        raise ValueError(
            f"duty ② FAIL: variants/{vid}/facets is missing — an "
            "edit pipeline copy must exist for every variant in a "
            "features-available workspace")

    raw = {spec["name"]: _make_raw(spec, i) for i, spec in enumerate(specs)}
    import importlib.util

    # load the pipeline module BY FILE PATH under a check-unique name: a
    # plain sys.path import could shadow (or be shadowed by) an stdlib /
    # third-party module of the same name in this process
    module_rel = builder_spec["module"].replace(".", "/")
    module_path = facets_dir / f"{module_rel}.py"
    if not module_path.is_file():
        raise ValueError(
            f"duty ② FAIL: the facet builder module is missing from the "
            f"variant pipeline: {module_path}")
    spec_obj = importlib.util.spec_from_file_location(
        f"pv2_facet_builder.{vid}.{builder_spec['module']}", module_path)
    if spec_obj is None or spec_obj.loader is None:
        raise ValueError(
            f"duty ② FAIL: cannot load the facet builder module "
            f"{module_path}")
    module = importlib.util.module_from_spec(spec_obj)
    try:
        spec_obj.loader.exec_module(module)
        factory = getattr(module, builder_spec["factory"])
        built = factory(**raw)
    except Exception as exc:
        raise ValueError(
            f"duty ② FAIL: the variant feature pipeline builder "
            f"{builder_spec['module']}.{builder_spec['factory']} raised on the "
            f"eval sample spec: {type(exc).__name__}: {exc}") from exc

    graph_inputs = _graph_signature(
        art / "variants" / vid / "onnx" / "model.onnx", "input")
    tensors: dict[str, Any]
    if isinstance(built, dict):
        tensors = built
    else:
        seq = list(built)
        if len(seq) != len(graph_inputs):
            raise ValueError(
                f"duty ② FAIL: the builder returned {len(seq)} tensors for "
                f"{len(graph_inputs)} ONNX inputs (positional return)")
        tensors = {inp["name"]: t for inp, t in zip(graph_inputs, seq)}

    if set(tensors) != {inp["name"] for inp in graph_inputs}:
        raise ValueError(
            f"duty ② FAIL: builder tensor names {sorted(tensors)} do not "
            f"match the variant ONNX inputs "
            f"{sorted(inp['name'] for inp in graph_inputs)}")
    for inp in graph_inputs:
        tensor = tensors[inp["name"]]
        if not isinstance(tensor, torch.Tensor):
            raise ValueError(
                f"duty ② FAIL: builder output for input {inp['name']!r} is "
                f"a {type(tensor).__name__}, not a torch.Tensor")
        shape = list(tensor.shape)
        elem = _ELEM_BY_DTYPE.get(str(tensor.dtype).removeprefix("torch."))
        if shape != inp["dims"] or elem != inp["elem_type"]:
            raise ValueError(
                f"duty ② FAIL: variant ONNX input {inp['name']!r} expects "
                f"dims={inp['dims']} elem_type={inp['elem_type']}, the "
                f"variant feature pipeline produced shape={shape} "
                f"dtype={tensor.dtype} — the input side may change with a "
                f"feature design, but the pipeline and the model must meet "
                f"(adapt them together)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("--vid", required=True)
    ns = ap.parse_args()
    art = Path(ns.artifacts)
    try:
        _check_outputs(art, ns.vid)     # ① unconditional
        contracts = _load_contracts(art)
        features = contracts["facets"].get("features")
        if features is not True:
            # structure-only variant / workspace: duty ② hangs idle by design
            sys.stderr.write(
                f"facet_check: duty ② skipped (contracts.json "
                f"facets.features={features!r})\n")
        else:
            _check_inputs(art, ns.vid, contracts)
    except (ValueError, KeyError, ImportError) as exc:
        print(f"facet_check: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "vid": ns.vid}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
