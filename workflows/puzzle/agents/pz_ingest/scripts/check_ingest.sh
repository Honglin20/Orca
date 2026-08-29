#!/usr/bin/env bash
# check_ingest.sh — deterministic gate for pz_ingest artifacts.
# Runs 5 checks, prints one tagged line per check, exits non-zero if any fail.
#   0. flat + adapters present (fatal — nothing else can run without them)
#   1. py_compile flat + adapters
#   2. manifest.yaml present + parses as YAML
#   3. manifest five-section schema + adapters_entry/metric.direction/forward_calling_convention
#   4. forward-convention consistency (manifest vs adapters.FORWARD_CALLING_CONVENTION)
#   5. flat __main__ runs and prints an output shape
set -uo pipefail

ARTIFACTS_DIR="${ORCA_ARTIFACTS_DIR:-$(pwd)}"
FAILS=0

ok()   { echo "[check_ingest] OK   $1"; }
fail() { echo "[check_ingest] FAIL $1"; FAILS=$((FAILS + 1)); }

cd "$ARTIFACTS_DIR" || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

# ── 0. flat + adapters + manifest present ─────────────────────────────────
FLAT="$(ls *_flat.py 2>/dev/null | head -1)"
ADAPTERS="puzzle_adapters.py"
MANIFEST="manifest.yaml"

if [ -z "$FLAT" ]; then
  fail "<base>_flat.py missing"
  echo "[check_ingest] result: FAIL"
  exit 1
fi
[ -s "$ADAPTERS" ] || fail "puzzle_adapters.py missing or empty"
[ -s "$MANIFEST" ] || fail "manifest.yaml missing or empty"
ok "flat ($FLAT) + adapters + manifest present"

# ── 1. py_compile flat + adapters ──────────────────────────────────────────
PY_RC=0
python3 -m py_compile "$FLAT" 2>/dev/null || PY_RC=$?
if [ "$PY_RC" -eq 0 ]; then
  ok "flat py_compile"
else
  fail "flat py_compile failed"
fi

PY_RC=0
python3 -m py_compile "$ADAPTERS" 2>/dev/null || PY_RC=$?
if [ "$PY_RC" -eq 0 ]; then
  ok "adapters py_compile"
else
  fail "adapters py_compile failed"
fi

# ── 2 + 3. manifest five-section schema + bridge fields ─────────────────────
# Single python invocation parses YAML once and asserts everything.
MANIFEST_RC=0
python3 - "$MANIFEST" 2>/dev/null <<'PY' || MANIFEST_RC=$?
import sys, yaml
m = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
assert isinstance(m, dict), f"manifest top-level must be mapping, got {type(m).__name__}"

required_sections = [
    "project_overview", "model", "training_and_evaluation",
    "data_and_environment", "relevant_source_files",
]
for sec in required_sections:
    assert sec in m, f"manifest missing section {sec!r}"

t = m["training_and_evaluation"]
assert "adapters_entry" in t and t["adapters_entry"], "training_and_evaluation.adapters_entry missing/empty"
assert t["adapters_entry"] == "puzzle_adapters.py", f"adapters_entry must == 'puzzle_adapters.py', got {t['adapters_entry']!r}"
assert "metric" in t and "direction" in t["metric"], "training_and_evaluation.metric.direction missing"
assert t["metric"]["direction"] in ("higher-better", "lower-better"), f"metric.direction invalid: {t['metric']['direction']!r}"
assert "forward_calling_convention" in t, "training_and_evaluation.forward_calling_convention missing"
assert t["forward_calling_convention"] in ("positional", "dict", "single"), f"forward_calling_convention invalid: {t['forward_calling_convention']!r}"
assert "eval_noise_atol" in t, "training_and_evaluation.eval_noise_atol missing"

# retired fields must not appear
for retired in ("eval_kind", "evaluation_entry", "data_loader_entry"):
    assert retired not in t, f"retired field {retired!r} still present in training_and_evaluation"
assert "eval_nondeterministic" not in t, "retired field 'eval_nondeterministic' (use eval_noise_atol)"

mod = m["model"]
assert "build_entry" in mod and mod["build_entry"], "model.build_entry missing/empty"

print("manifest schema OK")
PY

if [ "$MANIFEST_RC" -eq 0 ]; then
  ok "manifest five-section schema + bridge fields"
else
  fail "manifest schema check failed (run the inline python above to see which assertion)"
fi

# ── 4. forward-convention consistency (manifest vs adapters) ────────────────
CONV_RC=0
python3 - "$MANIFEST" "$ADAPTERS" 2>/dev/null <<'PY' || CONV_RC=$?
import sys, yaml, importlib.util, pathlib
manifest = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
manifest_conv = manifest["training_and_evaluation"]["forward_calling_convention"]
manifest_dir = manifest["training_and_evaluation"]["metric"]["direction"]
manifest_atol = float(manifest["training_and_evaluation"]["eval_noise_atol"])

# import the adapter module by file path (do not exec flat — it may import torch heavies)
spec = importlib.util.spec_from_file_location("_puzzle_adapters_check", sys.argv[2])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

adapter_conv = getattr(mod, "FORWARD_CALLING_CONVENTION")
adapter_dir = getattr(mod, "METRIC_DIRECTION")
adapter_atol = float(getattr(mod, "EVAL_NOISE_ATOL"))

assert adapter_conv == manifest_conv, f"FORWARD_CALLING_CONVENTION mismatch: manifest={manifest_conv!r} adapters={adapter_conv!r}"
assert adapter_dir == manifest_dir, f"METRIC_DIRECTION mismatch: manifest={manifest_dir!r} adapters={adapter_dir!r}"
# atol must be >= manifest (adapter is authoritative on noise magnitude; manifest drift is a [MAJOR])
assert abs(adapter_atol - manifest_atol) < 1e-12, f"EVAL_NOISE_ATOL mismatch: manifest={manifest_atol} adapters={adapter_atol}"

print("forward-convention consistency OK")
PY

if [ "$CONV_RC" -eq 0 ]; then
  ok "forward-convention consistency (manifest vs adapters)"
else
  fail "forward-convention consistency check failed"
fi

# ── 5. flat __main__ runs and prints an output shape ───────────────────────
# Run the flat as a subprocess; expect exit 0 + stdout mentions a shape (digit/paren).
MAIN_RC=0
MAIN_OUT="$(python3 "$FLAT" 2>/dev/null)" || MAIN_RC=$?
if [ "$MAIN_RC" -eq 0 ] && printf '%s' "$MAIN_OUT" | grep -Eq '[0-9]+'; then
  ok "flat __main__ ran and produced output"
else
  fail "flat __main__ did not run cleanly (exit=$MAIN_RC)"
fi

# ── Result ───────────────────────────────────────────────────────────────
if [ "$FAILS" -ne 0 ]; then
  echo "[check_ingest] result: FAIL ($FAILS check(s) failed)"
  exit 1
fi
echo "[check_ingest] result: PASS"
