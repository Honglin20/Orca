#!/usr/bin/env bash
# run_baseline_chain.sh — po_baseline seven-step chain driver.
#
# Steps (each idempotent: the step's product file existing = done, re-entry
# skips it; the product is the authority, baseline_status.md is the view):
#   1 reference-cross-check : compare <<reference_onnx>> vs the shadow export
#                            (shape/op-set); mismatch -> WARNING, never blocking.
#                            Runs the export first when base/model.onnx is absent.
#   2 export + snapshot     : snapshot the pristine shadow -> baseline/original_shadow/
#                            (the untouched round-0 structure the final stage may
#                            retrain at full budget), then render + run the export
#                            template -> base/model.onnx
#   3 profile               : four profiling artifacts -> base/profile/
#   4 analyze               : scripts/analyze.py -> base/bottleneck_report.json
#   5 baseline ref          : write baseline/baseline_ref.json from the
#                            --baseline-ref-acc input (empty -> explicit null
#                            marker; the final stage then auto-trains the
#                            baseline at full budget)
#   6 baseline proxy train  : zero-structure-change FROM-SCRATCH training at the
#                            SAME contracts.json proxy_budget every variant gets
#                            (fairness invariant: identical budget fields), then
#                            extract EVERY epoch metric -> baseline/baseline_metrics.jsonl,
#                            eval -> baseline/baseline_proxy_acc.json (the
#                            promotion anchor). ALWAYS detached + polled.
#                            The product-exists skip first re-verifies the
#                            anchor's recorded proxy_budget against the
#                            CURRENT contracts.json — mismatch fails loud
#                            (a stale anchor voids fair comparison).
#   7 target check          : baseline makespan must be strictly WORSE than the
#                            target, otherwise the loop has no headroom -> fail
#                            loud with guidance.
#
# Long steps (6 always; 3 when --profile-script is provided) run detached in
# their own session (setsid): this script re-invokes itself with --worker-step N.
# Each detach logs to its OWN per-attempt file (worker log naming below) so a
# first-attempt failure scene is never overwritten by a retry. A single
# invocation polls a detached step at most --poll-max-secs (default 480,
# keeping one bash call under ~10 min); still running -> stdout JSON status
# "running" and the AGENT simply re-invokes this script later. Crash guard:
# a step that died without an rc file is re-detached at most 3 times, then fail
# loud. No second live worker is ever started while one is alive (pid check).
#
# rc aggregation: first step whose rc != 0 stops the chain -> stdout JSON
# status "failed" with "baseline step N: <reason>" folded into `error`; all
# done -> "executed". stdout: ALWAYS exactly one JSON line whose fields are
# EXACTLY the node output_schema field set (the agent forwards the executed /
# failed line VERBATIM — an extra key would be rejected by
# additionalProperties:false); all logs to stderr/files. The mid-poll
# "running" line is agent-internal only (status not in the schema enum) and is
# never a final node output.
#
# Usage:
#   run_baseline_chain.sh --latency-reduction-min F --seed N
#                         [--baseline-ref-acc NUM] [--profile-script PATH]
#                         [--reference-onnx PATH] [--poll-max-secs S]
#                         [--worker-step N]
# Environment: ORCA_ARTIFACTS_DIR (required). The user project root comes
# ONLY from readiness/readiness.json — the engine already exports a
# same-purpose project-root env var (it resolves to the Orca repository
# root, NOT the user project), so reading any project-root env here would
# silently anchor to the wrong project.
set -uo pipefail

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (run_baseline_chain.sh)}"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

