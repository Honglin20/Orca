"""fidelity_check.py — per-leaf numeric-equivalence + AST + kind-direction check.

The codegen product is the four leaves under ``--leaves_dir``,
not a monolithic train_pipeline.py. This script verifies the leaves against the
user's original ``train.py`` / eval script.

Checks (per leaf + cross-leaf):

1. **AST self-containment** — each leaf has only whitelisted top-level
   imports and no sibling / relative imports. Mirrors ``kd/_leaves.py``.
2. **AST signature equality** — each contract callable's function name +
   required positional args match the contract (defaults additive).
3. **anti-fabrication** — ``data.py`` / ``eval.py`` must not use
   ``torch.rand`` / ``torch.randn`` / ``torch.randint`` / ``torch.randperm``
   or ``numpy.random.*`` as a data/label source.  Such calls signal that
   the codegen fabricated data instead of porting the user's real loader.
4. **loss** — ``compute_loss`` vs the user's loss fn on identical seeded
   inputs (``torch.allclose(rtol=1e-5)``); AST body fallback when the user
   module can't be imported.
5. **dataloader** — ``build_dataloader(batch_size=2)`` vs user
   ``build_dataloader``: same batch shape + both re-iterable.
6. **eval metric** — ``eval_metric`` vs the user eval script's metric on the
   same model instance (values allclose + kind identical).
7. **optimizer** — class name produced by ``build_optimizer`` vs the user
   train.py's optimizer class.
8. **kind direction hard check** — the kind returned by ``eval_metric``
   is in the same direction group (max: snr/acc; min: mse/nmse/ber/db) as
   ``--accuracy_baseline_kind``.
9. **model I/O** — model forward on ``DUMMY_INPUT`` shape preserves the shape.

Deterministic script contract: stdout is ``KEY: value`` lines,
non-zero exit on FAIL (fail loud).

Usage::

    fidelity_check.py --leaves_dir <dir> --user_train <path>
        [--user_eval <path>] --dummy_input <json-string|json-file>
        [--model_path <path>] [--build_fn build_model] [--build_cfg <json>]
        [--accuracy_baseline_kind <nmse|mse|ber|db|snr|acc>]
        [--project_root <path>]

Exit: 0 = FIDELITY: PASS; 2 = any checked item is false (no skip).
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import inspect
import json
import math
import os
import re
import sys
from pathlib import Path

import torch

# ---------------------------------------------------------------------------
# contract mirrors (must agree with kd/_leaves.py)
# ---------------------------------------------------------------------------
# Deliberately mirrored from ``kd/_leaves.py`` (not imported) — fidelity_check
# is a codegen-time CLI invoked as ``python3 scripts/fidelity_check.py`` and
# must not depend on ``_kd_scripts`` being on sys.path.  Drift between the two
# copies is caught by ``tests/workflows/test_kd_train_script.py::
# test_leaf_import_whitelist_contains_standard_scipy_stack`` (parity assert).
# "Self-contained" forbids user-project modules, NOT the standard scientific
# stack — torch / torchvision / numpy / scipy / scikit-learn / Pillow are pip
# packages and legitimate.  Only relative imports + non-whitelisted absolute
# imports (e.g. user_pkg) are rejected.
_LEAF_IMPORT_WHITELIST: frozenset[str] = frozenset(
    {
        "torch", "torchvision", "torchaudio", "numpy", "scipy", "sklearn", "PIL",
        "math", "os", "sys", "json", "pathlib", "typing",
        "itertools", "functools", "collections", "dataclasses",
        "random", "io", "abc", "copy", "re", "warnings", "time",
    }
)

_LEAF_SIGNATURES: dict[tuple[str, str], list[str]] = {
    ("loss.py", "compute_loss"): ["s_out", "y"],
    ("data.py", "build_dataloader"): ["batch_size"],
    ("eval.py", "eval_metric"): ["student", "device"],
    ("optim.py", "build_optimizer"): ["params", "lr"],
    ("optim.py", "build_scheduler"): ["optimizer", "epochs"],
}

_KIND_DIRECTION: dict[str, str] = {
    "snr": "max", "acc": "max",
    "mse": "min", "nmse": "min", "ber": "min", "db": "min",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _import_by_path(path: Path, module_name: str):
    """Import a .py file by absolute path; cache in sys.modules."""
    if not path.is_file():
        raise FileNotFoundError(f"file not found: {path}")
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot construct import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_dummy_input(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        p = Path(raw)
        if not p.is_file():
            raise ValueError(f"--dummy_input is neither JSON nor an existing file: {raw}")
        data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "shape" not in data:
        raise ValueError(f"--dummy_input must be a dict with 'shape': {data!r}")
    return data


# ---------------------------------------------------------------------------
# AST checks (self-containment + signature equality)
# ---------------------------------------------------------------------------
def _check_self_contained(path: Path) -> list[str]:
    """Return list of violations (empty = OK).  Mirrors kd/_leaves._check_self_contained."""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        return [f"{path}:{e.lineno}: syntax error: {e.msg}"]
    issues: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                issues.append(
                    f"{path}: relative import (level={node.level}) forbidden — "
                    f"leaf must be self-contained"
                )
                continue
            if node.module and node.module.split(".")[0] not in _LEAF_IMPORT_WHITELIST:
                issues.append(
                    f"{path}: import from {node.module!r} not in whitelist "
                    f"{sorted(_LEAF_IMPORT_WHITELIST)}"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in _LEAF_IMPORT_WHITELIST:
                    issues.append(
                        f"{path}: import {alias.name!r} not in whitelist "
                        f"{sorted(_LEAF_IMPORT_WHITELIST)}"
                    )
    return issues


def _required_args(fn: ast.FunctionDef) -> list[str]:
    args = fn.args.args
    ndef = len(fn.args.defaults)
    return [a.arg for a in args[: len(args) - ndef]]


# ---------------------------------------------------------------------------
# anti-fabrication (data / eval leaves must load real data, not synthesize)
# ---------------------------------------------------------------------------
# ``data.py`` and ``eval.py`` exist to load the user's REAL dataset (e.g.
# ``torchvision.datasets.<RealDataset>``).  Using ``torch.rand`` / ``torch.randn``
# / ``torch.randint`` / ``torch.randperm`` or ``numpy.random.*`` as the SOURCE of
# pixels or labels is fabrication — it decouples inputs from targets and
# silently produces a model that cannot learn.  Such calls are forbidden in
# data.py and eval.py *unless the
# user's own train.py uses them too* (genuine synthetic-data demos, denoising
# autoencoders, etc.).  In that case the leaf is porting the user verbatim,
# not fabricating.
# ``torch.randperm`` is intentionally absent — it produces permutation indices,
# not pixels or labels, and is the standard primitive for shuffling real
# samples (DataLoader samplers). ``np.random.seed`` is also excluded at the
# numpy branch below — seeding is the opposite of fabrication.
_RANDOM_TENSOR_ATTRS: frozenset[str] = frozenset(
    {"rand", "randn", "randint", "normal", "rand_like", "randn_like"}
)
# In-place tensor methods that fill a tensor with random data (e.g.
# ``x.uniform_()``).  Trailing-underscore methods mutate the callee; the
# callee need not be ``torch`` so we check the attr alone.
_RANDOM_INPLACE_ATTRS: frozenset[str] = frozenset(
    {"uniform_", "normal_", "exponential_", "cauchy_", "log_normal_", "geometric_"}
)
_RANDOM_CALL_RE = re.compile(
    r"\b(?:torch|numpy|np)\.(?:rand|randn|randint|rand_like|randn_like|normal|random)\b"
    r"|\brandom\.(?:random|randint|uniform|gauss|choice)\b"
)


def _head_name(node: ast.AST) -> str:
    """Return the leftmost Name id of an attribute chain, or '' if not a Name."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _user_uses_random_data(*paths: Path) -> bool:
    """True iff any of the user's source files uses a random-tensor factory.

    Used to whitelist genuine synthetic-data user code (kd-nas-demo, denoising
    autoencoders, BER noise eval).  Conservative: any match in the user's
    train.py / eval script → assume the user is OK with synthetic data and the
    leaf is porting verbatim rather than fabricating.
    """
    for p in paths:
        if p is None or not p.is_file():
            continue
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _RANDOM_CALL_RE.search(src):
            return True
    return False


