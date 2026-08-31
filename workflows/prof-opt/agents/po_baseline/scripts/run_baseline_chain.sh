#!/usr/bin/env bash
# run_baseline_chain.sh — po_baseline non-blocking chain driver.
#
# Steps (each idempotent: the step's product file existing = done, re-entry
# skips it; the product is the authority, baseline_status.md is the view):
#   1 export + pristine snapshot : snapshot the pristine shadow ->
#                                 baseline/original_shadow/ (the round-0
#                                 structure anchor for the write-back diff),
#                                 then render + run the export template ->
#                                 base/model.onnx
#   2 profile (dual-mode)        : four profiling artifacts -> base/profile/
#                                 (mode from profile_mode.json — resolved once
#                                 at entry)
#                                 - placeholder mode: the built-in estimator
#                                   runs inline (unchanged)
#                                 - mfu mode: the raw evaluation products under
#                                   base/profile/<onnx_stem>/ are produced by
#                                   the mfu-analyzer SUBAGENT (the chain never
#                                   runs the evaluation itself), then converted
#                                   by scripts/mfu_adapter.py. States when the
#                                   four-piece is absent:
#                                     raw products present -> adapt (this step)
#                                     report w/o raw       -> FATAL no-fallback
#                                     neither              -> waiting (rc 3):
#                                       the chain emits "running" telling the
#                                       agent to dispatch mfu-analyzer, then
#                                       re-invoke; the placeholder estimator is
#                                       NEVER run in mfu mode
#   3 analyze                    : scripts/analyze.py -> base/bottleneck_report.json
#   4 full-train launch          : claim the first free training device via
#                                 the allocation ledger (vid=baseline lock
#                                 under devices/, backend/count from
#                                 train_device.json), render the train
#                                 contract template at the FULL effective
#                                 epochs (--out baseline/train.rendered.sh,
#                                 --set device=<idx>), wipe any partial
#                                 out-dir (training always starts from
#                                 scratch), detach a wrapper whose group leader
#                                 writes its own pid/rc and does NOT exec:
#                                   setsid bash -c 'echo $$ > train.pid;
#                                     bash train.rendered.sh > train.attemptN.log 2>&1;
#                                     echo $? > train.rc'
#                                 A render failure after the claim releases
#                                 the lock explicitly (a claimed card never
#                                 outlives a failed launch).
#   5 finalizer launch           : detach this script's --finalizer mode
#                                 (setsid, baseline/finalizer.pid + .log)
#   6 liveness confirmation      : train pid alive + finalizer pid alive +
#                                 train log appeared (each /proc cmdline
#                                 attribution-checked). Re-entry equivalence:
#                                 alive OR train_final.json already written —
#                                 done -> confirmed; failed -> emit failed
#                                 (stage attribution); finalizer dead without
#                                 a terminal state -> fail loud.
#   7 emit gate                  : baseline/business_logic.md exists and is
#                                 non-empty (the business-logic-analyst
#                                 subagent's product) — absent -> the chain
#                                 emits the agent-internal "running" line and
#                                 the node re-invokes later.
#
# The chain NEVER waits for the training to finish (non-blocking baseline):
# `executed` = early chain passed + double liveness confirmed +
# business_logic.md on disk — training completion is the detached finalizer's
# job, not this node's.
#
# ── finalizer (this script re-invoked with --finalizer) ──────────────────────
# Self-contained detached guardian (the node has emitted by now; nobody drives
# it, so it must finish the baseline on its own). Every finalizer.log line
# starts with an ISO8601 UTC timestamp (`date -u +%FT%TZ`):
#   poll loop (every cycle): incremental curve extract (FULL re-derive from
#     the CURRENT attempt's train log, atomically replace
#     baseline/baseline_metrics.jsonl only when content changed) +
#     push_curves.py sidecar (best-effort) + alive heartbeat line (with the
#     curve point count)
#   train rc written, rc != 0      -> train_final{failed, rc, stage: train}
#   train group dead WITHOUT rc    -> crash: re-launch <= 3 attempts (per-
#                                     attempt log naming; partial out-dir
#                                     wiped — from-scratch training), then
#                                     train_final{failed, stage: relaunch_exhausted}
#   train rc == 0 -> finalize chain (a stage line per step):
#     final check   : extract --expected-epochs <full effective value>;
#                     actual != rendered -> train_final{failed, stage:
#                     final_check} — the message points at the admission
#                     clause (trainings must execute the rendered epoch count
#                     exactly; early-stopping projects are out of scope)
#     full eval     : last ckpt eval -> baseline/baseline_full_acc.json
#                     {acc, ckpt, full_train_budget fingerprint}
#     k eval        : (train.ckpt_per_epoch) k-th ckpt eval ->
#                     baseline/baseline_k_acc.json {acc, k, ckpt, fingerprint}
#     terminal mark : baseline/train_final.json {status: done, rc: 0, stage}
#   any internal failure -> best-effort train_final{failed, rc, stage} then exit
#
# Every terminal write_train_final ALSO releases the baseline's device lock
# (devices/<idx>.lock, idempotent) — the ledger claim spans exactly the
# training's lifetime. Lock ownership ladder: the claim's placeholder owner
# (the claiming chain invocation) is immediately ADOPTED by the detached
# training wrapper when it comes alive, then by the finalizer (the canonical
# owner — it outlives the training and performs the terminal release); a
# crash relaunch re-adopts the fresh wrapper and hands ownership back to the
# relaunching finalizer. The claim also confirms real occupancy (free ->
# acquire -> idx-in-free-set guard inside device_alloc claim).
#
# rc aggregation: first failing step -> stdout JSON status "failed" with
# "baseline step N: <reason>" folded into `error`; all done -> "executed".
# stdout: ALWAYS exactly one JSON line whose fields are EXACTLY the node
# output_schema field set (the executed / failed line is the agent's final
# reply VERBATIM; additionalProperties:false would reject an extra key). The
# mid-poll "running" line is agent-internal only (not in the schema enum) and
# is never a final node output. All logs to stderr/files.
#
# Usage:
#   run_baseline_chain.sh --latency-reduction-min F --seed N [--finalizer]
# Environment: ORCA_ARTIFACTS_DIR (required). The profiling mode (placeholder
# estimator vs mfu real evaluation) is read from $ORCA_ARTIFACTS_DIR/
# profile_mode.json — resolved once at entry by the flatten node; a missing
# file or an unknown mode is a hard error. The chip/precision/core_num the
# mfu path consumes come from the same file. The user project root comes
# ONLY from readiness/readiness.json — the engine already exports a
# same-purpose project-root env var (it resolves to the Orca repository
# root, NOT the user project), so reading any project-root env here would
# silently anchor to the wrong project.
set -uo pipefail

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (run_baseline_chain.sh)}"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