TARGET=""; SEED="0"; PROFILE_SCRIPT=""; REFERENCE=""; POLL_MAX_SECS=480; WORKER_STEP=""; BASELINE_REF_ACC=""
while [ $# -gt 0 ]; do
  case "$1" in
    --latency-reduction-min) TARGET="${2:?}"; shift 2 ;;
    --seed)              SEED="${2:?}"; shift 2 ;;
    --baseline-ref-acc)  BASELINE_REF_ACC="${2:?}"; shift 2 ;;
    --profile-script)    PROFILE_SCRIPT="${2:?}"; shift 2 ;;
    --reference-onnx)    REFERENCE="${2:?}"; shift 2 ;;
    --poll-max-secs)     POLL_MAX_SECS="${2:?}"; shift 2 ;;
    --worker-step)       WORKER_STEP="${2:?}"; shift 2 ;;
    *) echo "FATAL: unknown arg $1" >&2; exit 2 ;;
  esac
done
[ -n "$TARGET" ] || { echo "FATAL: --latency-reduction-min is required" >&2; exit 2; }
python3 -c "import sys; r=float('$TARGET'); sys.exit(0 if 0.0 < r < 1.0 else 1)" \
  || { echo "FATAL: --latency-reduction-min must be a number in (0, 1), got '$TARGET'" >&2; exit 2; }
cd "$ART" || { echo "FATAL: artifacts dir unreachable: $ART" >&2; exit 2; }

STAMPS="$ART/baseline/.stamps"
mkdir -p "$STAMPS" "$ART/base" "$ART/baseline"

# ── contracts + interpreter + render inputs (fail loud when absent) ──────────
read_contract() { # read_contract <python-expr over c> -> value
  python3 -c '
import json, sys
from pathlib import Path
c = json.loads(Path("contracts.json").read_text(encoding="utf-8"))
try:
    print(eval(sys.argv[1], {"json": json}, {"c": c}))  # noqa: S307 — fixed caller-supplied expr
except Exception as exc:
    print(f"FATAL: contracts.json field missing: {exc}", file=sys.stderr)
    sys.exit(2)
' "$1"
}

for f in contracts.json readiness/readiness.json \
         templates/export_onnx.template.sh templates/run_probe_finetune.template.sh \
         templates/run_eval.template.sh scripts/render_run.sh scripts/analyze.py; do
  [ -f "$f" ] || { echo "FATAL: upstream artifact missing: $ART/$f (contract stage incomplete)" >&2; exit 2; }
done

PY="$(read_contract 'c["interpreter"]["sys_executable"]')"
# sole source of the user project root: readiness.json (never any
# project-root env var — the engine owns that name, see the header comment)
PROJ_ROOT="$(python3 -c '
import json
from pathlib import Path
print(json.loads(Path("readiness/readiness.json").read_text(encoding="utf-8"))["project_root"])')"
SHADOW_DIR="$ART/shadow"
SHADOW_PKGS="$(read_contract '",".join(c["shadow"]["shadow_pkgs"])')"
# proxy budget: the single source of the fairness invariant — the baseline and
# every variant render these exact values (never re-derived here)
PB_EPOCHS="$(read_contract 'c["proxy_budget"]["epochs"]')"
PB_DATA_KNOB="$(read_contract 'c["proxy_budget"].get("dataset_knob") or ""')"
PB_DATA_VALUE="$(read_contract 'c["proxy_budget"].get("data_value") if c["proxy_budget"].get("data_value") is not None else ""')"
PB_MAX_STEPS="$(read_contract 'c["proxy_budget"].get("max_steps") if c["proxy_budget"].get("max_steps") is not None else ""')"
PB_SEED="$(read_contract 'c["proxy_budget"]["seed"]')"
command -v "$PY" >/dev/null 2>&1 || [ -x "$PY" ] || { echo "FATAL: interpreter not found: $PY" >&2; exit 2; }

render() { # render <template> <out> [extra --set k=v ...]
  local tpl="$1" out="$2"; shift 2
  bash "$ART/scripts/render_run.sh" --template "$tpl" --out "$out" \
    --set shadow_dir="$SHADOW_DIR" --set shadow_pkgs="$SHADOW_PKGS" \
    --set project_root="$PROJ_ROOT" --set python="$PY" "$@"
}

