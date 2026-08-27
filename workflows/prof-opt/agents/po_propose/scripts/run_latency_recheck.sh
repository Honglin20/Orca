#!/usr/bin/env bash
# run_latency_recheck.sh — batch latency re-verification for every DONE variant.
#
# Runs INSIDE the po_propose node (the implement → recheck → repair loop's
# measurement step). For each variants/<vid>/ with a DONE marker and no
# verdict.json:
#   1. two-layer declaration check (file / graph) against the CURRENT base
#      (base shadow, base onnx);
#   2. profile the variant onnx — the PROFILING mode comes from
#      profile_mode.json (written once at entry):
#        placeholder : re-profile inline with the pinned profiler (the
#                      deployed placeholder estimator unless --profiler
#                      says otherwise)
#        mfu         : trust the four-piece ALREADY under
#                      variants/<vid>/profile/ (the node dispatched
#                      mfu-analyzer + ran mfu_adapter.py per variant before
#                      this call). Inline profiling is DISABLED; a variant
#                      without a four-piece is a hard error, never an inline
#                      re-profile.
#   3. latency gate (no absolute / relative / ratio thresholds — a small
#      strictly-better step is a legitimate pass; the prediction ratio is an
#      informational field only, never a gate):
#        gate mode latency  (chase phase)    : pass <=> makespan < incumbent
#                                               (incumbent = best.json
#                                               makespan, else the origin
#                                               anchor baseline makespan)
#        gate mode accuracy (recovery phase) : pass <=> makespan <= the
#                                               frozen origin anchor's
#                                               target_cycles
#      The gate mode comes from the shared round_state.py (single source);
#      both anchors are read-only on disk.
#
# Writes variants/<vid>/verdict.json, appends rounds/<RRR>/verdicts.jsonl and
# the L0 history row through the typed history builder. Reconciliation pass:
# any verdict.json whose HISTORY row is missing (crash between the verdict
# write and the history append) gets its history row re-appended from the
# verdict file — the per-round jsonl is an append-only audit stream and is
# NOT re-derived by reconciliation (nothing downstream consumes it).
#
# Idempotent: a variant with an existing verdict.json is skipped (verdict.json
# presence IS the skip key — the node deletes a rejected variant's verdict.json
# before sending it back for repair and a fresh recheck).
# Per-variant eliminations (structural_mismatch / unsupported_op /
# latency_fail) are legitimate verdicts, not script failures.
# stdout: single-line JSON (consumed by the po_propose node as an info line —
# latency_pass_count feeds its output). Logs to stderr.
# rc 0 = executed; rc 2 = hard error (missing artifacts / infrastructure).
set -euo pipefail

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (run_latency_recheck.sh)}"
SCRIPTS="$ART/scripts"
PROFILER="$SCRIPTS/placeholder_profiler.py"
PROFILER_SET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --profiler) PROFILER="${2:?--profiler needs a value}"; PROFILER_SET=1; shift 2 ;;
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
for f in diff_check.py history_lib.py emit_result.py round_state.py; do
  [ -f "$SCRIPTS/$f" ] || { echo "FATAL: $SCRIPTS/$f not deployed — entry stage incomplete" >&2; exit 2; }
done
[ -n "$PROFILER" ] || PROFILER="$SCRIPTS/placeholder_profiler.py"
for req in "$ART/base/profile/profile_summary.json" "$ART/base/model.onnx" "$ART/shadow" "$ART/rounds" "$ART/profile_mode.json" "$ART/base/origin_anchor.json"; do
  [ -e "$req" ] || { echo "FATAL: base reference missing: $req (baseline stage incomplete)" >&2; exit 2; }
done

# ── profiling mode (profile_mode.json — single source) ────────────────────────
PROFILE_MODE="$("$PY" - "$ART/profile_mode.json" <<'PYEOF'
import json, sys
try:
    mode = json.loads(open(sys.argv[1], encoding="utf-8").read()).get("mode")
except Exception as exc:
    print(f"FATAL: profile_mode.json unparseable: {exc}", file=sys.stderr); raise SystemExit(2)
if mode not in ("placeholder", "mfu"):
    print(f"FATAL: profile_mode.json mode must be placeholder|mfu, got {mode!r} — re-run the entry node to re-resolve the profiling mode", file=sys.stderr)
    raise SystemExit(2)
print(mode)
PYEOF
)"
if [ "$PROFILE_MODE" = "placeholder" ] && [ ! -f "$PROFILER" ]; then
  echo "FATAL: profiler script not found: '$PROFILER'" >&2; exit 2; fi
if [ "$PROFILE_MODE" = "mfu" ] && [ "$PROFILER_SET" -eq 1 ]; then
  echo "FATAL: --profiler is mutually exclusive with mfu mode (profile_mode.json) — the four-piece under variants/<vid>/profile/ is the only measurement source in this mode" >&2
  exit 2; fi