TARGET=""; SEED="0"; FINALIZER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --latency-reduction-min) TARGET="${2:?}"; shift 2 ;;
    --seed)              SEED="${2:?}"; shift 2 ;;
    --finalizer)         FINALIZER="1"; shift ;;
    *) echo "FATAL: unknown arg $1" >&2; exit 2 ;;
  esac
done
[ -n "$TARGET" ] || { echo "FATAL: --latency-reduction-min is required" >&2; exit 2; }
python3 -c "import sys; r=float('$TARGET'); sys.exit(0 if 0.0 < r < 1.0 else 1)" \
  || { echo "FATAL: --latency-reduction-min must be a number in (0, 1), got '$TARGET'" >&2; exit 2; }
# profiling mode (single source: profile_mode.json, resolved at entry)
NPU_CHIP=""; NPU_PRECISION="INT8"; NPU_CORE_NUM="1"
if [ -z "$FINALIZER" ]; then
  MODE_DOC="$(python3 - "$ART/profile_mode.json" <<'PY'
import json, sys
try:
    doc = json.loads(open(sys.argv[1], encoding="utf-8").read())
except FileNotFoundError as exc:
    raise SystemExit(f"FATAL: {sys.argv[1]} missing — the profiling mode is "
                     f"resolved once at the entry node; re-run it (or "
                     f"rebuild with fresh_start)") from exc
mode = doc.get("mode")
if mode not in ("placeholder", "mfu"):
    raise SystemExit(f"FATAL: profile_mode.json mode must be "
                     f"placeholder|mfu, got {mode!r} — re-run the entry node "
                     f"to re-resolve the profiling mode")
if mode == "mfu":
    print(f"{doc.get('chip', '')} {doc.get('precision', 'INT8')} {doc.get('core_num', 1)}")
PY
)" || exit 2
  if [ -n "$MODE_DOC" ]; then
    NPU_CHIP="$(printf '%s' "$MODE_DOC" | awk '{print $1}')"
    NPU_PRECISION="$(printf '%s' "$MODE_DOC" | awk '{print $2}')"
    NPU_CORE_NUM="$(printf '%s' "$MODE_DOC" | awk '{print $3}')"
  fi
fi
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

for f in contracts.json readiness/readiness.json train_device.json \
         templates/export_onnx.template.sh templates/run_full_finetune.template.sh \
         templates/run_eval.template.sh scripts/render_run.sh scripts/analyze.py \
         scripts/metric_curve.py scripts/push_curves.py scripts/device_alloc.py; do
  [ -f "$f" ] || { echo "FATAL: upstream artifact missing: $ART/$f (contract stage incomplete)" >&2; exit 2; }
done

PY="$(read_contract 'c["interpreter"]["sys_executable"]')"
PROJ_ROOT="$(python3 -c '
import json
from pathlib import Path
print(json.loads(Path("readiness/readiness.json").read_text(encoding="utf-8"))["project_root"])')"
SHADOW_DIR="$ART/shadow"
SHADOW_PKGS="$(read_contract '",".join(c["shadow"]["shadow_pkgs"])')"
# full training budget: the single value-level fingerprint the baseline and
# every later full-budget render must share (fairness invariant)
FULL_EPOCHS="$(read_contract 'c["full_train_budget"]["epochs"]')"
FULL_SEED="$(read_contract 'c["full_train_budget"]["seed"]')"
# probe stop depth k (from the contract's proxy_budget — one source on disk)
PROBE_K="$(read_contract 'c["proxy_budget"]["epochs"]')"
CKPT_PER_EPOCH="$(read_contract 'c["train"]["ckpt_per_epoch"]')"
command -v "$PY" >/dev/null 2>&1 || [ -x "$PY" ] || { echo "FATAL: interpreter not found: $PY" >&2; exit 2; }
python3 -c "import sys; sys.exit(0 if int('$FULL_EPOCHS') >= 1 else 1)" \
  || { echo "FATAL: contracts.json full_train_budget.epochs must be an int >= 1" >&2; exit 2; }

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