extract_metric() { # extract_metric <log> -> prints the metric value per contracts rule
  python3 - "$1" <<'PY'
import json, re, sys
from pathlib import Path
rule = json.loads(Path("contracts.json").read_text(encoding="utf-8"))["eval"]["metric_extraction"]
text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
if rule["kind"] == "stdout_regex":
    m = re.search(rule["pattern"], text, re.MULTILINE)
    if not m:
        raise SystemExit(f"FATAL: metric regex did not match in {sys.argv[1]}")
    raw = m.group(1) if m.groups() else m.group(0)
    print(float(raw))
elif rule["kind"] == "json":
    data = json.loads(text)
    for part in rule["json_pointer"].strip("/").split("/"):
        data = data[int(part)] if isinstance(data, list) else data[part]
    print(float(data))
else:
    raise SystemExit(f"FATAL: unknown metric_extraction kind {rule['kind']!r}")
PY
}

resolve_ckpt() { # resolve_ckpt <out_dir> -> prints the concrete ckpt path per the recorded rule
  python3 - "$1" <<'PY'
import glob, json, os, sys
from pathlib import Path
rule = json.loads(Path("contracts.json").read_text(encoding="utf-8"))["train"]["ckpt_output_rule"]
pattern = rule.replace("{out_dir}", sys.argv[1])
if "*" in pattern:
    hits = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not hits:
        raise SystemExit(f"FATAL: ckpt rule glob matched nothing: {pattern}")
    print(hits[-1])
else:
    p = Path(pattern)
    if not p.is_file():
        raise SystemExit(f"FATAL: ckpt rule predicts {p} but it does not exist")
    print(p)
PY
}

# ── step bodies (each returns rc; product-exists short-circuits) ─────────────
step_export() {
  # pristine shadow snapshot FIRST: this chain runs before any round can advance,
  # so shadow/ still holds the untouched round-0 structure the final stage may
  # need to retrain at full budget
  if [ ! -d "$ART/baseline/original_shadow" ]; then
    python3 -c "import shutil; shutil.copytree(
        '$ART/shadow', '$ART/baseline/original_shadow',
        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))" >&2 || return 1
    echo "[chain] step2: pristine shadow snapshotted -> baseline/original_shadow" >&2
  fi
  [ -s "$ART/base/model.onnx" ] && { echo "[chain] step2 export: product exists, skip" >&2; return 0; }
  render "$ART/templates/export_onnx.template.sh" "$ART/base/.export.rendered.sh" \
    --set out="$ART/base/model.onnx" --set seed="$SEED" >&2 || return 1
  bash "$ART/base/.export.rendered.sh" >&2 || return 1
  [ -s "$ART/base/model.onnx" ] || { echo "FATAL: export produced no base/model.onnx" >&2; return 1; }
}

step_reference() {
  [ -f "$ART/baseline/reference_check.json" ] && return 0
  if [ -z "$REFERENCE" ]; then
    printf '{"skipped": true, "reason": "no reference onnx provided"}\n' > "$ART/baseline/reference_check.json"
    return 0
  fi
  [ -f "$REFERENCE" ] || { echo "FATAL: reference onnx not found: $REFERENCE" >&2; return 1; }
  step_export || return 1
  python3 - "$REFERENCE" "$ART/base/model.onnx" "$ART/baseline/reference_check.json" <<'PY' || return 1
import json, sys
from pathlib import Path
import onnx

def facts(path):
    m = onnx.load(str(path))
    ops = {}
    for n in m.graph.node:
        ops[n.op_type] = ops.get(n.op_type, 0) + 1
    io = [(i.name, [d.dim_value or d.dim_param for d in i.type.tensor_type.shape.dim])
          for i in list(m.graph.input) + list(m.graph.output)]
    return ops, io

ref_ops, ref_io = facts(sys.argv[1])
base_ops, base_io = facts(sys.argv[2])
diffs = []
for op in sorted(set(ref_ops) | set(base_ops)):
    if ref_ops.get(op, 0) != base_ops.get(op, 0):
        diffs.append(f"op {op}: reference {ref_ops.get(op, 0)} vs shadow {base_ops.get(op, 0)}")
if ref_io != base_io:
    diffs.append("graph input/output shapes differ")
Path(sys.argv[3]).write_text(json.dumps({
    "match": not diffs, "diffs": diffs,
    "reference": sys.argv[1], "shadow_export": sys.argv[2]}, indent=2), encoding="utf-8")
if diffs:
    print(f"WARN: reference onnx differs from the shadow export (NOT blocking): {diffs}", file=sys.stderr)
PY
}

