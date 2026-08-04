"""kd._leaves — single-file leaf loader for the KDTrainer engine.

Aligned with the plan §3.2 (v3.2) leaf contract.  Four leaves live under
``<artifacts_dir>/user/``:

* ``loss.py``  → ``compute_loss(s_out, y)``
* ``data.py``  → ``build_dataloader(batch_size)``
* ``eval.py``  → ``eval_metric(student, device) -> (value, kind)``
* ``optim.py`` → ``build_optimizer(params, lr)`` and ``build_scheduler(optimizer, epochs)``

Design (decision D9-c, plan §3.2):

* **No sys.path injection** (Q6) — each leaf is loaded via
  :func:`importlib.util.spec_from_file_location` as its own module so sibling
  helpers / relative imports cannot leak across files.
* **Eager validation** at load time: file existence + AST signature
  (function name + required positional params equal, defaults additive; E9)
  + AST self-containment deny-list (no relative imports, no sibling imports
  outside the whitelisted stdlib/torch subset).  Failures raise
  :class:`LeafContractError` before any exec.
* **Lazy exec** of the leaf body: the module is ``exec_module``-ed the first
  time one of its callables is invoked (D9-c).  Exec failures are wrapped in
  :class:`LeafExecError` carrying filename + line.

Error contract (B7):

* Missing file → :class:`FileNotFoundError` with the leaf name.
* Exec failure → :class:`LeafExecError` wrapping the original exception with
  ``filename:lineno`` context.
"""

from __future__ import annotations

import ast
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Whitelist of modules a leaf may import.  Leaves must be self-contained —
# they cannot reach into sibling KD-NAS scripts or the user's project via
# sys.path.  Stdlib + torch + numpy are enough for any reasonable loss / eval
# / optim definition; anything else is a sign the leaf is not self-contained.
_LEAF_IMPORT_WHITELIST: frozenset[str] = frozenset(
    {
        "torch",
        "math",
        "numpy",
        "typing",
        "itertools",
        "functools",
        "collections",
        "dataclasses",
        "random",
    }
)


# Required positional argument names per leaf callable.  Defaults are
# additive (a leaf may add extra defaulted kwargs but cannot drop these).
_LEAF_SIGNATURES: dict[tuple[str, str], list[str]] = {
    ("loss.py", "compute_loss"): ["s_out", "y"],
    ("data.py", "build_dataloader"): ["batch_size"],
    ("eval.py", "eval_metric"): ["student", "device"],
    ("optim.py", "build_optimizer"): ["params", "lr"],
    ("optim.py", "build_scheduler"): ["optimizer", "epochs"],
}


class LeafContractError(RuntimeError):
    """Leaf failed static validation (missing file, bad signature, illegal import)."""


class LeafExecError(RuntimeError):
    """Leaf body raised during exec / first-call.  Wraps the original exception."""


# ---------------------------------------------------------------------------
# AST validation
# ---------------------------------------------------------------------------
def _check_self_contained(tree: ast.AST, path: Path) -> None:
    """Reject imports outside the whitelist (Q6).  Relative imports always fail."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                raise LeafContractError(
                    f"{path}: relative import (level={node.level}) forbidden — "
                    f"leaf must be self-contained (no sibling helpers)"
                )
            if node.module and node.module.split(".")[0] not in _LEAF_IMPORT_WHITELIST:
                raise LeafContractError(
                    f"{path}: import from {node.module!r} not in whitelist "
                    f"{sorted(_LEAF_IMPORT_WHITELIST)} — leaf must be self-contained"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in _LEAF_IMPORT_WHITELIST:
                    raise LeafContractError(
                        f"{path}: import {alias.name!r} not in whitelist "
                        f"{sorted(_LEAF_IMPORT_WHITELIST)} — leaf must be self-contained"
                    )


def _required_args(fn: ast.FunctionDef) -> list[str]:
    """Positional args that have no default (defaults attach to the tail)."""
    args = fn.args.args
    ndef = len(fn.args.defaults)
    return [a.arg for a in args[: len(args) - ndef]]


def _check_signature(tree: ast.AST, path: Path, fname: str, expected_required: list[str]) -> None:
    """Function must exist + required positional param names match exactly (E9).

    Defaults are additive: extra optional params are fine, dropping or
    renaming a required param is a contract violation.
    """
    target: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == fname:
            target = node
            break
    if target is None:
        raise LeafContractError(
            f"{path}: required function {fname!r} not defined "
            f"(expected signature {fname}({', '.join(expected_required)}))"
        )
    actual = _required_args(target)
    if actual != expected_required:
        raise LeafContractError(
            f"{path}: {fname} signature mismatch — required positional args "
            f"expected {expected_required!r}, got {actual!r}. "
            f"Defaults are additive but required names must match exactly."
        )


def _ast_validate(path: Path, fname: str, expected_required: list[str]) -> ast.AST:
    try:
        src = path.read_text(encoding="utf-8")
    except OSError as e:
        raise LeafContractError(f"{path}: cannot read leaf file: {e}") from e
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        raise LeafContractError(f"{path}:{e.lineno}: syntax error: {e.msg}") from e
    _check_self_contained(tree, path)
    _check_signature(tree, path, fname, expected_required)
    return tree


# ---------------------------------------------------------------------------
# Lazy module
# ---------------------------------------------------------------------------
class _LazyModule:
    """One leaf .py file — AST-validated eagerly, body exec'd lazily on first call."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._module: Any | None = None
        # Build a fresh spec per leaf.  Unique module name prevents collisions
        # across runs / leaves with the same stem.
        self._mod_name = f"_kd_leaf_{path.stem}_{abs(hash(str(path))) & 0xFFFFFFFF:x}"
        self._spec = importlib.util.spec_from_file_location(self._mod_name, path)
        if self._spec is None or self._spec.loader is None:
            raise LeafContractError(f"{path}: cannot build import spec")

    def _exec(self) -> Any:
        if self._module is not None:
            return self._module
        try:
            mod = importlib.util.module_from_spec(self._spec)
            self._spec.loader.exec_module(mod)  # type: ignore[union-attr]
        except LeafExecError:
            raise
        except Exception as e:
            # ``exec_module`` typically embeds filename + lineno on the raised
            # exception; surface both loudly via LeafExecError wrap (B7).
            lineno = getattr(e, "lineno", None)
            msg = f"{self.path}"
            if lineno is not None:
                msg += f":{lineno}"
            raise LeafExecError(
                f"{msg}: leaf body exec failed — {type(e).__name__}: {e}",
            ) from e
        self._module = mod
        return mod

    def get(self, name: str) -> Callable[..., Any]:
        mod = self._exec()
        fn = getattr(mod, name, None)
        if not callable(fn):
            raise LeafExecError(
                f"{self.path}: attribute {name!r} missing or not callable after exec"
            )
        return fn