resolve_ckpt() { # resolve_ckpt <out_dir> -> prints the LAST ckpt path per the recorded rule
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

resolve_kth_ckpt() { # resolve_kth_ckpt <out_dir> <k> -> the k-th ckpt in write order
  # per-epoch ckpts are written sequentially, so mtime-ascending order IS
  # epoch order; the k-th epoch's ckpt is index k-1
  python3 - "$1" "$2" <<'PY'
import glob, json, os, sys
from pathlib import Path
rule = json.loads(Path("contracts.json").read_text(encoding="utf-8"))["train"]["ckpt_output_rule"]
pattern = rule.replace("{out_dir}", sys.argv[1])
k = int(sys.argv[2])
if "*" not in pattern:
    raise SystemExit("FATAL: per-epoch ckpt addressing needs a glob ckpt_output_rule")
hits = sorted(glob.glob(pattern), key=os.path.getmtime)
if len(hits) < k:
    raise SystemExit(f"FATAL: ckpt rule glob matched {len(hits)} files < k={k}: {pattern}")
print(hits[k - 1])
PY
}

# ── step bodies (each returns rc; product-exists short-circuits) ─────────────
step_export() {
  # pristine shadow snapshot FIRST: the round-0 structure anchor the final
  # write-back diff re-verifies against (kept even though the baseline now
  # trains here at full budget — the anchor is structural, not a spare copy)
  if [ ! -d "$ART/baseline/original_shadow" ]; then
    python3 -c "import shutil; shutil.copytree(
        '$ART/shadow', '$ART/baseline/original_shadow',
        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))" >&2 || return 1
    echo "[chain] step1: pristine shadow snapshotted -> baseline/original_shadow" >&2
  fi
  [ -s "$ART/base/model.onnx" ] && { echo "[chain] step1 export: product exists, skip" >&2; return 0; }
  render "$ART/templates/export_onnx.template.sh" "$ART/base/.export.rendered.sh" \
    --set out="$ART/base/model.onnx" --set seed="$SEED" >&2 || return 1
  bash "$ART/base/.export.rendered.sh" >&2 || return 1
  [ -s "$ART/base/model.onnx" ] || { echo "FATAL: export produced no base/model.onnx" >&2; return 1; }
}