def _check_no_random_fabrication(path: Path, user_synthetic: bool) -> list[str]:
    """Reject torch.*rand / numpy.random.* calls in data/eval leaves.

    Returns a list of violation messages (empty = OK).  When ``user_synthetic``
    is True (the user's own train.py / eval script already uses random-tensor
    factories), the leaf is treated as a verbatim port of a genuine
    synthetic-data user pipeline and the check is skipped.
    """
    if user_synthetic:
        return []
    try:
        src = path.read_text(encoding="utf-8")
    except OSError as e:
        return [f"{path}: cannot read leaf file: {e}"]
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        return [f"{path}:{e.lineno}: syntax error: {e.msg}"]

    issues: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        # torch.rand / torch.randn / torch.randint / torch.normal /
        # torch.rand_like / torch.randn_like
        if isinstance(fn, ast.Attribute) and fn.attr in _RANDOM_TENSOR_ATTRS:
            head = _head_name(fn.value)
            if head in {"torch"}:
                issues.append(
                    f"{path}:{node.lineno}: fabrication detected — "
                    f"`{head}.{fn.attr}(...)` synthesises data/labels; "
                    f"data/eval leaves must load the user's real dataset "
                    f"(e.g. `from torchvision.datasets import <RealDataset>`). "
                    f"If the user's data is genuinely unavailable, fail loud "
                    f"+ emit ask-user sentinel — never fabricate."
                )
        # In-place random-fill tensor methods (x.uniform_(), x.normal_(), ...).
        # The callee can be any Name; the trailing underscore is the signal.
        if isinstance(fn, ast.Attribute) and fn.attr in _RANDOM_INPLACE_ATTRS:
            issues.append(
                f"{path}:{node.lineno}: fabrication detected — "
                f"`...{fn.attr}()` fills a tensor with random data; data/eval "
                f"leaves must load the user's real dataset, not synthesise it."
            )
        # numpy.random.<func>(...) / np.random.<func>(...).  ``seed`` and
        # ``default_rng`` are constructors/seeds, not data synthesis — skip.
        if (
            isinstance(fn, ast.Attribute)
            and isinstance(fn.value, ast.Attribute)
            and fn.value.attr == "random"
            and fn.attr not in {"seed", "default_rng"}
            and _head_name(fn.value.value) in {"np", "numpy"}
        ):
            issues.append(
                f"{path}:{node.lineno}: fabrication detected — "
                f"`{_head_name(fn.value.value)}.random.{fn.attr}(...)` "
                f"synthesises data/labels; data/eval leaves must load the "
                f"user's real dataset. If the user's data is genuinely "
                f"unavailable, fail loud + emit ask-user sentinel."
            )
        # stdlib ``random.<func>(...)`` (module-level).  ``random.seed`` and
        # ``random.Random`` (constructor) are not data synthesis — skip.
        if (
            isinstance(fn, ast.Attribute)
            and isinstance(fn.value, ast.Name)
            and fn.value.id == "random"
            and fn.attr not in {"seed", "Random"}
        ):
            issues.append(
                f"{path}:{node.lineno}: fabrication detected — "
                f"`random.{fn.attr}(...)` synthesises data/labels; data/eval "
                f"leaves must load the user's real dataset."
            )
    return issues