# ---------------------------------------------------------------------------
# Leaves facade
# ---------------------------------------------------------------------------
@dataclass
class Leaves:
    """Eager-validated, lazily-executed bundle of the four leaf callables.

    Attribute access returns a thin closure that triggers exec on first call
    (D9-c) so an unused leaf file is never exec'd.
    """

    loss_module: _LazyModule
    data_module: _LazyModule
    eval_module: _LazyModule
    optim_module: _LazyModule

    @property
    def compute_loss(self) -> Callable[..., Any]:
        return self.loss_module.get("compute_loss")

    @property
    def build_dataloader(self) -> Callable[..., Any]:
        return self.data_module.get("build_dataloader")

    @property
    def eval_metric(self) -> Callable[..., Any]:
        return self.eval_module.get("eval_metric")

    @property
    def build_optimizer(self) -> Callable[..., Any]:
        return self.optim_module.get("build_optimizer")

    @property
    def build_scheduler(self) -> Callable[..., Any]:
        return self.optim_module.get("build_scheduler")


def load(user_dir: Path | str) -> Leaves:
    """Eager-validate + lazily wrap the four leaves under ``user_dir``.

    Raises :class:`FileNotFoundError` if any leaf is missing (B7).
    """
    user_dir = Path(user_dir)
    if not user_dir.is_dir():
        raise FileNotFoundError(
            f"leaves directory not found: {user_dir} (expected user/{{loss,data,eval,optim}}.py)"
        )

    modules: dict[str, _LazyModule] = {}
    # Single-callable leaves: parse + signature-check in one shot.
    single_checks = [
        ("loss.py", "compute_loss"),
        ("data.py", "build_dataloader"),
        ("eval.py", "eval_metric"),
    ]
    for fname, fn_name in single_checks:
        path = user_dir / fname
        if not path.is_file():
            raise FileNotFoundError(f"leaf file missing: {path}")
        _ast_validate(path, fn_name, _LEAF_SIGNATURES[(fname, fn_name)])
        modules[fname] = _LazyModule(path)

    # optim.py has two callables — validate both signatures against one parse.
    optim_path = user_dir / "optim.py"
    if not optim_path.is_file():
        raise FileNotFoundError(f"leaf file missing: {optim_path}")
    tree = _ast_validate(
        optim_path,
        "build_optimizer",
        _LEAF_SIGNATURES[("optim.py", "build_optimizer")],
    )
    _check_signature(
        tree,
        optim_path,
        "build_scheduler",
        _LEAF_SIGNATURES[("optim.py", "build_scheduler")],
    )
    modules["optim.py"] = _LazyModule(optim_path)

    return Leaves(
        loss_module=modules["loss.py"],
        data_module=modules["data.py"],
        eval_module=modules["eval.py"],
        optim_module=modules["optim.py"],
    )


__all__ = [
    "Leaves",
    "LeafContractError",
    "LeafExecError",
    "load",
]
