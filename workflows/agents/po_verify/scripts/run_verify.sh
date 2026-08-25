#!/usr/bin/env bash
# run_verify.sh — batch L0 (latency gate) verification for every DONE variant.
#
# For each variants/<vid>/ with a DONE marker and no verdict.json:
#   1. two-layer declaration check (file / graph) against the CURRENT base
#      (base shadow, base onnx);
#   2. re-profile the variant onnx with the pinned profiler;
#   3. latency gate: improvement >= max(min_improvement_cycles,
#      min_improvement_pct% x base) AND actual/predicted improvement
#      ratio >= min_pred_actual_ratio.
#
# Writes variants/<vid>/verdict.json, appends rounds/<RRR>/verdicts.jsonl and
# the L0 history row through the typed history builder. Reconciliation pass:
# any verdict.json whose HISTORY row is missing (crash between the verdict
# write and the history append) gets its history row re-appended from the
# verdict file — the per-round jsonl is an append-only audit stream and is
# NOT re-derived by reconciliation (nothing downstream consumes it).
#
# Idempotent: a variant with an existing verdict.json is skipped.
# Per-variant eliminations (structural_mismatch / unsupported_op /
# latency_fail) are legitimate verdicts, not script failures.
# stdout: single-line JSON (the node output). Logs to stderr.
# rc 0 = executed; rc 2 = hard error (missing artifacts / infrastructure).
set -euo pipefail

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (run_verify.sh)}"
SCRIPTS="$ART/scripts"
PROFILER=""
MIN_IMP="100"
MIN_PCT="1"
MIN_RATIO="0.5"
while [ $# -gt 0 ]; do
  case "$1" in
    --profiler)        PROFILER="${2:?--profiler needs a value}"; shift 2 ;;
    --min-improvement) MIN_IMP="${2:?--min-improvement needs a value}"; shift 2 ;;
    --min-pct)         MIN_PCT="${2:?--min-pct needs a value}"; shift 2 ;;
    --min-ratio)       MIN_RATIO="${2:?--min-ratio needs a value}"; shift 2 ;;
    *) echo "FATAL: unknown arg $1" >&2; exit 2 ;;
  esac
done

# ── pinned interpreter (from contracts.json) ─────────────────────────────────
# diff_check's graph layer needs onnx — only the interpreter pinned in
# contracts.json is guaranteed to have it. python3 below is bootstrap-only.
read_contract() { # read_contract <python-expr over c> -> value
  python3 - "$ART/contracts.json" "$1" <<'PYEOF'
import json, sys
from pathlib import Path
try:
    c = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(eval(sys.argv[2], {"json": json}, {"c": c}))  # fixed caller-supplied expr
except Exception as exc:
    print(f"FATAL: contracts.json field missing: {exc}", file=sys.stderr)
    sys.exit(2)
PYEOF
}
PY="$(read_contract 'c["interpreter"]["sys_executable"]')"
command -v "$PY" >/dev/null 2>&1 || [ -x "$PY" ] || {
  echo "FATAL: pinned interpreter not found: $PY" >&2; exit 2; }
for f in diff_check.py history_lib.py emit_result.py; do
  [ -f "$SCRIPTS/$f" ] || { echo "FATAL: $SCRIPTS/$f not deployed — entry stage incomplete" >&2; exit 2; }
done
[ -n "$PROFILER" ] && [ -f "$PROFILER" ] || {
  echo "FATAL: profiler script not found: '$PROFILER'" >&2; exit 2; }
for req in "$ART/base/profile/profile_summary.json" "$ART/base/model.onnx" "$ART/shadow" "$ART/rounds"; do
  [ -e "$req" ] || { echo "FATAL: base reference missing: $req (baseline stage incomplete)" >&2; exit 2; }
done
# gate thresholds are validated UP FRONT: a bad --min-pct must fail loud even
# when this invocation has zero DONE variants (no gate would ever evaluate it)
"$PY" -c 'import sys
try:
    ok = float(sys.argv[1]) >= 0
except ValueError:
    ok = False
sys.exit(0 if ok else 1)' "$MIN_PCT" || {
  echo "FATAL: --min-pct must be a number >= 0 (got '$MIN_PCT')" >&2; exit 2; }

