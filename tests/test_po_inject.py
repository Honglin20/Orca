"""test_po_inject.py — shadow injection via sitecustomize meta path finder.

Exercises the injection in its REAL invocation form: a `python train.py` entry
script run with the injection header exported (PYTHONPATH = inject_dir +
project_root, ORCA_SHADOW_DIR / ORCA_SHADOW_PKGS). Covers: shadow precedence
for packages AND submodules, bare-module sibling closure, the stdlib guard
(a shadow module named like a stdlib module must NOT hijack it), assert_shadow
success/failure paths, and the fact that `python -S` kills injection (the
contract-time flag probe relies on this being observable).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_INJECT_SRC = _REPO / "workflows" / "prof-opt" / "agents" / "_po_scripts" / "orca_inject"
_SCRIPTS_SRC = _REPO / "workflows" / "prof-opt" / "agents" / "_po_scripts"


def _build_workspace(tmp_path: Path) -> dict[str, Path]:
    """shadow with a package + submodule, a bare sibling module, and a
    stdlib-named module; project with a train.py entry; orca_inject deployed."""
    shadow = tmp_path / "shadow"
    pkg = shadow / "demo_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("TAG = 'shadow'\n", encoding="utf-8")
    (pkg / "sub.py").write_text("TAG = 'shadow-sub'\n", encoding="utf-8")
    (shadow / "bare_mod.py").write_text("TAG = 'shadow-bare'\n", encoding="utf-8")
    # stdlib collision: a module named exactly like a stdlib one
    (shadow / "json.py").write_text("TAG = 'shadow-json'\n", encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir()
    train = project / "train.py"
    train.write_text(
        "import json, sys\n"
        "out = {}\n"
        "import demo_pkg, demo_pkg.sub, bare_mod\n"
        "out['demo_pkg'] = demo_pkg.__file__\n"
        "out['demo_pkg.sub'] = demo_pkg.sub.__file__\n"
        "out['bare_mod'] = bare_mod.__file__\n"
        "out['json'] = json.__file__\n"
        "out['json_tag'] = getattr(json, 'TAG', 'stdlib')\n"
        "print(json.dumps(out))\n",
        encoding="utf-8")

    inject = tmp_path / "orca_inject"
    inject.mkdir()
    (inject / "sitecustomize.py").write_text(
        (_INJECT_SRC / "sitecustomize.py").read_text(encoding="utf-8"), encoding="utf-8")
    return {"shadow": shadow, "project": project, "train": train, "inject": inject}


def _run_entry(ws: dict[str, Path], pkgs: str, *extra_flags: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ORCA_SHADOW_DIR"] = str(ws["shadow"])
    env["ORCA_SHADOW_PKGS"] = pkgs
    env["PYTHONPATH"] = os.pathsep.join([str(ws["inject"]), str(ws["project"])])
    env.pop("PYTHONSTARTUP", None)
    cmd = [sys.executable, *extra_flags, str(ws["train"])]
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          cwd=str(ws["project"]), timeout=60)


def test_shadow_package_and_submodule_take_precedence(tmp_path: Path):
    ws = _build_workspace(tmp_path)
    proc = _run_entry(ws, "demo_pkg,bare_mod,json")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.splitlines()[-1])
    shadow_root = str(ws["shadow"].resolve())

    for key in ("demo_pkg", "demo_pkg.sub", "bare_mod"):
        resolved = str(Path(out[key]).resolve())
        assert resolved.startswith(shadow_root + os.sep), (key, resolved)
    # stdlib guard: json must resolve to the real stdlib, not the shadow copy
    assert not out["json"].startswith(shadow_root)
    assert out["json_tag"] == "stdlib"


def test_assert_shadow_ok_and_fail(tmp_path: Path):
    ws = _build_workspace(tmp_path)
    assert_script = _SCRIPTS_SRC / "assert_shadow.py"

    env = dict(os.environ)
    env["ORCA_SHADOW_DIR"] = str(ws["shadow"])
    env["PYTHONPATH"] = os.pathsep.join([str(ws["inject"]), str(ws["project"])])

    # all names resolvable inside the shadow -> ok
    env["ORCA_SHADOW_PKGS"] = "demo_pkg,bare_mod"
    ok = subprocess.run([sys.executable, str(assert_script)],
                        capture_output=True, text=True, env=env,
                        cwd=str(ws["project"]), timeout=60)
    assert ok.returncode == 0, ok.stderr
    payload = json.loads(ok.stdout.splitlines()[-1])
    assert set(payload["resolved"]) == {"demo_pkg", "bare_mod"}

    # stdlib-named module in the list -> resolves to stdlib -> must fail loud
    env["ORCA_SHADOW_PKGS"] = "demo_pkg,json"
    bad = subprocess.run([sys.executable, str(assert_script)],
                         capture_output=True, text=True, env=env,
                         cwd=str(ws["project"]), timeout=60)
    assert bad.returncode != 0
    assert "json" in bad.stderr

    # name absent from the shadow -> import error -> must fail loud
    env["ORCA_SHADOW_PKGS"] = "demo_pkg,ghost_mod"
    ghost = subprocess.run([sys.executable, str(assert_script)],
                           capture_output=True, text=True, env=env,
                           cwd=str(ws["project"]), timeout=60)
    assert ghost.returncode != 0
    assert "ghost_mod" in ghost.stderr


def test_injection_missing_env_is_noop_for_shadow(tmp_path: Path):
    """Without ORCA_SHADOW_* env the finder never installs: plain behavior."""
    ws = _build_workspace(tmp_path)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ws["inject"]), str(ws["project"])])
    env.pop("ORCA_SHADOW_DIR", None)
    env.pop("ORCA_SHADOW_PKGS", None)
    proc = subprocess.run([sys.executable, str(ws["train"])],
                          capture_output=True, text=True, env=env,
                          cwd=str(ws["project"]), timeout=60)
    assert proc.returncode != 0  # demo_pkg not importable without the finder
    assert "ModuleNotFoundError" in proc.stderr


def test_python_S_flag_kills_injection(tmp_path: Path):
    """`-S` skips site processing entirely, so sitecustomize never runs: the
    injection is observably dead and the entry fails loud (this is exactly the
    signal the contract-time interpreter-flag probe looks for)."""
    ws = _build_workspace(tmp_path)
    proc = _run_entry(ws, "demo_pkg,bare_mod", "-S")
    assert proc.returncode != 0
    assert "ModuleNotFoundError" in proc.stderr