step_profile_inline() {
  [ -s "$ART/base/profile/profile_summary.json" ] && { echo "[chain] step3 profile: product exists, skip" >&2; return 0; }
  local prof="$PROFILE_SCRIPT"
  [ -n "$prof" ] || prof="$ART/scripts/placeholder_profiler.py"
  [ -f "$prof" ] || { echo "FATAL: profiling script not found: $prof (custom profiler is the sole authority — no fallback)" >&2; return 1; }
  "$PY" "$prof" --onnx "$ART/base/model.onnx" --out-dir "$ART/base/profile" --seed "$SEED" >&2 || return 1
  for f in taskgraph.json ops.csv schedule.json profile_summary.json; do
    [ -s "$ART/base/profile/$f" ] || { echo "FATAL: profiler did not produce $f" >&2; return 1; }
  done
}

step_analyze() {
  [ -s "$ART/base/bottleneck_report.json" ] && { echo "[chain] step4 analyze: product exists, skip" >&2; return 0; }
  "$PY" "$ART/scripts/analyze.py" --profile-dir "$ART/base/profile" >&2 || return 1
  [ -s "$ART/base/bottleneck_report.json" ] || { echo "FATAL: analyze produced no report" >&2; return 1; }
}

step_baseline_ref() {
  [ -s "$ART/baseline/baseline_ref.json" ] && return 0
  if [ -n "$BASELINE_REF_ACC" ]; then
    # validate BEFORE writing: a non-numeric input would land as invalid JSON
    # and only explode much later (final stage) — fail loud here instead
    python3 -c 'import sys; float(sys.argv[1])' "$BASELINE_REF_ACC" 2>/dev/null \
      || { echo "FATAL: --baseline-ref-acc is not a number: '$BASELINE_REF_ACC'" >&2; return 1; }
    printf '{"baseline_ref_acc": %s, "source": "input"}\n' \
      "$BASELINE_REF_ACC" > "$ART/baseline/baseline_ref.json"
  else
    printf '{"baseline_ref_acc": null, "source": "not-provided (auto-trained at the final stage when needed)"}\n' \
      > "$ART/baseline/baseline_ref.json"
  fi
}

step_proxy_worker() { # long body — runs only inside the detached worker
  local out_dir="$ART/baseline/proxy_train"
  mkdir -p "$out_dir"
  # render with the proxy_budget values verbatim (no ckpt — from-scratch training);
  # data_value / max_steps are set only when the contract recorded them
  local render_args=(
    --set vid=baseline --set epochs="$PB_EPOCHS"
    --set out_dir="$out_dir" --set seed="$PB_SEED")
  [ -n "$PB_DATA_VALUE" ] && render_args+=(--set data_value="$PB_DATA_VALUE")
  [ -n "$PB_MAX_STEPS" ] && render_args+=(--set max_steps="$PB_MAX_STEPS")
  render "$ART/templates/run_probe_finetune.template.sh" "$out_dir/.run.rendered.sh" \
    "${render_args[@]}" >&2 || return 1
  bash "$out_dir/.run.rendered.sh" > "$out_dir/train.log" 2>&1 || return 1
  "$PY" "$ART/scripts/metric_curve.py" extract \
    --contract "$ART/contracts.json" --log "$out_dir/train.log" \
    --out "$ART/baseline/baseline_metrics.jsonl" \
    --expected-epochs "$PB_EPOCHS" >&2 || return 1
  local ckpt
  ckpt="$(resolve_ckpt "$out_dir")" || return 1
  render "$ART/templates/run_eval.template.sh" "$out_dir/.eval.rendered.sh" \
    --set ckpt="$ckpt" --set log="$ART/baseline/proxy_eval.log" >&2 || return 1
  bash "$out_dir/.eval.rendered.sh" >> "$ART/baseline/proxy_eval.log" 2>&1 || return 1
  local acc
  acc="$(extract_metric "$ART/baseline/proxy_eval.log")" || return 1
  printf '{"proxy_acc": %s, "ckpt": "%s", "proxy_budget": %s}\n' \
    "$acc" "$ckpt" "$(read_contract 'json.dumps(c["proxy_budget"], sort_keys=True)')" \
    > "$ART/baseline/baseline_proxy_acc.json"
}