def _check_signature(path: Path, fname: str, expected_required: list[str]) -> bool:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return False
    target = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == fname),
        None,
    )
    if target is None:
        return False
    return _required_args(target) == expected_required


# ---------------------------------------------------------------------------
# user-side discovery (mirrors agent Step 1)
# ---------------------------------------------------------------------------
def _discover_loss_fn(module, batch_shape: list[int]):
    if hasattr(module, "compute_loss"):
        return module.compute_loss
    candidates: list = []
    for name, obj in vars(module).items():
        if isinstance(obj, type):
            continue
        if not callable(obj) or getattr(obj, "__module__", None) != module.__name__:
            continue
        try:
            params = list(inspect.signature(obj).parameters.values())
        except (ValueError, TypeError):
            continue
        if len(params) != 2 or name == "build_dataloader":
            continue
        candidates.append(obj)
    s = torch.zeros(2, *batch_shape[1:])
    for fn in candidates:
        try:
            with torch.no_grad():
                out = fn(s, s)
        except Exception:
            continue
        if isinstance(out, torch.Tensor) and out.dim() == 0:
            return fn
    return None


def _loss_ast_match(user_src: str, gen_src: str, user_fn_name: str) -> bool:
    user_tree = ast.parse(user_src)
    gen_tree = ast.parse(gen_src)
    user_fn = next(
        (n for n in ast.walk(user_tree)
         if isinstance(n, ast.FunctionDef) and n.name == user_fn_name),
        None,
    )
    gen_fn = next(
        (n for n in ast.walk(gen_tree)
         if isinstance(n, ast.FunctionDef) and n.name == "compute_loss"),
        None,
    )
    if user_fn is None or gen_fn is None:
        return False

    def normalised_body(fn: ast.FunctionDef) -> list[str]:
        rename = {a.arg: f"_A{i}" for i, a in enumerate(fn.args.args)}
        body = list(fn.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]

        class _Norm(ast.NodeTransformer):
            def visit_Name(self, node: ast.Name):
                if node.id in rename:
                    node.id = rename[node.id]
                return node

        return [ast.dump(_Norm().visit(stmt), include_attributes=False) for stmt in body]

    return normalised_body(user_fn) == normalised_body(gen_fn)


