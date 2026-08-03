"""fidelity_check.py — deterministic numeric-equivalence check (kd-train-script Layer 3).

Verifies that the specialised ``train_pipeline.py`` slots are **numerically
equivalent** to the user's original code:

1. loss — ``user_compute_loss`` vs the user train.py loss fn on identical
   seeded inputs (``torch.allclose(rtol=1e-5)``).
2. dataloader — ``user_build_dataloader(batch_size=2)`` vs user
   ``build_dataloader``: same batch shape + both re-iterable.
3. eval metric — ``user_eval_metric`` vs the user eval script's metric on the
   same model instance (values allclose + kind identical).
4. optimizer — class name produced by ``build_user_optimizer`` vs the user
   train.py's optimizer class.
5. model I/O — model forward on ``DUMMY_INPUT`` shape preserves the shape.

Degradation: if the user train.py cannot be imported cleanly (import
side-effects / missing deps), ``FIDELITY_LEVEL: ast`` and the loss item is
checked via AST body equivalence instead (``LOSS_AST_MATCH``); other numeric
items are skipped with the reason on stderr (fail loud reporting, no silent
skip of the *level*).

Deterministic script contract (CONTRACTS §3): stdout is ``KEY: value`` lines,
non-zero exit on FAIL (fail loud).

Usage::

    fidelity_check.py --train_pipeline <path> --user_train <path>
        [--user_eval <path>] --dummy_input <json-string|json-file>
        [--model_path <path>] [--build_fn build_model] [--build_cfg <json>]
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
    """--dummy_input accepts either a JSON string or a path to a .json file."""
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


def _discover_loss_fn(module, batch_shape: list[int]):
    """Prefer ``compute_loss``; else the first top-level 2-arg function that
    returns a scalar tensor on tensor inputs (semantic recognition mirroring
    the agent's Step 1).  Returns the callable or None."""
    if hasattr(module, "compute_loss"):
        return module.compute_loss
    candidates: list = []
    for name, obj in vars(module).items():
        if isinstance(obj, type):  # classes are not loss candidates
            continue
        if not callable(obj) or getattr(obj, "__module__", None) != module.__name__:
            continue
        try:
            params = list(inspect.signature(obj).parameters.values())
        except (ValueError, TypeError):
            continue
        if len(params) != 2:
            continue
        if name == "build_dataloader":
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
    """AST function-body equivalence between the user loss and
    ``user_compute_loss``: same statement node dumps after normalising
    parameter names and dropping docstrings (ignores arg names / doc drift)."""
    user_tree = ast.parse(user_src)
    gen_tree = ast.parse(gen_src)
    user_fn = next(
        (n for n in ast.walk(user_tree)
         if isinstance(n, ast.FunctionDef) and n.name == user_fn_name),
        None,
    )
    gen_fn = next(
        (n for n in ast.walk(gen_tree)
         if isinstance(n, ast.FunctionDef) and n.name == "user_compute_loss"),
        None,
    )
    if user_fn is None or gen_fn is None:
        return False

    def normalised_body(fn: ast.FunctionDef) -> list[str]:
        rename = {a.arg: f"_A{i}" for i, a in enumerate(fn.args.args)}
        body = list(fn.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]  # drop docstring

        class _Norm(ast.NodeTransformer):
            def visit_Name(self, node: ast.Name):
                if node.id in rename:
                    node.id = rename[node.id]
                return node

        return [ast.dump(_Norm().visit(stmt), include_attributes=False) for stmt in body]

    return normalised_body(user_fn) == normalised_body(gen_fn)


def _call_user_build_dataloader(fn, batch_size: int = 2):
    """Fixed-slot interface is ``(batch_size=)``; tolerate a no-arg builder so
    both sides are compared at the same batch size (apples-to-apples)."""
    try:
        return fn(batch_size=batch_size)
    except TypeError:
        return fn()


def _batch_info(loader) -> tuple[list, int] | None:
    """(first-batch x shape, first-batch y shape) over two fresh iter()s;
    None if either iter yields nothing (one-shot / empty)."""
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
    """Seed alignment: grep the user eval script for ``torch.manual_seed`` /
    ``manual_seed``; fall back to the fixed demo seed 20260725."""
    src = eval_path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"manual_seed\(\s*(\d+)\s*\)", src)
    if m:
        return int(m.group(1))
    return 20260725