verify_anchor_budget() { # the promotion anchor is only reusable when it was
  # trained under the CURRENT contracts.json proxy_budget — a stale anchor
  # (budget rebuilt since) voids the fairness invariant: variants get the
  # current budget, the anchor did not. Field-wise comparison; any mismatch
  # fails loud with rebuild guidance — never a silent skip, never auto-deleted.
  python3 - <<'PY'
import json, sys
from pathlib import Path
def _load(path, what):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        sys.exit(f"FATAL: {what} unreadable ({path}: {exc}) — the anchor's "
                 "training budget cannot be re-verified. Delete "
                 "baseline/baseline_proxy_acc.json and re-run to rebuild "
                 "the anchor.")
anchor = _load("baseline/baseline_proxy_acc.json", "baseline proxy anchor")
recorded = anchor.get("proxy_budget") if isinstance(anchor, dict) else None
current = _load("contracts.json", "contracts.json")["proxy_budget"]
if not isinstance(recorded, dict):
    sys.exit(f"FATAL: baseline_proxy_acc.json records no proxy_budget "
             f"(got {recorded!r}) — the anchor's training budget is unknown, "
             "fair comparison is void. Delete baseline/baseline_proxy_acc.json "
             "and re-run to rebuild the anchor.")
if recorded != current:
    diff = "; ".join(
        f"{k}: anchor={recorded.get(k)!r} vs current={current.get(k)!r}"
        for k in sorted(set(recorded) | set(current))
        if recorded.get(k) != current.get(k))
    sys.exit("FATAL: the baseline proxy anchor was trained under a different "
             f"proxy_budget ({diff}) — fair comparison is void. Delete "
             "baseline/baseline_proxy_acc.json and re-run to rebuild the "
             "anchor (the file is not deleted automatically).")
PY
}

step_target_check() {
  # Relative target: the ratio itself is validated at argument parsing; here we
  # only confirm the baseline makespan is readable (the gate derives the
  # absolute threshold as baseline x (1 - ratio) at decision time).
  local makespan
  makespan="$(python3 -c '
import json
from pathlib import Path
print(json.loads(Path("base/profile/profile_summary.json").read_text(encoding="utf-8"))["makespan_cycles"])')" \
    || { echo "FATAL: cannot read base/profile/profile_summary.json" >&2; return 1; }
  echo "[chain] step7 target check: baseline makespan $makespan readable; relative reduction target $TARGET (derived threshold = $makespan x (1 - $TARGET) at gate time)" >&2
}

# ── detach + bounded poll for long steps ─────────────────────────────────────
worker_rc_file() { echo "$STAMPS/step$1/rc"; }
worker_pid_file() { echo "$STAMPS/step$1/pid"; }
# per-attempt worker log: a re-detach after a crash must never overwrite the
# previous attempt's failure scene (first-attempt evidence stays diagnosable)
worker_log_file() { # worker_log_file <step> <attempt>
  case "$1" in
    3) echo "$STAMPS/step$1/profile_worker.attempt$2.log" ;;
    *) echo "$STAMPS/step$1/train_worker.attempt$2.log" ;;
  esac
}