# ── current round + base makespan ─────────────────────────────────────────────
ROUND="$("$PY" - "$ART/rounds" <<'PYEOF'
import sys
from pathlib import Path
d = Path(sys.argv[1])
nums = [int(c.name) for c in d.iterdir() if c.is_dir() and c.name.isdigit()]
print(max(nums) if nums else 0)
PYEOF
)"
[ "$ROUND" -gt 0 ] || { echo "FATAL: no rounds/<NNN>/ directory — proposal stage missing" >&2; exit 2; }
ROUND_DIR="$ART/rounds/$(printf '%03d' "$ROUND")"
BASE_MS="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["makespan_cycles"])' "$ART/base/profile/profile_summary.json")"
mkdir -p "$ROUND_DIR"
VERDICTS="$ROUND_DIR/verdicts.jsonl"
touch "$VERDICTS"

# ── per-variant verification ──────────────────────────────────────────────────
NEW_COUNT=0
PASS_COUNT=0
OUTCOME_PARTS=""

record_outcome_count() { # record_outcome_count <outcome>
  local outcome="$1" found=""
  local rebuilt=""
  local entry name count
  for entry in $OUTCOME_PARTS; do
    name="${entry%%=*}"; count="${entry#*=}"
    if [ "$name" = "$outcome" ]; then
      count=$((count + 1)); found="1"
    fi
    if [ -n "$rebuilt" ]; then rebuilt="$rebuilt $name=$count"; else rebuilt="$name=$count"; fi
  done
  if [ -z "$found" ]; then
    if [ -n "$rebuilt" ]; then rebuilt="$rebuilt $outcome=1"; else rebuilt="$outcome=1"; fi
  fi
  OUTCOME_PARTS="$rebuilt"
}

append_layer_note() { # append_layer_note <layers-json> <note> -> prints new layers-json
  "$PY" - "$1" "$2" <<'PYEOF'
import json, sys
layers = json.loads(sys.argv[1])
layers.append(sys.argv[2])
print(json.dumps(layers))
PYEOF
}

write_verdict() { # write_verdict <vid> <verdict-json> — verdict.json + jsonl + history row
  local vid="$1" payload="$2"
  printf '%s\n' "$payload" > "$ART/variants/$vid/verdict.json"
  printf '%s\n' "$payload" >> "$VERDICTS"
  "$PY" - "$SCRIPTS" "$ART/history.jsonl" "$vid" "$payload" <<'PYEOF'
import json, sys
sys.path.insert(0, sys.argv[1])
from history_lib import append_latency
vid, payload = sys.argv[3], json.loads(sys.argv[4])
append_latency(sys.argv[2], vid,
               structural_check=payload["structural_check"],
               makespan_cycles=payload["makespan_cycles"],
               latency_gate=payload["latency_gate"],
               pred_actual_ratio=payload["pred_actual_ratio"],
               outcome=payload["outcome"])
PYEOF
}