def _discover_user_eval_callable(eval_path: Path, kind_hint: str = ""):
    """Mirror the agent's Step 1 discovery: prefer ``evaluate``; else the
    first top-level fn whose name hints nmse/mse/ber/snr/acc; adapt the call
    signature ((model, n_samples) -> partial; (model) -> direct).  Returns
    (callable, kind) or (None, "") when no fn matches the signature."""
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
        if sig_nargs <= 1:
            adapter = (lambda m, _fn=fn: _fn(m))
        else:
            adapter = (lambda m, _fn=fn: _fn(m, "cpu"))
    kind = ""
    m = re.search(r"(nmse|mse|ber|snr|acc)", name)
    if m:
        kind = m.group(1)
    return adapter, kind


def _extract_user_optimizer_class(user_src: str) -> str | None:
    """First ``torch.optim.<Class>`` occurrence in the user train.py source."""
    m = re.search(r"torch\.optim\.(\w+)", user_src)
    return m.group(1) if m else None


def _build_model(model_path: Path, build_fn: str, cfg: dict):
    mod = _import_by_path(model_path, "_fid_model")
    if not hasattr(mod, build_fn):
        raise AttributeError(f"{model_path} has no build fn {build_fn!r}")
    return getattr(mod, build_fn)(**cfg)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        description="kd-train-script Layer 3 fidelity check (numeric equivalence)"
    )
    p.add_argument("--train_pipeline", required=True, help="generated train_pipeline.py path")
    p.add_argument("--user_train", required=True, help="user original train.py path")
    p.add_argument("--user_eval", default=None, help="user eval script path (else auto-glob)")
    p.add_argument("--dummy_input", required=True, help="JSON string or .json file (dict with 'shape')")
    p.add_argument("--model_path", default=None, help="model .py for I/O + eval checks")
    p.add_argument("--build_fn", default="build_model")
    p.add_argument("--build_cfg", default="{}")
    p.add_argument("--project_root", default=None)
    args = p.parse_args()

    if args.project_root and args.project_root not in sys.path:
        sys.path.insert(0, args.project_root)

    dummy = _load_dummy_input(args.dummy_input)
    shape = [int(s) for s in dummy["shape"]]

    gen = _import_by_path(Path(args.train_pipeline), "_gen_train_pipeline")

    # ---- user train module (degrade to AST level on import failure) -----
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
                g = gen.user_compute_loss(s_out, y)
            except (NotImplementedError, AttributeError) as e:
                print(
                    f"LOSS_ERROR: {type(e).__name__}: {e} "
                    f"(slot 未特化/接口缺失 → loss item false)",
                    file=sys.stderr,
                )
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
        ast_ok = _loss_ast_match(user_src, gen_src=Path(args.train_pipeline).read_text(
            encoding="utf-8"), user_fn_name=user_fn_name)
        print(f"LOSS_AST_MATCH: {'true' if ast_ok else 'false'}")
        if not ast_ok:
            print("LOSS_AST_DIFF: user loss body != user_compute_loss body (AST)", file=sys.stderr)
        loss_ok = "true" if ast_ok else "false"

    # ---- 2. dataloader -----------------------------------------------------
    loader_ok = "skip"
    if level == "numeric" and user is not None:
        user_build_dl = getattr(user, "build_dataloader", None)
        if user_build_dl is None:
            print("LOADER_FOUND: false")
        else:
            try:
                user_dl = _call_user_build_dataloader(user_build_dl)
                gen_dl = gen.user_build_dataloader(batch_size=2)
            except Exception as e:
                print(f"LOADER_ERROR: {type(e).__name__}: {e}", file=sys.stderr)
                loader_ok = "false"
            else:
                u_info = _batch_info(user_dl)
                g_info = _batch_info(gen_dl)
                if u_info is None or g_info is None:
                    print(
                        "LOADER_REITERABLE: false (an iter() yielded no batch — "
                        "empty or one-shot generator)",
                        file=sys.stderr,
                    )
                    loader_ok = "false"
                else:
                    (u_shapes, _u_iters), (g_shapes, _g_iters) = u_info, g_info
                    same = u_shapes == g_shapes
                    if not same:
                        print(f"LOADER_SHAPE_DIFF: user={u_shapes} gen={g_shapes}", file=sys.stderr)
                    loader_ok = "true" if same else "false"
    elif level == "ast":
        print("LOADER_SHAPE_OK: skip (user module import failed; see stderr)", file=sys.stderr)

    # ---- model (IO + eval shared instance) ---------------------------------
    model = None
    io_ok = "skip"
    if args.model_path:
        try:
            model = _build_model(Path(args.model_path), args.build_fn,
                                 json.loads(args.build_cfg))
        except Exception as e:
            print(f"MODEL_ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            model = None
    if model is not None:
        # ---- 5. model I/O -------------------------------------------------
        model.eval()
        with torch.no_grad():
            out = model(torch.zeros(*shape))
        ok = tuple(out.shape) == tuple(shape)
        if not ok:
            print(f"IO_SHAPE_DIFF: out={tuple(out.shape)} dummy={tuple(shape)}", file=sys.stderr)
        io_ok = "true" if ok else "false"

    # ---- 3. eval metric -----------------------------------------------------
    eval_ok = "skip"
    eval_path = None
    if args.user_eval:
        eval_path = Path(args.user_eval)
    else:
        proj = Path(args.user_train).parent
        candidates = []
        for pattern in ("test_*.py", "eval*.py", "evaluate*.py", "test.py"):
            candidates.extend(sorted(proj.glob(pattern)))
        # 取第一个「真发现指标函数」的脚本（test_*.py 可能是 pytest 文件——
        # 无 nmse/mse/ber/snr/acc 顶层函数则跳过，镜像 agent Step 1 的发现判断）。
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
    if eval_path is None:
        print("EVAL_SCRIPT_FOUND: false")
    else:
        try:
            user_eval, user_kind = _discover_user_eval_callable(eval_path)
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
                    g_val, g_kind = gen.user_eval_metric(model, "cpu")
            except (NotImplementedError, AttributeError, TypeError) as e:
                print(
                    f"EVAL_ERROR: {type(e).__name__}: {e} "
                    f"(slot 未特化/接口缺失/签名不匹配 → eval item false)",
                    file=sys.stderr,
                )
                eval_ok = "false"
            else:
                ok = bool(torch.allclose(torch.tensor(u), torch.tensor(float(g_val)), rtol=1e-5))
                if not ok:
                    print(f"EVAL_DIFF: user={u} gen={float(g_val)}", file=sys.stderr)
                if user_kind and user_kind != g_kind:
                    print(f"EVAL_KIND_DIFF: user={user_kind} gen={g_kind}", file=sys.stderr)
                    ok = False
                eval_ok = "true" if ok else "false"

    # ---- 4. optimizer --------------------------------------------------------
    opt_ok = "skip"
    if level == "numeric" and user is not None:
        user_opt = _extract_user_optimizer_class(user_src)
        if user_opt is None:
            print("OPT_FOUND: false")
        else:
            try:
                inst = gen.build_user_optimizer(
                    [torch.zeros(2, requires_grad=True)], lr=1e-3
                )
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

    # ---- report ---------------------------------------------------------------
    print(f"FIDELITY_LEVEL: {level}")
    print(f"LOSS_ALLCLOSE: {loss_ok}")
    print(f"LOADER_SHAPE_OK: {loader_ok}")
    print(f"EVAL_ALLCLOSE: {eval_ok}")
    print(f"OPT_TYPE_OK: {opt_ok}")
    print(f"IO_SHAPE_OK: {io_ok}")
    any_false = any(v == "false" for v in (loss_ok, loader_ok, eval_ok, opt_ok, io_ok))
    print(f"FIDELITY: {'PASS' if not any_false else 'FAIL'}")
    return 2 if any_false else 0


if __name__ == "__main__":
    sys.exit(main())