worker_alive() { # worker_alive <step>
  local pidfile pid
  pidfile="$(worker_pid_file "$1")"
  [ -f "$pidfile" ] || return 1
  pid="$(cat "$pidfile" 2>/dev/null || echo 0)"
  [ "$pid" -gt 0 ] && kill -0 "$pid" 2>/dev/null
}

detach_worker() { # detach_worker <step>
  local n="$1" stamp="$STAMPS/step$1" attempts log
  mkdir -p "$stamp"
  attempts="$(cat "$stamp/attempts" 2>/dev/null || echo 0)"
  attempts=$((attempts + 1)); echo "$attempts" > "$stamp/attempts"
  if [ "$attempts" -gt 3 ]; then
    echo "FATAL: baseline step $n died without finishing $((attempts - 1)) times — see $stamp/*.attempt*.log. Not re-detaching." >&2
    return 1
  fi
  log="$(worker_log_file "$n" "$attempts")"
  local cmd
  cmd="$(printf '%q ' bash "$SELF" --worker-step "$n" --latency-reduction-min "$TARGET" --seed "$SEED")"
  [ -n "$PROFILE_SCRIPT" ] && cmd="$cmd $(printf '%q ' --profile-script "$PROFILE_SCRIPT")"
  [ -n "$REFERENCE" ] && cmd="$cmd $(printf '%q ' --reference-onnx "$REFERENCE")"
  # setsid: own session/process group so the worker survives this bash call; the
  # worker writes its own rc file on exit (worker-step dispatch below).
  setsid bash -c 'echo $$ > "$1"; shift; exec "$@"' \
    bash "$stamp/pid" $cmd </dev/null >>"$log" 2>&1 &
  # race-free wait for the pid file
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do [ -s "$stamp/pid" ] && break; sleep 0.3; done
  echo "[chain] step$n detached (attempt $attempts, pid $(cat "$stamp/pid" 2>/dev/null || echo '?'))" >&2
}

run_long_step() { # run_long_step <step> ; returns 0 done, 1 failed, 2 still running
  local n="$1" rcfile rc elapsed=0
  rcfile="$(worker_rc_file "$n")"
  if [ -f "$rcfile" ]; then
    rc="$(cat "$rcfile" 2>/dev/null || echo 1)"
    [ "$rc" -eq 0 ] && return 0
    echo "FATAL: baseline step $n worker failed rc=$rc (see $STAMPS/step$n/*.attempt*.log)" >&2
    return 1
  fi
  if ! worker_alive "$n"; then
    detach_worker "$n" || return 1
  else
    echo "[chain] step$n worker already running (pid $(cat "$(worker_pid_file "$n")")) — no second detach" >&2
  fi
  while [ "$elapsed" -lt "$POLL_MAX_SECS" ]; do
    [ -f "$rcfile" ] && break
    sleep 10; elapsed=$((elapsed + 10))
  done
  if [ -f "$rcfile" ]; then
    rc="$(cat "$rcfile" 2>/dev/null || echo 1)"
    [ "$rc" -eq 0 ] && return 0
    echo "FATAL: baseline step $n worker failed rc=$rc (see $STAMPS/step$n/*.attempt*.log)" >&2
    return 1
  fi
  echo "[chain] step$n still running after ${POLL_MAX_SECS}s — re-invoke this script to continue polling" >&2
  return 2
}

# ── worker dispatch (synchronous single-step execution) ──────────────────────
if [ -n "$WORKER_STEP" ]; then
  rc=0
  case "$WORKER_STEP" in
    3) step_profile_inline || rc=$? ;;
    6) step_proxy_worker || rc=$? ;;
    *) echo "FATAL: --worker-step $WORKER_STEP is not a long step" >&2; exit 2 ;;
  esac
  echo "$rc" > "$(worker_rc_file "$WORKER_STEP")"
  echo "[worker] step$WORKER_STEP finished rc=$rc" >&2
  exit "$rc"