for vdir in "$ART"/variants/*/; do
  [ -d "$vdir" ] || continue
  vid="$(basename "$vdir")"
  if [ ! -f "$vdir/DONE" ]; then continue; fi
  if [ -f "$vdir/verdict.json" ]; then
    echo "skip $vid: verdict already on disk" >&2
    continue
  fi
  decl="$vdir/declaration.json"
  [ -f "$decl" ] || { echo "FATAL: $vid has DONE but no declaration.json" >&2; exit 2; }

  edited="$("$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps(d["edited_files"]))' "$decl")"
  opdelta="$("$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps(d["op_delta"]))' "$decl")"
  predicted="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["predicted_delta_cycles"])' "$decl")"

  # ---- layer 1: file ----
  mismatch_layers="[]"
  file_rc=0
  file_out="$("$PY" "$SCRIPTS/diff_check.py" --layer file \
      --base-shadow "$ART/shadow" --variant-shadow "$vdir/shadow" \
      --edited-files "$edited")" || file_rc=$?
  [ "$file_rc" -lt 2 ] || { echo "FATAL: file layer hard error for $vid" >&2; exit 2; }
  if [ "$file_rc" -eq 1 ]; then
    note="$("$PY" -c 'import json,sys; v=json.loads(sys.stdin.read()); print("file: not_declared=" + ",".join(v["not_declared"]) + " declared_but_absent=" + ",".join(v["declared_but_absent"]))' <<<"$file_out")"
    mismatch_layers="$(append_layer_note "$mismatch_layers" "$note")"
  fi

  # ---- layer 2: graph ----
  vonnx="$vdir/onnx/model.onnx"
  graph_rc=0
  if [ ! -f "$vonnx" ]; then
    mismatch_layers="$(append_layer_note "$mismatch_layers" "graph: variant onnx missing")"
    graph_rc=1
  else
    graph_out="$("$PY" "$SCRIPTS/diff_check.py" --layer graph \
        --base-onnx "$ART/base/model.onnx" --variant-onnx "$vonnx" \
        --op-delta "$opdelta")" || graph_rc=$?
    [ "$graph_rc" -lt 2 ] || { echo "FATAL: graph layer hard error for $vid" >&2; exit 2; }
    if [ "$graph_rc" -eq 1 ]; then
      note="$("$PY" -c 'import json,sys; v=json.loads(sys.stdin.read()); print("graph: ops " + ",".join(v["mismatched_ops"]))' <<<"$graph_out")"
      mismatch_layers="$(append_layer_note "$mismatch_layers" "$note")"
    fi
  fi

  structural="pass"
  if [ "$file_rc" -ne 0 ] || [ "$graph_rc" -ne 0 ]; then
    structural="fail"
  fi

  if [ "$structural" = "fail" ]; then
    verdict="$("$PY" - "$vid" "$ROUND" "$mismatch_layers" "$predicted" <<'PYEOF'
import json, sys
vid, rnd, layers, predicted = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
print(json.dumps({"vid": vid, "round": rnd, "structural_check": "fail",
                  "mismatch_layers": json.loads(layers), "makespan_cycles": None,
                  "base_makespan_cycles": None, "improvement_cycles": None,
                  "required_improvement_cycles": None, "pred_actual_ratio": None,
                  "latency_gate": None, "predicted_delta_cycles": predicted,
                  "outcome": "structural_mismatch"}))
PYEOF
)"
    write_verdict "$vid" "$verdict"
    NEW_COUNT=$((NEW_COUNT + 1)); record_outcome_count structural_mismatch
    echo "verdict $vid: structural_mismatch" >&2
    continue
  fi

  # ---- re-profile ----
  prof_rc=0
  "$PY" "$PROFILER" --onnx "$vonnx" --out-dir "$vdir/profile" \
      > "$vdir/profile.stdout.json" 2> "$vdir/profile.stderr.log" || prof_rc=$?
  if [ "$prof_rc" -ne 0 ]; then
    if grep -q '^unsupported:' "$vdir/profile.stderr.log" 2>/dev/null; then
      verdict="$("$PY" - "$vid" "$ROUND" "$vdir/profile.stderr.log" "$predicted" <<'PYEOF'
import json, sys
vid, rnd, predicted = sys.argv[1], int(sys.argv[2]), int(sys.argv[4])
ops = [l.split(":", 1)[1].strip() for l in open(sys.argv[3], encoding="utf-8")
       if l.startswith("unsupported:")]
print(json.dumps({"vid": vid, "round": rnd, "structural_check": "pass",
                  "unsupported_ops": sorted(set(ops)), "makespan_cycles": None,
                  "base_makespan_cycles": None, "improvement_cycles": None,
                  "required_improvement_cycles": None, "pred_actual_ratio": None,
                  "latency_gate": None, "predicted_delta_cycles": predicted,
                  "outcome": "unsupported_op"}))
PYEOF
)"
      write_verdict "$vid" "$verdict"
      NEW_COUNT=$((NEW_COUNT + 1)); record_outcome_count unsupported_op
      echo "verdict $vid: unsupported_op" >&2
      continue
    fi
    echo "FATAL: profiler exited $prof_rc for $vid without an unsupported-op diagnosis" >&2
    exit 2
  fi
  var_ms="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["makespan_cycles"])' "$vdir/profile/profile_summary.json")"

  # ---- latency gate ----
  gate_rc=0
  gate="$("$PY" - "$BASE_MS" "$var_ms" "$predicted" "$MIN_IMP" "$MIN_PCT" "$MIN_RATIO" <<'PYEOF'
import json, sys
base, var, pred = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
min_imp, min_pct, min_ratio = int(sys.argv[4]), float(sys.argv[5]), float(sys.argv[6])
if pred >= 0:
    print(json.dumps({"error": f"predicted_delta_cycles {pred} is not negative"}))
    sys.exit(3)
improvement = base - var
required = max(min_imp, int(base * min_pct / 100))
ratio = improvement / (-pred)
gate = "pass" if (improvement >= required and ratio >= min_ratio) else "fail"
print(json.dumps({"improvement": improvement, "required": required,
                  "ratio": round(ratio, 6), "latency_gate": gate}))
PYEOF
)" || gate_rc=$?
if [ "$gate_rc" -ne 0 ]; then
  echo "FATAL: gate math failed for $vid: $gate" >&2
  exit 2
fi
  outcome="latency_fail"
  if [ "$("$PY" -c 'import json,sys; print(json.loads(sys.argv[1])["latency_gate"])' "$gate")" = "pass" ]; then
    outcome="latency_pass"
  fi
  verdict="$("$PY" - "$vid" "$ROUND" "$BASE_MS" "$predicted" "$gate" "$outcome" <<'PYEOF'
import json, sys
vid, rnd, base, predicted = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
g, outcome = json.loads(sys.argv[5]), sys.argv[6]
print(json.dumps({"vid": vid, "round": rnd, "structural_check": "pass",
                  "makespan_cycles": base - g["improvement"],
                  "base_makespan_cycles": base,
                  "improvement_cycles": g["improvement"],
                  "required_improvement_cycles": g["required"],
                  "pred_actual_ratio": g["ratio"],
                  "latency_gate": g["latency_gate"],
                  "predicted_delta_cycles": predicted, "outcome": outcome}))
PYEOF
)"
  write_verdict "$vid" "$verdict"
  NEW_COUNT=$((NEW_COUNT + 1)); record_outcome_count "$outcome"
  if [ "$outcome" = "latency_pass" ]; then PASS_COUNT=$((PASS_COUNT + 1)); fi
  echo "verdict $vid: $outcome (makespan $var_ms vs base $BASE_MS)" >&2
done

# ── reconciliation: verdict.json present but history row missing ──────────────
RECON=0
for vdir in "$ART"/variants/*/; do
  [ -d "$vdir" ] || continue
  [ -f "$vdir/verdict.json" ] || continue
  vid="$(basename "$vdir")"
  need="$("$PY" - "$SCRIPTS" "$ART/history.jsonl" "$vid" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