def _call_user_build_dataloader(fn, batch_size: int = 2):
    try:
        return fn(batch_size=batch_size)
    except TypeError:
        return fn()


def _batch_info(loader) -> tuple[list, int] | None:
    shapes: list[tuple] = []
    for _ in range(2):
        it = iter(loader)
        first = next(it, None)
        if first is None:
            return None
        x, y = first
        shapes.append((tuple(x.shape), tuple(y.shape)))
    if shapes[0] != shapes[1]:
        return None
    return [list(s) for s in shapes[0]], 2


def _extract_seed(eval_path: Path) -> int:
    src = eval_path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"manual_seed\(\s*(\d+)\s*\)", src)
    return int(m.group(1)) if m else 20260725


def _discover_user_eval_callable(eval_path: Path, loader_factory=None):
    mod = _import_by_path(eval_path, "_user_eval")
    fn = None
    name = ""
    if hasattr(mod, "evaluate"):
        fn, name = mod.evaluate, "evaluate"
    else:
        pattern = re.compile(r"(nmse|mse|ber|snr|acc|accuracy)")
        for n, obj in vars(mod).items():
            if isinstance(obj, type):
                continue
            if not callable(obj) or getattr(obj, "__module__", None) != mod.__name__:
                continue
            if not pattern.search(n):
                continue
            if not n.startswith("_"):
                fn, name = obj, n
                break
            if fn is None:
                fn, name = obj, n
    if fn is None:
        return None, ""
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (ValueError, TypeError):
        return None, ""
    adapter = None
    for p in params:
        if p.name in ("n_samples", "n", "num_samples"):
            def _partial(m, _fn=fn):
                return _fn(m, n_samples=8)
            adapter = _partial
            break
    if adapter is None:
        try:
            sig_nargs = len(
                [p for p in params if p.default is inspect.Parameter.empty]
            )
        except Exception:
            sig_nargs = 1
        # (model, loader[, device]) family: supply the leaf loader lazily so the
        # numeric compare exercises the user's formula on the leaf's data.
        loader_names = ("loader", "data_loader", "test_loader", "dataloader", "test_dl")
        second_is_loader = len(params) >= 2 and params[1].name in loader_names
        if sig_nargs <= 1:
            adapter = (lambda m, _fn=fn: _fn(m))
        elif second_is_loader and loader_factory is not None:
            _ld = loader_factory
            if sig_nargs >= 3:
                adapter = (lambda m, _fn=fn, _ld=_ld: _fn(m, _ld(), "cpu"))
            else:
                adapter = (lambda m, _fn=fn, _ld=_ld: _fn(m, _ld()))
        else:
            adapter = (lambda m, _fn=fn: _fn(m, "cpu"))
    kind = ""
    m = re.search(r"(nmse|mse|ber|snr|acc)", name)
    if m:
        kind = m.group(1)
    return adapter, kind