fi

# ── status view ───────────────────────────────────────────────────────────────
write_status() { # write_status <state-vector description...>
  {
    echo "# baseline chain status"
    echo ""
    echo "updated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo ""
    echo "| step | name | state | product |"
    echo "|---|---|---|---|"
    echo "| 1 | reference-cross-check | $1 | baseline/reference_check.json |"
    echo "| 2 | export + pristine snapshot | $2 | base/model.onnx + baseline/original_shadow/ |"
    echo "| 3 | profile | $3 | base/profile/ |"
    echo "| 4 | analyze | $4 | base/bottleneck_report.json |"
    echo "| 5 | baseline-ref | $5 | baseline/baseline_ref.json |"
    echo "| 6 | baseline-proxy-train | $6 | baseline/baseline_proxy_acc.json |"
    echo "| 7 | target-check | $7 | - |"
    echo ""
    echo "latency_reduction_min: $TARGET; proxy_budget: epochs=$PB_EPOCHS data=${PB_DATA_KNOB:-none}=${PB_DATA_VALUE:-n/a} max_steps=${PB_MAX_STEPS:-none} seed=$PB_SEED; profile_script: ${PROFILE_SCRIPT:-built-in estimator}"
  } > "$ART/baseline_status.md"
}

emit() { # emit <status> <error> — the stdout line is EXACTLY the node
  # output_schema field set (additionalProperties:false): the executed / failed
  # line is the agent's final reply VERBATIM, so any extra key (a bare step
  # number, say) would be rejected downstream. The failing step number is
  # folded into <error> as "baseline step N: ...".
  python3 -c '
import json, sys
from pathlib import Path
art = Path(".")
def num(path, key):
    p = art / path
    if not p.is_file():
        return 0
    try:
        v = json.loads(p.read_text(encoding="utf-8")).get(key)
        return v if isinstance(v, (int, float)) else 0
    except Exception:
        return 0
def nullable(path, key):
    p = art / path
    if not p.is_file():
        return None
    try:
        v = json.loads(p.read_text(encoding="utf-8")).get(key)
        return v if isinstance(v, (int, float)) else None
    except Exception:
        return None
def produced(probe, rel):  # schema: path fields are "" for products not produced
    return str(art / rel) if (art / probe).exists() else ""
generated = [rel for probe, rel in [
    ("base/model.onnx", "base/model.onnx"),
    ("base/profile/profile_summary.json", "base/profile/"),
    ("base/bottleneck_report.json", "base/bottleneck_report.json"),
    ("baseline/original_shadow", "baseline/original_shadow/"),
    ("baseline/reference_check.json", "baseline/reference_check.json"),
    ("baseline/baseline_ref.json", "baseline/baseline_ref.json"),
    ("baseline/baseline_proxy_acc.json", "baseline/baseline_proxy_acc.json"),
    ("baseline/baseline_metrics.jsonl", "baseline/baseline_metrics.jsonl"),
    ("baseline_status.md", "baseline_status.md"),
] if (art / probe).exists()]
print(json.dumps({
    "status": sys.argv[1], "error": sys.argv[2],
    "makespan_cycles": num("base/profile/profile_summary.json", "makespan_cycles"),
    "baseline_proxy_acc": num("baseline/baseline_proxy_acc.json", "proxy_acc"),
    "baseline_ref_acc": nullable("baseline/baseline_ref.json", "baseline_ref_acc"),
    "base_onnx": produced("base/model.onnx", "base/model.onnx"),
    "profile_dir": produced("base/profile/profile_summary.json", "base/profile"),
    "bottleneck_report": produced("base/bottleneck_report.json",
                                  "base/bottleneck_report.json"),
    "baseline_metrics": produced("baseline/baseline_metrics.jsonl",
                                 "baseline/baseline_metrics.jsonl"),
    "generated_artifacts": generated,
}))' "$1" "$2"
}