from history_lib import read_latest
latest = read_latest(sys.argv[2])
row = latest.get(sys.argv[3], {})
print("yes" if "structural_check" not in row else "no")
PYEOF
)"
  if [ "$need" = "yes" ]; then
    "$PY" - "$SCRIPTS" "$ART/history.jsonl" "$vid" "$vdir/verdict.json" <<'PYEOF'
import json, sys
sys.path.insert(0, sys.argv[1])
from history_lib import append_latency
v = json.loads(open(sys.argv[4], encoding="utf-8").read())
append_latency(sys.argv[2], sys.argv[3],
               structural_check=v["structural_check"],
               makespan_cycles=v["makespan_cycles"],
               latency_gate=v["latency_gate"],
               pred_actual_ratio=v["pred_actual_ratio"],
               outcome=v["outcome"])
PYEOF
    RECON=$((RECON + 1))
    echo "reconciled history row for $vid" >&2
  fi
done

# ── summary emit (single-line JSON on stdout) ─────────────────────────────────
SUMMARY="$NEW_COUNT verdicts"
if [ -n "$OUTCOME_PARTS" ]; then
  SUMMARY="$SUMMARY [$OUTCOME_PARTS]"
fi
if [ "$RECON" -gt 0 ]; then
  SUMMARY="$SUMMARY; $RECON history rows reconciled"
fi
"$PY" "$SCRIPTS/emit_result.py" \
  --field "status=executed" \
  --field "verdicts_count=$NEW_COUNT" \
  --field "latency_pass_count=$PASS_COUNT" \
  --field "verdicts_path=$VERDICTS" \
  --field "summary=$SUMMARY" \
  --field 'error='
