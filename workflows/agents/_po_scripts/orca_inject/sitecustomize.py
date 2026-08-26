"""sitecustomize.py — shadow-package injection via a meta path finder.

Mechanism (empirically pinned, kept verbatim on purpose — do not "simplify"):
PYTHONPATH-only injection and sys.path.insert(0) both LOSE to the script
directory, so shadow resolution must intercept imports BEFORE sys.path is
scanned.

Env contract (set by the rendered run header, see orca_inject/header.env):
    ORCA_SHADOW_DIR   absolute path to the shadow root
    ORCA_SHADOW_PKGS  comma-separated top-level module/package names to shadow
"""
import os, sys
from importlib.machinery import PathFinder
_s = os.environ.get("ORCA_SHADOW_DIR")
_pkgs = frozenset(filter(None, os.environ.get("ORCA_SHADOW_PKGS", "").split(",")))
if _s and _pkgs:
    class _ShadowFinder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname in sys.stdlib_module_names:   # never shadow stdlib
                return None
            if "." not in fullname and fullname in _pkgs:
                return PathFinder.find_spec(fullname, path=[_s])
            return None
    sys.meta_path.insert(0, _ShadowFinder())