step_profile() { # rc 0 = four-piece on disk; rc 3 = mfu waiting state (agent
  # must dispatch the mfu-analyzer subagent, then re-invoke); rc 1 = failed
  # (PROFILE_FAIL_DETAIL then carries the specific cause into the emit line)
  [ -s "$ART/base/profile/profile_summary.json" ] && { echo "[chain] step2 profile: product exists, skip" >&2; return 0; }
  if [ -z "$NPU_CHIP" ]; then
    local prof="$ART/scripts/placeholder_profiler.py"
    [ -f "$prof" ] || { echo "FATAL: profiling script not found: $prof" >&2; return 1; }
    "$PY" "$prof" --onnx "$ART/base/model.onnx" --out-dir "$ART/base/profile" --seed "$SEED" >&2 || return 1
  else
    local adapter="$ART/scripts/mfu_adapter.py"
    [ -f "$adapter" ] || { echo "FATAL: $adapter not deployed (entry stage incomplete) — mfu mode cannot adapt the raw evaluation products" >&2; PROFILE_FAIL_DETAIL="mfu_adapter not deployed"; return 1; }
    if ls "$ART"/base/profile/*/schedule_result.json >/dev/null 2>&1; then
      local errout
      errout="$("$PY" "$adapter" --profile-dir "$ART/base/profile" 2>&1)" || {
        echo "$errout" >&2
        PROFILE_FAIL_DETAIL="mfu_adapter failed: $(printf '%s' "$errout" | tail -n 1)"
        return 1
      }
      echo "$errout" >&2
    elif [ -s "$ART/base/profile/mfu_bottleneck_report.md" ]; then
      echo "FATAL: mfu-analyzer reported but left no usable raw products under base/profile/ (see base/profile/mfu_bottleneck_report.md) — no placeholder fallback in mfu mode" >&2
      PROFILE_FAIL_DETAIL="mfu-analyzer reported but left no usable raw products under base/profile/ (see base/profile/mfu_bottleneck_report.md) — no placeholder fallback in mfu mode"
      return 1
    else
      echo "[chain] step2 profile: awaiting mfu-analyzer raw products (dispatch mfu-analyzer: onnx=base/model.onnx profile_dir=base/profile report=base/profile/mfu_bottleneck_report.md chip=$NPU_CHIP precision=$NPU_PRECISION core_num=$NPU_CORE_NUM)" >&2
      return 3
    fi
  fi
  for f in taskgraph.json ops.csv schedule.json profile_summary.json; do
    [ -s "$ART/base/profile/$f" ] || { echo "FATAL: profiler did not produce $f" >&2; return 1; }
  done
}

step_analyze() {
  [ -s "$ART/base/bottleneck_report.json" ] && { echo "[chain] step3 analyze: product exists, skip" >&2; return 0; }
  "$PY" "$ART/scripts/analyze.py" --profile-dir "$ART/base/profile" >&2 || return 1
  [ -s "$ART/base/bottleneck_report.json" ] || { echo "FATAL: analyze produced no report" >&2; return 1; }
}

# ── full-training launch (step 4; also the finalizer's crash re-launch) ──────
TRAIN_DIR="$ART/baseline"
TRAIN_RENDERED="$TRAIN_DIR/train.rendered.sh"
TRAIN_OUT="$TRAIN_DIR/train.out"
TRAIN_PID="$TRAIN_DIR/train.pid"
TRAIN_RC="$TRAIN_DIR/train.rc"
TRAIN_ATTEMPTS="$TRAIN_DIR/.train_attempts"
FINALIZER_PID="$TRAIN_DIR/finalizer.pid"
FINALIZER_LOG="$TRAIN_DIR/finalizer.log"
TRAIN_FINAL="$TRAIN_DIR/train_final.json"

pid_alive_owned() { # pid_alive_owned <pidfile> <expect-substr>
  # attribution check: the pid must be alive AND its /proc cmdline must
  # reference the expected artifact (a reused pid from an unrelated process
  # must never be mistaken for our worker)
  local pidfile="$1" expect="$2" pid cmd
  [ -s "$pidfile" ] || return 1
  pid="$(cat "$pidfile" 2>/dev/null || echo 0)"
  [ "$pid" -gt 0 ] 2>/dev/null || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" || return 1
  case "$cmd" in *"$expect"*) return 0 ;; *) return 1 ;; esac
}

train_attempt_log() { echo "$TRAIN_DIR/train.attempt$(cat "$TRAIN_ATTEMPTS" 2>/dev/null || echo 1).log"; }

# ── training-device ledger wiring ────────────────────────────────────────────
# The baseline claims the FIRST FREE device via the run-scoped allocation
# ledger (O_EXCL lock under devices/, vid=baseline) before its full training
# renders; the claim binds the render (--set device=<idx>) and is released by
# the finalizer's terminal write_train_final (idempotent). The backend/count
# come from train_device.json — resolved once at the entry node; a missing
# file fails loud above with the upstream-artifact check.
DEV_IDX_FILE="$TRAIN_DIR/.train_device_idx"

alloc_print_idx() { # prints the claimed device idx (stdout) or fails loud
  # idempotent: a recorded idx whose lock still names vid=baseline is reused
  # (the finalizer's crash relaunch must not claim a SECOND card)
  if [ -s "$DEV_IDX_FILE" ]; then
    local idx lock_vid
    idx="$(cat "$DEV_IDX_FILE" 2>/dev/null || echo -1)"
    lock_vid="$(python3 -c '
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
vid = ""
if p.is_file():
    try:
        vid = json.loads(p.read_text(encoding="utf-8")).get("vid", "")
    except Exception:
        vid = ""
print(vid)' "$ART/devices/$idx.lock" 2>/dev/null || echo "")"
    if [ "$idx" -ge 0 ] 2>/dev/null && [ "$lock_vid" = "baseline" ]; then
      printf '%s' "$idx"
      return 0
    fi
  fi
  local out ok idx
  out="$(python3 "$ART/scripts/device_alloc.py" claim \
      --artifacts "$ART" --vid baseline)" || {
    echo "FATAL: device_alloc claim (vid=baseline) failed — see its stderr above" >&2
    return 1; }
  ok="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("ok"))')"
  if [ "$ok" != "True" ]; then
    echo "FATAL: no free training device for the baseline (busy_real/locked per the claim output above; a stale dead-pid lock is reclaimed by the claim itself)" >&2
    return 1
  fi
  idx="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["idx"])')"
  printf '%s\n' "$idx" > "$DEV_IDX_FILE"
  echo "[chain] training device $idx claimed for vid=baseline (devices/$idx.lock)" >&2
  printf '%s' "$idx"
}

adopt_train_device() { # adopt_train_device <owner-pid> — rebind the claim to a
  # long-lived owner (the wrapper/guardian that just came alive); a claim
  # whose owner is the short-lived chain process is reclaimable by free the
  # moment this invocation exits — an adopt that fails quietly would
  # silently revive exactly that failure mode, so it fails LOUD (rc 1)
  local owner_pid="${1:?adopt_train_device needs an owner pid}"
  [ -s "$DEV_IDX_FILE" ] || return 0
  python3 "$ART/scripts/device_alloc.py" adopt \
      --artifacts "$ART" --vid baseline --pid "$owner_pid" >/dev/null \
    || { echo "FATAL: device lock adopt failed for vid=baseline pid=$owner_pid — the claim stays bound to the claiming process and becomes reclaimable; refusing to continue" >&2
         return 1; }
}

release_train_device() { # idempotent terminal sweep (the finalizer's terminal
  # action; the render-failure path in launch_full_train calls it too)
  if [ -s "$DEV_IDX_FILE" ]; then
    local idx
    idx="$(cat "$DEV_IDX_FILE" 2>/dev/null || echo -1)"
    if [ "$idx" -ge 0 ] 2>/dev/null; then
      python3 "$ART/scripts/device_alloc.py" release \
          --artifacts "$ART" --idx "$idx" >/dev/null \
        || echo "WARN: device lock release failed for idx=$idx (the report sweep covers it)" >&2
    fi
    rm -f "$DEV_IDX_FILE"
  fi
}

launch_full_train() { # claim device + render (idempotent) + wipe partial out-dir + detach wrapper
  mkdir -p "$TRAIN_DIR"
  local dev_idx
  dev_idx="$(alloc_print_idx)" || return 1
  if ! render "$ART/templates/run_full_finetune.template.sh" "$TRAIN_RENDERED" \
      --set vid=baseline --set epochs="$FULL_EPOCHS" \
      --set out_dir="$TRAIN_OUT" --set seed="$FULL_SEED" \
      --set device="$dev_idx" >&2; then
    release_train_device   # a claimed card never outlives a failed render
    return 1
  fi
  # from-scratch training: a partial out-dir from a dead attempt would leave
  # mixed-attempt checkpoints under the glob rule — wipe before every launch
  if [ -e "$TRAIN_OUT" ]; then
    echo "[chain] wiping partial training out-dir (from-scratch resume rule): $TRAIN_OUT" >&2
    rm -rf "$TRAIN_OUT"
  fi
  mkdir -p "$TRAIN_OUT"
  local attempts log
  attempts=$(( $(cat "$TRAIN_ATTEMPTS" 2>/dev/null || echo 0) + 1 ))
  echo "$attempts" > "$TRAIN_ATTEMPTS"
  log="$TRAIN_DIR/train.attempt${attempts}.log"
  rm -f "$TRAIN_RC"
  # wrapper group leader does NOT exec: pid and rc each have their own writer
  # (a killed group leaves rc absent = distinguishable from a failed run)
  setsid bash -c '
      echo $$ > "$1"; shift
      bash "$1" > "$2" 2>&1
      echo $? > "$3"
    ' bash "$TRAIN_PID" "$TRAIN_RENDERED" "$log" "$TRAIN_RC" \
    </dev/null >>"$TRAIN_DIR/wrapper.attempt${attempts}.log" 2>&1 &
  local i
  # launch confirmed = wrapper alive OR already finished (a fast-crashing or
  # fast-completing train legitimately exits before this check; rc present
  # means the wrapper ran — that is the finalizer's business now, not a
  # launch failure)
  for i in 1 2 3 4 5 6 7 8 9 10; do
    { pid_alive_owned "$TRAIN_PID" "train.rendered.sh" || [ -s "$TRAIN_RC" ]; } && break
    sleep 0.3
  done
  { pid_alive_owned "$TRAIN_PID" "train.rendered.sh" || [ -s "$TRAIN_RC" ]; } \
    || { echo "FATAL: full-train wrapper did not come alive (see $TRAIN_DIR/wrapper.attempt${attempts}.log)" >&2; return 1; }
  # the wrapper owns the claim from here (long-lived for the whole training;
  # superseded by the finalizer adopt at step 5). A FAST-finishing wrapper
  # (rc already written — quick crash or completion) is dead by definition:
  # ownership passes to the finalizer at step 5 instead, never a dead pid.
  if [ -s "$TRAIN_PID" ] && [ ! -s "$TRAIN_RC" ]; then
    adopt_train_device "$(cat "$TRAIN_PID")" || return 1
  fi
  echo "[chain] full-train detached (attempt $attempts, pid $(cat "$TRAIN_PID"), device=$dev_idx epochs=$FULL_EPOCHS seed=$FULL_SEED)" >&2
}

launch_finalizer() { # detach this script's --finalizer mode (own session);
  # the wrapper leader writes its own pid then execs the finalizer (same pid)
  mkdir -p "$TRAIN_DIR"
  setsid bash -c 'echo $$ > "$1"; shift; exec "$@"' \
    bash "$FINALIZER_PID" "$SELF" --finalizer \
    --latency-reduction-min "$TARGET" --seed "$SEED" \
    </dev/null >>"$FINALIZER_LOG" 2>&1 &
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    pid_alive_owned "$FINALIZER_PID" "--finalizer" && break
    sleep 0.3
  done
  pid_alive_owned "$FINALIZER_PID" "--finalizer" || { echo "FATAL: finalizer did not come alive (see $FINALIZER_LOG)" >&2; return 1; }
  # the finalizer outlives the training AND the terminal release — it is the
  # canonical lock owner from here on (supersedes the wrapper adopt)
  adopt_train_device "$(cat "$FINALIZER_PID")" || return 1
  echo "[chain] finalizer detached (pid $(cat "$FINALIZER_PID"))" >&2
}

step_train_launch() {
  if [ -f "$TRAIN_RC" ] || pid_alive_owned "$TRAIN_PID" "train.rendered.sh" || [ -f "$TRAIN_FINAL" ]; then
    echo "[chain] step4 train launch: already launched (rc file, live pid, or terminal state present), skip" >&2
    return 0
  fi
  launch_full_train
}

step_finalizer_launch() {
  if pid_alive_owned "$FINALIZER_PID" "--finalizer" || [ -f "$TRAIN_FINAL" ]; then
    echo "[chain] step5 finalizer: already running or terminal, skip" >&2
    return 0
  fi
  launch_finalizer
}

verify_full_acc_fingerprint() { # train_final done but the anchor must carry the
  # CURRENT full_train_budget — a stale anchor (budget rebuilt since) voids
  # the fairness invariant exactly like the old proxy anchor did
  python3 - "$TRAIN_DIR/baseline_full_acc.json" <<'PY'
import json, sys
from pathlib import Path
def _load(path, what):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        sys.exit(f"FATAL: {what} unreadable ({path}: {exc}) — the full-training "
                 "anchor cannot be re-verified. Delete baseline/train_final.json "
                 "and baseline/baseline_full_acc.json and re-run.")
current = json.loads(Path("contracts.json").read_text(encoding="utf-8"))["full_train_budget"]
anchor = _load(sys.argv[1], "baseline full-acc anchor")
recorded = anchor.get("full_train_budget")
if recorded != current:
    sys.exit("FATAL: baseline_full_acc.json was trained under a different "
             "full_train_budget — fair comparison is void. Delete "
             "baseline/train_final.json and baseline/baseline_full_acc.json "
             "and re-run (never auto-deleted).")
PY
}

# ── finalizer mode (detached guardian; see the header contract) ──────────────
fin_log() { # fin_log <message> — every line starts with an ISO8601 UTC stamp
  echo "$(date -u +%FT%TZ) $*" >> "$FINALIZER_LOG"
}

write_train_final() { # write_train_final <status> <rc-or-null> <stage> [message]
  python3 - "$TRAIN_FINAL" "$1" "$2" "$3" "${4:-}" <<'PY'
import json, os, sys, tempfile
status = {"status": sys.argv[2], "rc": None if sys.argv[3] == "null" else int(sys.argv[3]),
          "stage": sys.argv[4]}
if sys.argv[5]:
    status["message"] = sys.argv[5]
path = sys.argv[1]
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(status, fh, sort_keys=True)
os.replace(tmp, path)
PY
  fin_log "stage=train_final status=$1 stage_name=$3 msg=${4:-}"
  # terminal action: the ledger claim spans exactly the training's lifetime
  local rel_idx
  rel_idx="$(cat "$DEV_IDX_FILE" 2>/dev/null || echo none)"
  release_train_device
  fin_log "stage=device_release idx=$rel_idx"
}

incremental_curve() { # full re-derive from the CURRENT attempt log; atomic
  # replace only on content change (idempotent pushes see a stable file)
  local log tmp
  log="$(train_attempt_log)"
  [ -s "$log" ] || return 0
  tmp="$TRAIN_DIR/.baseline_metrics.tmp"
  if "$PY" "$ART/scripts/metric_curve.py" extract \
      --contract "$ART/contracts.json" --log "$log" --out "$tmp" >/dev/null 2>&1; then
    if [ ! -s "$TRAIN_DIR/baseline_metrics.jsonl" ] || ! cmp -s "$tmp" "$TRAIN_DIR/baseline_metrics.jsonl"; then
      mv -f "$tmp" "$TRAIN_DIR/baseline_metrics.jsonl"
    else
      rm -f "$tmp"
    fi
  else
    rm -f "$tmp"   # early-state log (no epoch line yet) — next cycle retries
  fi
}

curve_points() {
  python3 -c '
import sys
from pathlib import Path
p = Path(sys.argv[1])
print(sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip()) if p.is_file() else 0)
' "$TRAIN_DIR/baseline_metrics.jsonl" 2>/dev/null || echo 0
}

finalizer_main() {
  mkdir -p "$TRAIN_DIR"
  # already terminal (crash between train_final write and finalizer exit)
  [ -f "$TRAIN_FINAL" ] && { fin_log "finalizer: train_final already present — exiting"; exit 0; }
  fin_log "finalizer alive: polling full-train (epochs=$FULL_EPOCHS seed=$FULL_SEED k=$PROBE_K)"
  local relaunches=0
  while true; do
    if [ -s "$TRAIN_RC" ]; then
      local rc; rc="$(cat "$TRAIN_RC")"
      fin_log "stage=train_exit rc=$rc"
      if [ "$rc" -ne 0 ]; then
        write_train_final failed "$rc" train
        exit 0
      fi
      break   # rc == 0 -> finalize chain below
    fi
    if ! pid_alive_owned "$TRAIN_PID" "train.rendered.sh"; then
      # died without an rc file: crash scene (kill -9 / OOM). Re-launch <= 3.
      relaunches=$((relaunches + 1))
      if [ "$relaunches" -gt 3 ]; then
        fin_log "stage=relaunch_exhausted attempts=$relaunches"
        write_train_final failed null relaunch_exhausted
        exit 0
      fi
      fin_log "stage=relaunch attempt=$relaunches (train group died without rc)"
      # sub-command output silenced: finalizer.log lines start ISO8601 only
      launch_full_train >/dev/null 2>&1 || {
        write_train_final failed null relaunch_failed; exit 0; }
      # the relaunch re-adopted the new wrapper; take ownership back so the
      # claim survives until THIS finalizer's terminal write. A failed adopt
      # here is a FATAL (the claim would sit on the dying wrapper's pid) —
      # terminal-fail the baseline rather than continue unsupervised.
      if ! adopt_train_device "$$" >/dev/null 2>&1; then
        fin_log "stage=relaunch_adopt verdict=failed (device lock adopt failed — the claim is stranded on the relaunched wrapper's pid)"
        write_train_final failed null relaunch_adopt_failed
        exit 0
      fi
    fi
    incremental_curve
    "$PY" "$ART/scripts/push_curves.py" --artifacts "$ART" \
      >/dev/null 2>>"$TRAIN_DIR/.chart_push.err" || true
    fin_log "alive curve_points=$(curve_points)"
    sleep 10
  done

  # ── rc == 0: finalize chain (a stage line per step) ───────────────────────
  fin_log "stage=final_check expected_epochs=$FULL_EPOCHS"
  # tmp + atomic replace (same write discipline as the incremental cycle —
  # a concurrent curve reader never sees a half-written file)
  if ! "$PY" "$ART/scripts/metric_curve.py" extract \
      --contract "$ART/contracts.json" --log "$(train_attempt_log)" \
      --out "$TRAIN_DIR/.baseline_metrics.final.tmp" \
      --expected-epochs "$FULL_EPOCHS" >/dev/null 2>&1 \
      || ! mv -f "$TRAIN_DIR/.baseline_metrics.final.tmp" \
                "$TRAIN_DIR/baseline_metrics.jsonl"; then
    rm -f "$TRAIN_DIR/.baseline_metrics.final.tmp"
    fin_log "stage=final_check verdict=failed (actual epochs != rendered $FULL_EPOCHS)"
    write_train_final failed 0 final_check \
      "final check failed: the training log does not carry exactly the rendered $FULL_EPOCHS epochs — 训练须按给定轮数精确执行，自带 early-stopping 的项目不在 workflow 范围（准入条款见 contracts.json reason）"
    exit 0
  fi
  "$PY" "$ART/scripts/push_curves.py" --artifacts "$ART" \
    >/dev/null 2>>"$TRAIN_DIR/.chart_push.err" || true

  fin_log "stage=full_eval"
  local ckpt acc
  ckpt="$(resolve_ckpt "$TRAIN_OUT")" || {
    fin_log "stage=full_eval verdict=failed (last ckpt unresolvable)"
    write_train_final failed 0 full_eval; exit 0; }
  render "$ART/templates/run_eval.template.sh" "$TRAIN_DIR/.full_eval.rendered.sh" \
    --set ckpt="$ckpt" --set log="$TRAIN_DIR/full_eval.log" >/dev/null 2>&1 || {
    write_train_final failed 0 full_eval; exit 0; }
  bash "$TRAIN_DIR/.full_eval.rendered.sh" >>"$TRAIN_DIR/full_eval.log" 2>&1 || {
    fin_log "stage=full_eval verdict=failed (eval run rc != 0)"
    write_train_final failed 0 full_eval; exit 0; }
  acc="$(extract_metric "$TRAIN_DIR/full_eval.log")" || {
    write_train_final failed 0 full_eval; exit 0; }
  python3 - "$TRAIN_DIR/baseline_full_acc.json" "$acc" "$ckpt" <<'PY' || { write_train_final failed 0 full_eval; exit 0; }
import json, os, sys, tempfile
from pathlib import Path
budget = json.loads(Path("contracts.json").read_text(encoding="utf-8"))["full_train_budget"]
doc = {"baseline_full_acc": float(sys.argv[2]), "ckpt": sys.argv[3],
       "full_train_budget": budget}
path = sys.argv[1]
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, sort_keys=True)
os.replace(tmp, path)
PY
  fin_log "stage=full_eval acc=$acc ckpt=$ckpt"

  if [ "$CKPT_PER_EPOCH" = "True" ]; then
    fin_log "stage=k_eval k=$PROBE_K"
    local kckpt kacc
    kckpt="$(resolve_kth_ckpt "$TRAIN_OUT" "$PROBE_K")" || {
      write_train_final failed 0 k_eval; exit 0; }
    render "$ART/templates/run_eval.template.sh" "$TRAIN_DIR/.k_eval.rendered.sh" \
      --set ckpt="$kckpt" --set log="$TRAIN_DIR/k_eval.log" >/dev/null 2>&1 || {
      write_train_final failed 0 k_eval; exit 0; }
    bash "$TRAIN_DIR/.k_eval.rendered.sh" >>"$TRAIN_DIR/k_eval.log" 2>&1 || {
      write_train_final failed 0 k_eval; exit 0; }
    kacc="$(extract_metric "$TRAIN_DIR/k_eval.log")" || {
      write_train_final failed 0 k_eval; exit 0; }
    python3 - "$TRAIN_DIR/baseline_k_acc.json" "$kacc" "$kckpt" "$PROBE_K" <<'PY' || { write_train_final failed 0 k_eval; exit 0; }
import json, os, sys, tempfile
from pathlib import Path
budget = json.loads(Path("contracts.json").read_text(encoding="utf-8"))["full_train_budget"]
doc = {"baseline_k_acc": float(sys.argv[2]), "ckpt": sys.argv[3], "k": int(sys.argv[4]),
       "full_train_budget": budget}
path = sys.argv[1]
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, sort_keys=True)
os.replace(tmp, path)
PY
    fin_log "stage=k_eval acc=$kacc k=$PROBE_K ckpt=$kckpt"
  else
    fin_log "stage=k_eval skipped (train.ckpt_per_epoch false — probe judges on the curve alone)"
  fi

  write_train_final done 0 done
  fin_log "finalizer: baseline finalized (status=done)"
  exit 0
}

if [ -n "$FINALIZER" ]; then
  finalizer_main
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
    echo "| 1 | export + pristine snapshot | $1 | base/model.onnx + baseline/original_shadow/ |"
    echo "| 2 | profile | $2 | base/profile/ |"
    echo "| 3 | analyze | $3 | base/bottleneck_report.json |"
    echo "| 4 | full-train launch (non-blocking) | $4 | baseline/train.rendered.sh + train.pid |"
    echo "| 5 | finalizer launch | $5 | baseline/finalizer.pid + finalizer.log |"
    echo "| 6 | liveness confirmation | $6 | - |"
    echo "| 7 | emit gate (business_logic.md) | $7 | baseline/business_logic.md |"
    echo ""
    echo "finalizer tail: $(tail -n 2 "$FINALIZER_LOG" 2>/dev/null | tr '\n' ' ')"
    echo "train device: $(cat "$DEV_IDX_FILE" 2>/dev/null || echo '-') (ledger claim vid=baseline)"
    echo "full_train_budget: epochs=$FULL_EPOCHS seed=$FULL_SEED; probe k=$PROBE_K; ckpt_per_epoch=$CKPT_PER_EPOCH; profile mode: ${NPU_CHIP:+mfu (chip=$NPU_CHIP precision=$NPU_PRECISION core_num=$NPU_CORE_NUM)}${NPU_CHIP:-placeholder estimator}"
  } > "$ART/baseline_status.md"
}

emit() { # emit <status> <error> — the stdout line is EXACTLY the node
  # output_schema field set (additionalProperties:false): the executed / failed
  # line is the agent's final reply VERBATIM. The failing step number is
  # folded into <error> as "baseline step N: ...".
  python3 -c '
import json, sys
from pathlib import Path
art = Path(".")
generated = [rel for probe, rel in [
    ("base/model.onnx", "base/model.onnx"),
    ("base/profile/profile_summary.json", "base/profile/"),
    ("base/bottleneck_report.json", "base/bottleneck_report.json"),
    ("baseline/original_shadow", "baseline/original_shadow/"),
    ("baseline/train.rendered.sh", "baseline/train.rendered.sh"),
    ("baseline/train.pid", "baseline/train.pid"),
    ("baseline/finalizer.pid", "baseline/finalizer.pid"),
    ("baseline/baseline_metrics.jsonl", "baseline/baseline_metrics.jsonl"),
    ("baseline/baseline_full_acc.json", "baseline/baseline_full_acc.json"),
    ("baseline/baseline_k_acc.json", "baseline/baseline_k_acc.json"),
    ("baseline/train_final.json", "baseline/train_final.json"),
    ("baseline/business_logic.md", "baseline/business_logic.md"),
    ("baseline_status.md", "baseline_status.md"),
] if (art / probe).exists()]
print(json.dumps({
    "status": sys.argv[1], "error": sys.argv[2],
    "generated_artifacts": generated,
}))' "$1" "$2"
}

# ── chain execution ──────────────────────────────────────────────────────────
S1=pending S2=pending S3=pending S4=pending S5=pending S6=pending S7=pending
fail_step=0 fail_err=""
PROFILE_FAIL_DETAIL=""

step_export || { fail_step=1; fail_err="shadow export failed"; }
[ "$fail_step" -eq 0 ] && { S1=done
  rc2=0; step_profile || rc2=$?
  if [ "$rc2" -eq 3 ]; then
    # mfu waiting state: the raw evaluation products are the mfu-analyzer
    # SUBAGENT's product, not the chain's — hand control back to the agent
    # (which dispatches the analyzer) and expect a re-invocation
    S2=running; write_status "$S1" running "$S3" "$S4" "$S5" "$S6" "$S7"
    emit running "baseline step 2: awaiting mfu-analyzer raw products — dispatch the mfu-analyzer subagent (onnx=base/model.onnx, profile_dir=base/profile, report=base/profile/mfu_bottleneck_report.md), then re-invoke"
    exit 0
  elif [ "$rc2" -ne 0 ]; then
    fail_step=2
    if [ -n "$PROFILE_FAIL_DETAIL" ]; then
      fail_err="profiling failed — $PROFILE_FAIL_DETAIL"
    else
      fail_err="profiling failed (root cause in the chain stderr of this invocation)"
    fi
  fi; }
[ "$fail_step" -eq 0 ] && { S2=done; step_analyze || { fail_step=3; fail_err="bottleneck analysis failed"; }; }
[ "$fail_step" -eq 0 ] && { S3=done; step_train_launch || { fail_step=4; fail_err="full training launch failed"; }; }
[ "$fail_step" -eq 0 ] && { S4=done; step_finalizer_launch || { fail_step=5; fail_err="finalizer launch failed"; }; }
[ "$fail_step" -eq 0 ] && { S5=done
  # liveness confirmation with re-entry equivalence (alive OR train_final)
  if [ -f "$TRAIN_FINAL" ]; then
    T_STATUS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' "$TRAIN_FINAL")"
    if [ "$T_STATUS" = "done" ]; then
      if verify_full_acc_fingerprint 2>"$STAMPS/fingerprint.err"; then
        S6=done   # training finished, terminal state recorded, anchor fresh
        echo "[chain] step6 liveness: train_final=done and anchor fingerprint verified" >&2
      else
        detail="$(tr '\n' ' ' < "$STAMPS/fingerprint.err" 2>/dev/null | head -c 200)"
        fail_step=6; fail_err="stale full-training anchor ($detail)"
      fi
    else
      T_STAGE="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("stage","?"))' "$TRAIN_FINAL")"
      T_MSG="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("message",""))' "$TRAIN_FINAL")"
      fail_step=6; fail_err="baseline full training failed (train_final stage: $T_STAGE${T_MSG:+ — $T_MSG}; see baseline/finalizer.log)"
    fi
  elif pid_alive_owned "$TRAIN_PID" "train.rendered.sh" && pid_alive_owned "$FINALIZER_PID" "--finalizer"; then
    # wait briefly for the train log to appear (interpreter startup latency)
    logwait=0
    while [ "$logwait" -lt 15 ]; do
      [ -s "$(train_attempt_log)" ] && break
      sleep 1; logwait=$((logwait + 1))
    done
    if [ -s "$(train_attempt_log)" ]; then
      S6=done
      echo "[chain] step6 liveness: train pid + finalizer pid alive, train log present — confirmed (non-blocking: NOT waiting for completion)" >&2
    else
      write_status "$S1" "$S2" "$S3" "$S4" "$S5" running "$S7"
      emit running "baseline step 6: workers alive but train log not yet on disk — re-invoke"
      exit 0
    fi
  elif pid_alive_owned "$FINALIZER_PID" "--finalizer"; then
    # train group gone but the finalizer is still running: either the
    # training just ended (rc present — the finalizer is finalizing) or it
    # crashed without rc (the finalizer owns the relaunch). Both transient.
    write_status "$S1" "$S2" "$S3" "$S4" "$S5" running "$S7"
    if [ -s "$TRAIN_RC" ]; then
      emit running "baseline step 6: training ended (rc=$(cat "$TRAIN_RC" 2>/dev/null)), finalizer finalizing — re-invoke"
    else
      emit running "baseline step 6: train group dead without rc, finalizer alive (relaunch in progress) — re-invoke"
    fi
    exit 0
  else
    fail_step=6; fail_err="finalizer exited without a terminal state (no train_final.json, finalizer pid dead) — see baseline/finalizer.log"
  fi; }
[ "$fail_step" -eq 0 ] && { S6=done
  # emit gate: the business-logic document is a HARD precondition of executed
  if [ -s "$ART/baseline/business_logic.md" ]; then
    S7=done
  else
    S7=running; write_status "$S1" "$S2" "$S3" "$S4" "$S5" "$S6" running
    emit running "baseline step 7: business_logic.md not yet on disk (business-logic-analyst in flight) — re-invoke"
    exit 0
  fi; }

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
