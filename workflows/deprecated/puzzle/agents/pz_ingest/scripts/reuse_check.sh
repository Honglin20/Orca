#!/usr/bin/env bash
# reuse_check.sh — pz_ingest Step 0 soft-skip gate.
# Prints REUSE_VALID when the four authoritative artifacts all exist and the
# flat still parses; otherwise prints nothing and the agent runs Step 1.
# Project-scoped artifacts are reused across runs to avoid re-ingesting a
# stable project (saves LLM compute).
set +e
cd "$ORCA_ARTIFACTS_DIR" 2>/dev/null || { echo "FATAL: ORCA_ARTIFACTS_DIR unreachable" >&2; exit 1; }

MISSING=""
for f in puzzle_adapters.py manifest.yaml project_manifest.md; do
  [ -s "$f" ] || MISSING="$MISSING $f"
done
FLAT="$(ls *_flat.py 2>/dev/null | head -1)"
[ -n "$FLAT" ] || MISSING="$MISSING <base>_flat.py"
[ -n "$MISSING" ] && exit 0   # not all artifacts present → run Step 1

# flat parses + manifest has the five top-level sections + adapter module imports.
python3 - "$FLAT" <<'PY' 2>/dev/null
import ast, sys, yaml, importlib.util
# flat parses
ast.parse(open(sys.argv[1], encoding="utf-8").read())
# manifest five sections
m = yaml.safe_load(open("manifest.yaml", encoding="utf-8"))
for sec in ("project_overview", "model", "training_and_evaluation",
            "data_and_environment", "relevant_source_files"):
    assert sec in m, f"manifest missing section {sec}"
# adapter module imports (do not exec flat)
spec = importlib.util.spec_from_file_location("_chk", "puzzle_adapters.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
for name in ("build_model", "FORWARD_CALLING_CONVENTION", "forward_model",
             "calib_iter", "train_iter", "extract_labels", "kd_loss",
             "task_loss", "evaluate", "METRIC_DIRECTION", "EVAL_NOISE_ATOL",
             "load_pretrained", "DUMMY_INPUT"):
    assert hasattr(mod, name), f"puzzle_adapters missing API {name}"
print("REUSE_VALID")
PY
