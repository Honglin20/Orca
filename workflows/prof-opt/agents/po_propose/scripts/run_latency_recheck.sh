#!/usr/bin/env bash
# run_latency_recheck.sh — batch latency re-verification for every DONE variant.
#
# Runs INSIDE the po_propose node (the implement → recheck → repair loop's
# measurement step). For each variants/<vid>/ with a DONE marker and no
# verdict.json:
#   1. two-layer declaration check (file / graph) against the CURRENT base
#      (base shadow, base onnx);
#   2. profile the variant onnx — the mfu four-piece ALREADY under
#      variants/<vid>/profile/ (v7: mfu is the ONE profiling path; the node
#      dispatched mfu-analyzer + ran mfu_adapter.py per variant before this
#      call). A variant without a four-piece is a hard error — there is no
#      inline profiling path to fall back to.
#   3. latency gate via scripts/check_verdict.py —makespan (v7 §6.2: the
#      ONE latency-line predicate; this script and the probe emit gate call
#      it, neither re-implements the comparison). pass <=> makespan <=
#      target_cycles, the frozen origin anchor's line (inclusive boundary).
#
# Writes variants/<vid>/verdict.json, appends rounds/<RRR>/verdicts.jsonl and
# the L0 history row through the typed history builder. Reconciliation pass:
# any verdict.json whose HISTORY row is missing (crash between the verdict
# write and the history append) gets its history row re-appended from the
# verdict file — the per-round jsonl is an append-only audit stream and is
# NOT re-derived by reconciliation (nothing downstream consumes it).
#
# Repair ledger (v6 §5.2): every latency_fail measurement appends one attempt
# to variants/<vid>/repair_trace.json ({"vid", "repair_count", "attempts":
# [{round, measured_makespan_cycles, target_cycles, gap_cycles, reason}]};
# repair_count = len(attempts)). The attempt is recorded BEFORE the verdict
# write and is NEVER value-deduplicated: the recheck's replay idempotency key
# is verdict.json presence, so reaching the measurement step means a fresh
# repair pass — including one whose makespan repeats the previous value (a
# no-op repair must still consume budget, or the repair loop is unbounded).
# The narrow crash window between record and verdict write can only
# double-record a replayed measurement (budget consumed one notch early —
# fail-safe); an attempt is never lost (an under-count would disable the
# budget entirely). A vid whose repair_count is already >= 5 is TERMINAL —
# measuring it again means the caller tried a 6th repair, which fails LOUD
# here (rc 2, §14): the script, not the node prompt, is the repair-budget
# backstop.
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
while [ $# -gt 0 ]; do
  case "$1" in
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
for f in diff_check.py history_lib.py emit_result.py round_state.py check_verdict.py; do
  [ -f "$SCRIPTS/$f" ] || { echo "FATAL: $SCRIPTS/$f not deployed — entry stage incomplete" >&2; exit 2; }
done
for req in "$ART/base/profile/profile_summary.json" "$ART/base/model.onnx" "$ART/shadow" "$ART/rounds" "$ART/base/origin_anchor.json"; do
  [ -e "$req" ] || { echo "FATAL: base reference missing: $req (baseline stage incomplete)" >&2; exit 2; }
done

# ── target line (origin anchor — single source; read-only) ───────────────────
TARGET_CYCLES="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["target_cycles"])' "$ART/base/origin_anchor.json")"

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

repair_guard() { # repair_guard <vid> — rc 2 when the 5-repair budget is spent (v6 §5.2/§14)
  "$PY" - "$ART" "$1" <<'PYEOF'
import json, sys
from pathlib import Path
trace = Path(sys.argv[1]) / "variants" / sys.argv[2] / "repair_trace.json"
if not trace.is_file():
    raise SystemExit(0)   # never failed a measurement yet: nothing to guard
try:
    doc = json.loads(trace.read_text(encoding="utf-8"))
except json.JSONDecodeError as exc:
    print(f"FATAL: {sys.argv[2]} repair_trace.json unparseable: {exc}", file=sys.stderr)
    raise SystemExit(2)
count = doc.get("repair_count")
if isinstance(count, int) and count >= 5:
    print(f"FATAL: {sys.argv[2]} repair budget exhausted (repair_count={count} >= 5, "
          "v6 section 5.2) — a latency_fail variant is terminal; do not delete its "
          "verdict and re-repair", file=sys.stderr)
    raise SystemExit(2)
PYEOF
}

record_repair_attempt() { # record_repair_attempt <vid> <verdict-json> — one failed measurement
  "$PY" - "$ART" "$1" "$2" <<'PYEOF'
import json, os, sys
from pathlib import Path
art, vid = Path(sys.argv[1]), sys.argv[2]
v = json.loads(sys.argv[3])
trace = art / "variants" / vid / "repair_trace.json"
doc = {"vid": vid, "repair_count": 0, "attempts": []}
if trace.is_file():
    try:
        doc = json.loads(trace.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"FATAL: {vid} repair_trace.json unparseable: {exc}", file=sys.stderr)
        raise SystemExit(2)
attempt = {"round": v["round"],
           "measured_makespan_cycles": v["makespan_cycles"],
           "target_cycles": v["target_cycles"],
           "gap_cycles": v["makespan_cycles"] - v["target_cycles"],
           "reason": "makespan > target_cycles (unified v6 gate)"}
attempts = doc.setdefault("attempts", [])
attempts.append(attempt)   # never value-deduplicated: see header comment
doc["vid"] = vid
doc["repair_count"] = len(attempts)
tmp = trace.with_suffix(trace.suffix + f".tmp.{os.getpid()}")
tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
               encoding="utf-8")
os.replace(tmp, trace)
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
  repair_guard "$vid"   # rc 2 (set -e aborts) when a 6th repair is attempted
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
    verdict="$("$PY" - "$vid" "$ROUND" "$mismatch_layers" "$predicted" \
        "$TARGET_CYCLES" <<'PYEOF'
import json, sys
vid, rnd, layers, predicted = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
target = int(sys.argv[5])
print(json.dumps({"vid": vid, "round": rnd, "structural_check": "fail",
                  "mismatch_layers": json.loads(layers), "makespan_cycles": None,
                  "base_makespan_cycles": None, "target_cycles": target,
                  "improvement_cycles": None,
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

  # ---- profile: the mfu four-piece (the one profiling path, v7) ----
  var_ms=""
  # the four-piece was produced per-variant by the node (mfu-analyzer
  # subagent + mfu_adapter.py) BEFORE this call — there is no inline path
  if [ ! -s "$vdir/profile/profile_summary.json" ]; then
    echo "FATAL: $vid has no four-piece under variants/$vid/profile/ — profile it first (dispatch mfu-analyzer + run scripts/mfu_adapter.py --profile-dir variants/$vid/profile); there is no inline profiling path" >&2
    exit 2
  fi
  var_ms="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["makespan_cycles"])' "$vdir/profile/profile_summary.json")"

  # ---- latency gate: scripts/check_verdict.py is the ONE predicate ----
  gate="fail"
  if gate_note="$("$PY" "$SCRIPTS/check_verdict.py" --vid "$vid" --makespan "$var_ms" 2>&1)"; then
    gate="pass"
  else
    # the predicate's own reason travels to stderr (never swallowed): an
    # above-the-line verdict is a normal fail; a torn anchor is visible too
    echo "latency gate $vid: $gate_note" >&2
  fi
  verdict="$("$PY" - "$vid" "$ROUND" "$BASE_MS" "$var_ms" "$predicted" \
      "$TARGET_CYCLES" "$gate" <<'PYEOF'
import json, sys
vid, rnd = sys.argv[1], int(sys.argv[2])
base, var, pred, target = (int(sys.argv[3]), int(sys.argv[4]),
                           int(sys.argv[5]), int(sys.argv[6]))
gate = sys.argv[7]
ratio = (base - var) / (-pred) if pred < 0 else None
outcome = "latency_pass" if gate == "pass" else "latency_fail"
print(json.dumps({"vid": vid, "round": rnd, "structural_check": "pass",
                  "makespan_cycles": var, "base_makespan_cycles": base,
                  "target_cycles": target,
                  "improvement_cycles": base - var,
                  "pred_actual_ratio": None if ratio is None else round(ratio, 6),
                  "latency_gate": gate,
                  "predicted_delta_cycles": pred, "outcome": outcome}))
PYEOF
)"
  outcome="$("$PY" -c 'import json,sys; print(json.loads(sys.argv[1])["outcome"])' "$verdict")"
  if [ "$outcome" = "latency_fail" ]; then
    # v6 §5.2 repair ledger — recorded BEFORE the verdict write (the verdict
    # file is the replay skip key): an attempt is never lost, a crash-replay
    # may double-record (fail-safe, consumes budget early)
    record_repair_attempt "$vid" "$verdict"
  fi
  write_verdict "$vid" "$verdict"
  NEW_COUNT=$((NEW_COUNT + 1)); record_outcome_count "$outcome"
  if [ "$outcome" = "latency_pass" ]; then PASS_COUNT=$((PASS_COUNT + 1)); fi
  echo "verdict $vid: $outcome (makespan $var_ms vs target $TARGET_CYCLES)" >&2
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
  --field "target_cycles=$TARGET_CYCLES" \
  --field "verdicts_path=$VERDICTS" \
  --field "summary=$SUMMARY" \
  --field 'error='