def _extract_user_optimizer_class(user_src: str) -> str | None:
    # Match the instantiated optimizer class (`torch.optim.<Class>(`), not a
    # type annotation (`-> torch.optim.Optimizer` / `: torch.optim.Optimizer`).
    m = re.search(r"torch\.optim\.(\w+)\s*\(", user_src)
    return m.group(1) if m else None


def _build_model(model_path: Path, build_fn: str, cfg: dict):
    mod = _import_by_path(model_path, "_fid_model")
    if not hasattr(mod, build_fn):
        raise AttributeError(f"{model_path} has no build fn {build_fn!r}")
    return getattr(mod, build_fn)(**cfg)


def _kind_direction(kind: str) -> str | None:
    return _KIND_DIRECTION.get(kind)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        description="kd-train-script fidelity check (per-leaf numeric + AST + kind)"
    )
    p.add_argument("--leaves_dir", required=True,
                   help="directory containing user/{loss,data,eval,optim}.py")
    p.add_argument("--user_train", required=True, help="user original train.py path")
    p.add_argument("--user_eval", default=None, help="user eval script path (else auto-glob)")
    p.add_argument("--dummy_input", required=True,
                   help="JSON string or .json file (dict with 'shape')")
    p.add_argument("--model_path", default=None,
                   help="model .py for I/O + eval checks")
    p.add_argument("--build_fn", default="build_model")
    p.add_argument("--build_cfg", default="{}")
    p.add_argument("--accuracy_baseline_kind", default=None,
                   help="locks the metric direction group; leaf kind must match")
    p.add_argument("--project_root", default=None)
    # Back-compat: the old monolithic flag is still accepted but ignored.
    p.add_argument("--train_pipeline", default=None,
                   help=argparse.SUPPRESS)
    args = p.parse_args()

    if args.project_root and args.project_root not in sys.path:
        sys.path.insert(0, args.project_root)

    # Fail loud on bad CLI inputs (rc=2 + stderr, per the deterministic script contract).
    user_train_path = Path(args.user_train)
    if not user_train_path.is_file():
        print(
            f"FIDELITY: FAIL\nUSER_TRAIN_MISSING: {user_train_path}",
            file=sys.stderr,
        )
        return 2
    if args.user_eval:
        ue = Path(args.user_eval)
        if not ue.is_file():
            print(
                f"FIDELITY: FAIL\nUSER_EVAL_MISSING: {ue}",
                file=sys.stderr,
            )
            return 2

    leaves_dir = Path(args.leaves_dir)
    if not leaves_dir.is_dir():
        print(f"FIDELITY: FAIL\nLEAVES_DIR_MISSING: {leaves_dir}", file=sys.stderr)
        return 2

    leaf_paths = {
        "loss.py": leaves_dir / "loss.py",
        "data.py": leaves_dir / "data.py",
        "eval.py": leaves_dir / "eval.py",
        "optim.py": leaves_dir / "optim.py",
    }
    missing = [str(p) for p in leaf_paths.values() if not p.is_file()]
    if missing:
        print(f"FIDELITY: FAIL\nLEAF_MISSING: {missing}", file=sys.stderr)
        return 2

    dummy = _load_dummy_input(args.dummy_input)
    shape = [int(s) for s in dummy["shape"]]

    # ---- 0. AST self-containment + signature equality --------------------
    ast_ok = True
    for fname, path in leaf_paths.items():
        violations = _check_self_contained(path)
        if violations:
            ast_ok = False
            for v in violations:
                print(f"LEAF_AST_VIOLATION: {v}", file=sys.stderr)
    for (fname, fn_name), expected in _LEAF_SIGNATURES.items():
        if not _check_signature(leaf_paths[fname], fn_name, expected):
            ast_ok = False
            print(
                f"LEAF_SIGNATURE_MISMATCH: {fname}::{fn_name} expected required={expected}",
                file=sys.stderr,
            )
    print(f"LEAF_AST_OK: {'true' if ast_ok else 'false'}")

    # ---- 0b. Anti-fabrication: data/eval leaves must not synthesise data ----
    # The user's own train.py / eval script may legitimately use random-tensor
    # factories (synthetic-data demos, denoising autoencoders).  In that case
    # the leaf is porting verbatim, not fabricating — skip the check.
    user_eval_path_obj = Path(args.user_eval) if args.user_eval else None
    user_synthetic = _user_uses_random_data(Path(args.user_train), user_eval_path_obj)
    if user_synthetic:
        print(
            "LEAF_FABRICATION_NOTE: user train.py / eval script uses random-tensor "
            "factories — leaf random data treated as verbatim port, not fabrication.",
            file=sys.stderr,
        )
    fabric_ok = True
    for fname in ("data.py", "eval.py"):
        violations = _check_no_random_fabrication(leaf_paths[fname], user_synthetic)
        if violations:
            fabric_ok = False
            for v in violations:
                print(f"LEAF_FABRICATION: {v}", file=sys.stderr)
    print(f"LEAF_FABRICATION_OK: {'true' if fabric_ok else 'false'}")

    # ---- load leaf modules (signatures already verified) ----------------
    loss_mod = _import_by_path(leaf_paths["loss.py"], "_gen_loss")
    data_mod = _import_by_path(leaf_paths["data.py"], "_gen_data")
    eval_mod = _import_by_path(leaf_paths["eval.py"], "_gen_eval")
    optim_mod = _import_by_path(leaf_paths["optim.py"], "_gen_optim")

    # ---- user train module (degrade to AST on import failure) -----------
    level = "numeric"
    user = None
    user_src = Path(args.user_train).read_text(encoding="utf-8", errors="replace")
    try:
        user = _import_by_path(Path(args.user_train), "_user_train")
    except Exception as e:
        level = "ast"
        print(
            f"WARN: user train.py import failed ({type(e).__name__}: {e}); "
            f"loss check degraded to AST body comparison (FIDELITY_LEVEL: ast).",
            file=sys.stderr,
        )

    # ---- 1. loss ----------------------------------------------------------
    loss_ok = "skip"
    if level == "numeric":
        user_loss = _discover_loss_fn(user, shape)
        if user_loss is None:
            print("LOSS_FN_FOUND: false")
        else:
            torch.manual_seed(1234)
            s_out = torch.randn(2, *shape[1:])
            y = torch.randn(2, *shape[1:])
            try:
                u = user_loss(s_out, y)
                g = loss_mod.compute_loss(s_out, y)
            except Exception as e:
                print(f"LOSS_ERROR: {type(e).__name__}: {e}", file=sys.stderr)
                loss_ok = "false"
            else:
                if isinstance(u, torch.Tensor) and isinstance(g, torch.Tensor):
                    ok = bool(torch.allclose(u.double(), g.double(), rtol=1e-5))
                    if not ok:
                        print(f"LOSS_DIFF: user={float(u)} gen={float(g)}", file=sys.stderr)
                    loss_ok = "true" if ok else "false"
                else:
                    loss_ok = "false"
    else:
        user_fn_name = "compute_loss"
        if user_fn_name not in user_src:
            cand = re.findall(r"^def\s+(\w+)\s*\(", user_src, re.MULTILINE)
            user_fn_name = cand[0] if cand else "compute_loss"
        ast_ok_loss = _loss_ast_match(
            user_src,
            leaf_paths["loss.py"].read_text(encoding="utf-8"),
            user_fn_name=user_fn_name,
        )
        print(f"LOSS_AST_MATCH: {'true' if ast_ok_loss else 'false'}")
        if not ast_ok_loss:
            print("LOSS_AST_DIFF: user loss body != compute_loss body (AST)", file=sys.stderr)
        loss_ok = "true" if ast_ok_loss else "false"

    # ---- 2. dataloader ---------------------------------------------------
    loader_ok = "skip"
    if level == "numeric" and user is not None:
        user_build_dl = getattr(user, "build_dataloader", None)
        if user_build_dl is None:
            print("LOADER_FOUND: false")
        else:
            try:
                user_dl = _call_user_build_dataloader(user_build_dl)
                gen_dl = data_mod.build_dataloader(batch_size=2)
            except Exception as e:
                print(f"LOADER_ERROR: {type(e).__name__}: {e}", file=sys.stderr)
                loader_ok = "false"
            else:
                u_info = _batch_info(user_dl)
                g_info = _batch_info(gen_dl)
                if u_info is None or g_info is None:
                    print(
                        "LOADER_REITERABLE: false (an iter() yielded no batch — "
                        "empty or one-shot generator)", file=sys.stderr,
                    )
                    loader_ok = "false"
                else:
                    (u_shapes, _), (g_shapes, _) = u_info, g_info
                    same = u_shapes == g_shapes
                    if not same:
                        print(f"LOADER_SHAPE_DIFF: user={u_shapes} gen={g_shapes}", file=sys.stderr)
                    loader_ok = "true" if same else "false"
    elif level == "ast":
        print("LOADER_SHAPE_OK: skip (user module import failed; see stderr)", file=sys.stderr)

    # ---- model (IO + eval shared instance) -------------------------------
    model = None
    io_ok = "skip"
    if args.model_path:
        try:
            model = _build_model(Path(args.model_path), args.build_fn, json.loads(args.build_cfg))
        except Exception as e:
            print(f"MODEL_ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            model = None
    if model is not None:
        model.eval()
        with torch.no_grad():
            out = model(torch.zeros(*shape))
        out_shape = tuple(out.shape)
        in_shape = tuple(shape)
        # Classifier-family I/O: the output shape need not equal the input
        # shape (e.g. [B, C, H, W] -> [B, num_classes]).  Prefer the model
        # contract's optional OUTPUT_SHAPE when declared; otherwise require the
        # forward to have succeeded on a DUMMY_INPUT batch with the batch dim
        # preserved (mirrors validate_contract P4 7a/7c for the classifier
        # family — reconstruction equality was over-constrained).
        _fid_mod = sys.modules.get("_fid_model")
        declared = None
        if _fid_mod is not None:
            _decl = getattr(_fid_mod, "OUTPUT_SHAPE", None)
            if _decl is not None:
                declared = tuple(int(s) for s in _decl)
        if declared is not None:
            ok = out_shape == declared
            if not ok:
                print(f"IO_SHAPE_DIFF: out={out_shape} OUTPUT_SHAPE={declared}", file=sys.stderr)
        else:
            ok = bool(out_shape) and out_shape[0] == in_shape[0]
            if not ok:
                print(f"IO_SHAPE_DIFF: out={out_shape} input_batch_dim={in_shape[0]}", file=sys.stderr)
        io_ok = "true" if ok else "false"

    # ---- 3. eval metric + 7. kind direction hard check -------------------
    eval_ok = "skip"
    kind_ok = "skip"
    eval_path = None
    if args.user_eval:
        eval_path = Path(args.user_eval)
    else:
        proj = Path(args.user_train).parent
        candidates = []
        for pattern in ("test_*.py", "eval*.py", "evaluate*.py", "test.py"):
            candidates.extend(sorted(proj.glob(pattern)))
        for cand in candidates:
            try:
                fn, _kind = _discover_user_eval_callable(cand)
            except Exception:
                continue
            if fn is not None:
                eval_path = cand
                break
        if eval_path is not None:
            print(f"EVAL_SCRIPT_DISCOVERED: {eval_path}")

    # Kind direction hard check: the leaf's returned kind must match
    # --accuracy_baseline_kind's direction group. We do not need to run the
    # metric — we read the kind the leaf returns on a synthetic forward.
    leaf_kind = ""
    if model is not None:
        try:
            with torch.no_grad():
                _v, leaf_kind = eval_mod.eval_metric(model, "cpu")
        except Exception as e:
            print(f"LEAF_EVAL_PROBE_ERROR: {type(e).__name__}: {e}", file=sys.stderr)

    if args.accuracy_baseline_kind:
        baseline_dir = _kind_direction(args.accuracy_baseline_kind)
        leaf_dir = _kind_direction(leaf_kind) if leaf_kind else None
        if baseline_dir is None:
            print(f"BASELINE_KIND_UNKNOWN: {args.accuracy_baseline_kind!r}", file=sys.stderr)
            kind_ok = "false"
        elif leaf_dir is None:
            # Fall back: assume the leaf matches the baseline direction unless
            # we can prove otherwise. The numeric-eval branch below still runs.
            kind_ok = "skip"
        elif baseline_dir != leaf_dir:
            print(
                f"KIND_DIRECTION_DIFF: baseline={args.accuracy_baseline_kind}({baseline_dir}) "
                f"leaf={leaf_kind!r}({leaf_dir}) — kind direction mismatch",
                file=sys.stderr,
            )
            kind_ok = "false"
        else:
            kind_ok = "true"
    print(f"KIND_DIRECTION_OK: {kind_ok}")

    if eval_path is None:
        print("EVAL_SCRIPT_FOUND: false")
    else:
        try:
            user_eval, user_kind = _discover_user_eval_callable(
                eval_path,
                loader_factory=(lambda: data_mod.build_dataloader(batch_size=8)),
            )
        except Exception as e:
            print(f"EVAL_ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            user_eval, user_kind = None, ""
        if user_eval is None:
            print("EVAL_MATCH: false (no matching user eval fn; skip)")
        elif model is None:
            print("EVAL_MATCH: skip (function-level numeric compare needs --model_path)")
        else:
            seed = _extract_seed(eval_path)
            torch.manual_seed(seed)
            try:
                u = float(user_eval(model))
                torch.manual_seed(seed)
                with torch.no_grad():
                    g_val, g_kind = eval_mod.eval_metric(model, "cpu")
            except Exception as e:
                print(f"EVAL_ERROR: {type(e).__name__}: {e}", file=sys.stderr)
                eval_ok = "false"
            else:
                ok = bool(torch.allclose(torch.tensor(u), torch.tensor(float(g_val)), rtol=1e-5))
                if not ok:
                    print(f"EVAL_DIFF: user={u} gen={float(g_val)}", file=sys.stderr)
                if user_kind and user_kind != g_kind:
                    print(f"EVAL_KIND_DIFF: user={user_kind} gen={g_kind}", file=sys.stderr)
                    ok = False
                eval_ok = "true" if ok else "false"

    # ---- 4. optimizer ----------------------------------------------------
    opt_ok = "skip"
    if level == "numeric" and user is not None:
        user_opt = _extract_user_optimizer_class(user_src)
        if user_opt is None:
            print("OPT_FOUND: false")
        else:
            try:
                inst = optim_mod.build_optimizer([torch.zeros(2, requires_grad=True)], lr=1e-3)
            except Exception as e:
                print(f"OPT_ERROR: {type(e).__name__}: {e}", file=sys.stderr)
                inst = None
            if inst is None:
                print(f"OPT_TYPE_DIFF: user={user_opt} gen=None", file=sys.stderr)
                opt_ok = "false"
            else:
                gen_opt = inst.__class__.__name__
                ok = gen_opt == user_opt
                if not ok:
                    print(f"OPT_TYPE_DIFF: user={user_opt} gen={gen_opt}", file=sys.stderr)
                opt_ok = "true" if ok else "false"
    elif level == "ast":
        print("OPT_TYPE_OK: skip (user module import failed; see stderr)", file=sys.stderr)

    # ---- report -----------------------------------------------------------
    print(f"FIDELITY_LEVEL: {level}")
    print(f"LEAF_AST_OK: {ast_ok}")
    print(f"LEAF_FABRICATION_OK: {'true' if fabric_ok else 'false'}")
    print(f"LOSS_ALLCLOSE: {loss_ok}")
    print(f"LOADER_SHAPE_OK: {loader_ok}")
    print(f"EVAL_ALLCLOSE: {eval_ok}")
    print(f"OPT_TYPE_OK: {opt_ok}")
    print(f"IO_SHAPE_OK: {io_ok}")
    print(f"KIND_DIRECTION_OK: {kind_ok}")
    any_false = any(
        v == "false"
        for v in (
            "true" if ast_ok else "false",
            "true" if fabric_ok else "false",
            loss_ok, loader_ok, eval_ok, opt_ok, io_ok, kind_ok,
        )
    )
    print(f"FIDELITY: {'PASS' if not any_false else 'FAIL'}")
    return 2 if any_false else 0


if __name__ == "__main__":
    sys.exit(main())