# ── chain execution ──────────────────────────────────────────────────────────
S1=pending S2=pending S3=pending S4=pending S5=pending S6=pending S7=pending
fail_step=0 fail_err=""

step_reference || { fail_step=1; fail_err="reference cross-check (or its prerequisite shadow export) failed — root cause in the chain stderr of this invocation"; }
[ "$fail_step" -eq 0 ] && { S1=done; step_export || { fail_step=2; fail_err="shadow export failed"; }; }
[ "$fail_step" -eq 0 ] && { S2=done
  if [ -s "$ART/base/profile/profile_summary.json" ]; then
    S3=done; echo "[chain] step3 profile: product exists, skip" >&2
  elif [ -n "$PROFILE_SCRIPT" ]; then
    # custom profiler = potentially long -> detached + polled
    rc=0; run_long_step 3 || rc=$?
    if [ "$rc" -eq 2 ]; then S3=running; write_status done done running "$S4" "$S5" "$S6" "$S7"
      emit running ""; exit 0
    elif [ "$rc" -ne 0 ]; then fail_step=3; fail_err="profiling failed (worker logs: baseline/.stamps/step3/*.attempt*.log)"; fi
  else
    step_profile_inline || { fail_step=3; fail_err="profiling failed (root cause in the chain stderr of this invocation)"; }
  fi; }
[ "$fail_step" -eq 0 ] && { S3=done; step_analyze || { fail_step=4; fail_err="bottleneck analysis failed"; }; }
[ "$fail_step" -eq 0 ] && { S4=done; step_baseline_ref || { fail_step=5; fail_err="baseline reference accuracy record failed"; }; }
[ "$fail_step" -eq 0 ] && { S5=done
  if [ -s "$ART/baseline/baseline_proxy_acc.json" ]; then
    ANCHOR_ERR="$STAMPS/anchor_verify.err"
    if verify_anchor_budget 2>"$ANCHOR_ERR"; then
      S6=done; echo "[chain] step6 proxy train: product exists, skip" >&2
    else
      # capture the verifier's detail so the emitted error (forwarded
      # verbatim as the node output) is self-contained; echo keeps it on
      # stderr for the node transcript as well
      detail="$(tr '\n' ' ' < "$ANCHOR_ERR" 2>/dev/null)"
      detail="${detail#FATAL: }"
      [ -n "$detail" ] || detail="anchor verification failed without a message"
      echo "FATAL: $detail" >&2
      fail_step=6; fail_err="existing baseline proxy anchor is not reusable ($detail) — fair comparison void; delete baseline/baseline_proxy_acc.json and re-run to rebuild the anchor (never auto-deleted)"
    fi
  else
    rc=0; run_long_step 6 || rc=$?
    if [ "$rc" -eq 2 ]; then S6=running; write_status "$S1" "$S2" "$S3" "$S4" "$S5" running "$S7"
      emit running ""; exit 0
    elif [ "$rc" -ne 0 ]; then fail_step=6; fail_err="baseline proxy training failed (worker logs: baseline/.stamps/step6/*.attempt*.log)"; fi
  fi; }
[ "$fail_step" -eq 0 ] && { S6=done; step_target_check || { fail_step=7; fail_err="target check failed: cannot read the baseline makespan from base/profile/profile_summary.json — the relative latency target is derived at gate time from this value"; }; }

if [ "$fail_step" -ne 0 ]; then
  eval "S$fail_step=failed"
  write_status "$S1" "$S2" "$S3" "$S4" "$S5" "$S6" "$S7"
  emit failed "baseline step $fail_step: $fail_err"
  exit 1
fi
S7=done
write_status "$S1" "$S2" "$S3" "$S4" "$S5" "$S6" "$S7"
emit executed ""
exit 0