# ── gate mode + anchors (round_state.py — single source; read-only) ──────────
GATE_JSON="$("$PY" "$SCRIPTS/round_state.py" --artifacts "$ART" mode)" || exit 2
GATE_MODE="$("$PY" -c 'import json,sys; print(json.loads(sys.argv[1])["mode"])' "$GATE_JSON")"
TARGET_CYCLES="$("$PY" -c 'import json,sys; print(json.loads(sys.argv[1])["target_cycles"])' "$GATE_JSON")"
BEST_MS="$("$PY" -c 'import json,sys; print(json.loads(sys.argv[1])["best_makespan"])' "$GATE_JSON")"
if [ "$BEST_MS" = "None" ]; then
  INCUMBENT="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["baseline_makespan_cycles"])' "$ART/base/origin_anchor.json")"
else
  INCUMBENT="$BEST_MS"
fi

# ── current round (single source) + base makespan ────────────────────────────
ROUND_RAW="$("$PY" "$SCRIPTS/round_state.py" --artifacts "$ART" current)"
ROUND="$("$PY" -c 'import json,sys; print(json.loads(sys.argv[1])["round"])' "$ROUND_RAW")"
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
                  "base_makespan_cycles": None, "incumbent_cycles": None,
                  "improvement_cycles": None, "gate_mode": None,
                  "pred_actual_ratio": None,
                  "latency_gate": None, "predicted_delta_cycles": predicted,
                  "outcome": "structural_mismatch"}))
PYEOF
)"
    write_verdict "$vid" "$verdict"
    NEW_COUNT=$((NEW_COUNT + 1)); record_outcome_count structural_mismatch
    echo "verdict $vid: structural_mismatch" >&2
    continue
  fi

  # ---- profile: inline (placeholder) or the mfu four-piece ----
  var_ms=""
  if [ "$PROFILE_MODE" = "mfu" ]; then
    # mfu mode: the four-piece was produced per-variant by the node
    # (mfu-analyzer subagent + mfu_adapter.py) BEFORE this call — never
    # re-profile inline here
    if [ ! -s "$vdir/profile/profile_summary.json" ]; then
      echo "FATAL: mfu mode: $vid has no four-piece under variants/$vid/profile/ — profile it first (dispatch mfu-analyzer + run scripts/mfu_adapter.py --profile-dir variants/$vid/profile), inline profiling is disabled in this mode" >&2
      exit 2
    fi
    var_ms="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["makespan_cycles"])' "$vdir/profile/profile_summary.json")"
  else
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
                  "base_makespan_cycles": None, "incumbent_cycles": None,
                  "improvement_cycles": None, "gate_mode": None,
                  "pred_actual_ratio": None,
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
  fi

  # ---- latency gate (mode-conditioned; informational ratio only) ----
  verdict="$("$PY" - "$vid" "$ROUND" "$BASE_MS" "$var_ms" "$predicted" \
      "$GATE_MODE" "$INCUMBENT" "$TARGET_CYCLES" <<'PYEOF'
import json, sys
vid, rnd = sys.argv[1], int(sys.argv[2])
base, var, pred = int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
gate_mode, incumbent, target = sys.argv[6], int(sys.argv[7]), int(sys.argv[8])
if gate_mode == "latency":
    gate = "pass" if var < incumbent else "fail"
else:  # accuracy recovery phase: the frozen line is the filter
    gate = "pass" if var <= target else "fail"
ratio = (base - var) / (-pred) if pred < 0 else None
outcome = "latency_pass" if gate == "pass" else "latency_fail"
print(json.dumps({"vid": vid, "round": rnd, "structural_check": "pass",
                  "makespan_cycles": var, "base_makespan_cycles": base,
                  "incumbent_cycles": incumbent,
                  "improvement_cycles": base - var, "gate_mode": gate_mode,
                  "pred_actual_ratio": None if ratio is None else round(ratio, 6),
                  "latency_gate": gate,
                  "predicted_delta_cycles": pred, "outcome": outcome}))
PYEOF
)"
  write_verdict "$vid" "$verdict"
  outcome="$("$PY" -c 'import json,sys; print(json.loads(sys.argv[1])["outcome"])' "$verdict")"
  NEW_COUNT=$((NEW_COUNT + 1)); record_outcome_count "$outcome"
  if [ "$outcome" = "latency_pass" ]; then PASS_COUNT=$((PASS_COUNT + 1)); fi
  echo "verdict $vid: $outcome (makespan $var_ms vs incumbent $INCUMBENT, gate_mode $GATE_MODE)" >&2
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
  --field "gate_mode=$GATE_MODE" \
  --field "verdicts_path=$VERDICTS" \
  --field "summary=$SUMMARY" \
  --field 'error='
