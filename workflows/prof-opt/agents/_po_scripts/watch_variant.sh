#!/usr/bin/env bash
# watch_variant.sh — detached per-variant training watchdog (v6 §7).
#
# Detached by po_probe right after the training wrapper (setsid; this script
# self-writes watchdog.pid, every watchdog.log line starts with an ISO8601
# UTC stamp — the baseline finalizer's lifecycle pattern). Its five duties:
#
#   §7.1 lifecycle   supervise the training; a train group that dies WITHOUT
#                    an rc file is a crash scene -> re-launch <= 3 attempts
#                    (partial checkpoint artifacts wiped per the contract's
#                    ckpt_output_rule, the crashed attempt's log archived,
#                    training always restarts from scratch) -> exhausted:
#                    terminal probe_insufficient. rc != 0 is an honest
#                    failure exit (same policy as the baseline finalizer):
#                    terminal probe_insufficient, no re-launch.
#   §7.2 per-epoch   every cycle (sleep 10): metric_curve extract (full
#                    re-parse of the CURRENT attempt log, atomic replace of
#                    variants/<VID>/metrics/metrics.jsonl) -> compare with
#                    baseline/baseline_metrics.jsonl at each NEW common epoch
#                    (budget = origin anchor accuracy_budget, direction =
#                    contracts eval.metric_direction) -> gap = normalized
#                    loss. Warmup: epochs <= ceil(0.1 x E) are never judged
#                    and never counted. Counting is one count per EPOCH,
#                    never per poll (a stalled epoch must not inflate the
#                    streak): gap <= budget -> streak = 0; gap > budget ->
#                    streak += 1. streak >= 10 -> kill the process group
#                    (TERM -> 10s grace -> KILL, /proc cmdline attribution
#                    check first — the inherited v5 kill semantics) ->
#                    stopped_at_epoch = the FROZEN log's re-parsed max epoch
#                    (lines written between the kill decision and the
#                    group's death are real trained epochs) -> terminal
#                    accuracy_fail.
#   §7.3 terminal    rc == 0 -> final eval chain (resolve last ckpt per the
#                    contract rule + render the eval template with the
#                    VARIANT's shadow + extract the metric per the contract
#                    rule — the baseline finalizer's chain) -> write
#                    variants/<VID>/eval/final_acc.json (within_budget null)
#                    -> verdict_decide.py final-budget backfills the verdict
#                    -> success (within budget) or accuracy_fail.
#   §7.4 side        every cycle: atomically update train_status.json and the
#                    variant's ledger SHARD variants/<VID>/ledger_entry.json
#                    (single writer: this watchdog owns the shard; the
#                    change_summary the proposal node seeded is preserved
#                    verbatim — only epoch/metric/gap/status/device/ts are
#                    touched) -> push_curves.py (best-effort live line). At
#                    the terminal: aggregate the derived ledger, release the
#                    device lock, append the typed history terminal row, and
#                    drop the variants/<VID>/.rules_pending marker for the
#                    proposal node's rules refresh to consume.
#   §7.6 idempotence re-entry reads train_status.json / pid / rc first: a
#                    terminal stage (killed|done|failed) is replayed verbatim
#                    (no restart, no re-kill, no re-release); a live training
#                    resumes supervision; an already-released lock is never
#                    released again.
#
# The early-stop curve stays a PREFIX of the full-budget render (fairness
# invariant): the training renders at the SAME full_train_budget the baseline
# trained under — only the kill is early, never the budget.
#
# Usage:
#   watch_variant.sh --vid <VID> --device <IDX> [--once]
# --once: run exactly ONE supervision cycle against the current disk state
#   and exit (stdout: the cycle's status JSON). The tests drive the judgment
#   boundaries through it; the detached guardian never uses it.
#
# Environment: ORCA_ARTIFACTS_DIR (required — the run workspace root).
# stdout: ALWAYS exactly one JSON line (the cycle / terminal / replay
# status). Hard errors exit 2 with a FATAL line on stderr + watchdog.log;
# the early-stop attribution check failing is exactly such a FATAL (§14:
# refuse to kill, never touch the terminal state — a torn workspace is the
# report sweep's business, not something to paper over here).
set -uo pipefail

VID=""; DEVICE=""; ONCE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --vid)    VID="${2:?--vid needs a value}"; shift 2 ;;
    --device) DEVICE="${2:?--device needs a value}"; shift 2 ;;
    --once)   ONCE="1"; shift ;;
    --help)
      echo "usage: watch_variant.sh --vid <VID> --device <IDX> [--once]"
      echo "detached per-variant training watchdog (env: ORCA_ARTIFACTS_DIR)"
      exit 0 ;;
    *)
      echo "FATAL: unknown argument $1 (usage: watch_variant.sh --vid <VID> --device <IDX> [--once])" >&2
      exit 2 ;;
  esac
done
[ -n "$VID" ] || { echo "FATAL: --vid is required" >&2; exit 2; }
[ -n "$DEVICE" ] || { echo "FATAL: --device is required" >&2; exit 2; }
case "$DEVICE" in
  ''|*[!0-9]*)
    echo "FATAL: --device must be a non-negative integer, got '$DEVICE'" >&2; exit 2 ;;
esac

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (watch_variant.sh)}"
VDIR="$ART/variants/$VID"
TRAIN_DIR="$VDIR/train"
TLOG="$TRAIN_DIR/train.log"
TPID="$TRAIN_DIR/train.pid"
TRC="$TRAIN_DIR/rc"
RENDERED="$TRAIN_DIR/train.rendered.sh"
ATTEMPTS="$TRAIN_DIR/.train_attempts"
METRICS="$VDIR/metrics/metrics.jsonl"
STATUS="$VDIR/train_status.json"
SHARD="$VDIR/ledger_entry.json"
WPID="$VDIR/watchdog.pid"
WLOG="$VDIR/watchdog.log"
EDIR="$VDIR/eval"
FINAL_ACC="$EDIR/final_acc.json"
RULES_PENDING="$VDIR/.rules_pending"
BASELINE_FULL="$ART/baseline/baseline_full_acc.json"
BASELINE_FINAL="$ART/baseline/train_final.json"

mkdir -p "$VDIR" "$TRAIN_DIR" "$VDIR/metrics" "$EDIR"
echo $$ > "$WPID"

wlog() { # wlog <message> — every line starts with an ISO8601 UTC stamp
  echo "$(date -u +%FT%TZ) $*" >> "$WLOG"
}

fatal() { # fatal <message> — log + stderr + exit 2, never a silent patch
  wlog "FATAL: $*"
  echo "FATAL: $*" >&2
  exit 2
}

# ── contract / anchor readers (fail loud when the workspace is torn) ─────────
contract_field() { # contract_field <python-expr over c> -> value
  python3 -c '
import json, sys
from pathlib import Path
try:
    c = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(eval(sys.argv[2], {"json": json}, {"c": c}))  # noqa: S307 — fixed caller-supplied expr
except Exception as exc:
    print(f"FATAL: contracts.json field missing: {exc}", file=sys.stderr)
    sys.exit(2)
' "$ART/contracts.json" "$1"
}

PY="$(contract_field 'c["interpreter"]["sys_executable"]')" || exit 2
EPOCHS="$(contract_field 'c["full_train_budget"]["epochs"]')" || exit 2
DIRECTION="$(contract_field 'c["eval"]["metric_direction"]')" || exit 2
SHADOW_PKGS="$(contract_field '",".join(c["shadow"]["shadow_pkgs"])')" || exit 2
CKPT_RULE="$(contract_field 'c["train"]["ckpt_output_rule"]')" || exit 2
BUDGET="$(python3 -c '
import json, sys
from pathlib import Path
try:
    anchor = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    budget = float(anchor["accuracy_budget"])
except Exception as exc:
    sys.exit(f"FATAL: origin anchor unreadable ({exc}) — the accuracy budget is the frozen anchor, never a guess")
if budget < 0:
    sys.exit("FATAL: origin anchor accuracy_budget must be >= 0")
print(budget)' "$ART/base/origin_anchor.json")" || exit 2
PROJ_ROOT="$(python3 -c '
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["project_root"])' \
  "$ART/readiness/readiness.json")" || { echo "FATAL: readiness/readiness.json unreadable" >&2; exit 2; }
python3 -c "import sys; sys.exit(0 if int('$EPOCHS') >= 1 else 1)" \
  || { echo "FATAL: contracts.json full_train_budget.epochs must be an int >= 1" >&2; exit 2; }

for f in contracts.json readiness/readiness.json templates/run_eval.template.sh \
         scripts/metric_curve.py scripts/verdict_decide.py scripts/history_lib.py \
         scripts/ledger_aggregate.py scripts/device_alloc.py scripts/render_run.sh \
         baseline/baseline_metrics.jsonl; do
  [ -f "$ART/$f" ] || { echo "FATAL: upstream artifact missing: $ART/$f (contract stage incomplete or launch torn)" >&2
                        wlog "FATAL startup: missing $f"; exit 2; }
done
[ -f "$RENDERED" ] || { echo "FATAL: $RENDERED missing (no rendered training to supervise)" >&2
                        wlog "FATAL startup: train.rendered.sh missing"; exit 2; }

wlog "watchdog alive: vid=$VID device=$DEVICE pid=$$ epochs=$EPOCHS budget=$BUDGET direction=$DIRECTION${ONCE:+ (once mode)}"

# ── small shared helpers ──────────────────────────────────────────────────────
print_status() { # the on-disk train_status.json, one sorted JSON line
  python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1], encoding="utf-8")), sort_keys=True))' "$STATUS"
}

stage_now() { # the terminal stage when train_status.json carries one, else ""
  [ -f "$STATUS" ] || { echo ""; return 0; }
  python3 -c '
import json, sys
try:
    doc = json.loads(open(sys.argv[1], encoding="utf-8").read())
except Exception as exc:
    sys.exit(f"FATAL: train_status.json unparseable: {exc} (single-writer file — a torn read is a real anomaly)")
stage = doc.get("stage", "")
if stage not in ("waiting", "training", "killed", "done", "failed"):
    sys.exit(f"FATAL: train_status.json stage {stage!r} is outside the v6 enum")
print(stage if stage in ("killed", "done", "failed") else "")' "$STATUS" || exit 2
}

pid_from_file() { cat "$TPID" 2>/dev/null || echo 0; }

group_alive() { # group_alive <pid>
  [ "$1" -gt 0 ] 2>/dev/null && kill -0 "-$1" 2>/dev/null
}

cmdline_matches() { # cmdline_matches <pid> <expect-substr>
  local cmd
  cmd="$(tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null)" || return 1
  [ -n "$cmd" ] || return 1
  case "$cmd" in *"$2"*) return 0 ;; *) return 1 ;; esac
}

max_epoch() { # the CURRENT train log's max complete epoch, via the same
  # contract-reading parser metric_curve extract uses (single source)
  python3 - "$ART" "$TLOG" <<'PY'
import sys
from pathlib import Path
art = Path(sys.argv[1])
sys.path.insert(0, str(art / "scripts"))
import metric_curve

log = Path(sys.argv[2])
if not log.is_file():
    print(0)
    raise SystemExit(0)
try:
    pattern = metric_curve._contract_pattern(art / "contracts.json")
    points = metric_curve._extract(log, pattern)
except metric_curve.MetricCurveError as exc:
    if "no epoch metric matched" in str(exc):
        print(0)
        raise SystemExit(0)
    print(f"metric_curve: FAIL {exc}", file=sys.stderr)
    raise SystemExit(2) from exc
print(max(int(p["epoch"]) for p in points))
PY
}

write_status() { # write_status <stage> <epoch> <metric> <gap> <streak> <stopped>
  # the literal token "keep" preserves the value already recorded for that
  # field (a failed extract cycle must not silently wipe the streak — the
  # early stop would be postponed by a transient parse failure)
  python3 - "$STATUS" "$VID" "$1" "$2" "$3" "$4" "$5" "$6" "$DEVICE" <<'PY' \
    || fatal "writing train_status.json failed"
import json, os, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
previous = {}
if path.is_file():
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.exit(f"FATAL: train_status.json unparseable: {exc}")

def maybe(raw, conv, key):
    if raw == "keep":
        return previous.get(key)
    return None if raw in ("", "null", "None") else conv(raw)

doc = {"vid": sys.argv[2], "stage": sys.argv[3],
       "epoch": maybe(sys.argv[4], int, "epoch"),
       "metric": maybe(sys.argv[5], float, "metric"),
       "gap": maybe(sys.argv[6], float, "gap"),
       "over_budget_streak": maybe(sys.argv[7], int, "over_budget_streak"),
       "stopped_at_epoch": maybe(sys.argv[8], int, "stopped_at_epoch"),
       "device": int(sys.argv[9]),
       "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
fd, tmp = tempfile.mkstemp(dir=str(path.parent))
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, sort_keys=True)
os.replace(tmp, path)
PY
}

update_shard() { # update_shard <status> [epoch] [metric] [gap] — the variant's
  # single-writer ledger shard; the proposal-seeded change_summary is the one
  # field preserved verbatim (it is not this writer's to touch). The literal
  # token "keep" preserves the recorded epoch/metric/gap (same wipe guard as
  # write_status).
  python3 - "$SHARD" "$VID" "$1" "${2:-null}" "${3:-null}" "${4:-null}" "$DEVICE" <<'PY' \
    || fatal "updating the ledger shard failed"
import json, os, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
existing = {}
if path.is_file():
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.exit(f"FATAL: ledger shard unparseable: {path} ({exc}) — single-writer file, never patched around")
if not isinstance(existing, dict):
    sys.exit(f"FATAL: ledger shard is not a JSON object: {path}")

def maybe(raw, conv, key):
    if raw == "keep":
        return existing.get(key)
    return None if raw in ("null", "None", "") else conv(raw)

doc = {"vid": sys.argv[2], "status": sys.argv[3],
       "epoch": maybe(sys.argv[4], int, "epoch"),
       "metric": maybe(sys.argv[5], float, "metric"),
       "gap": maybe(sys.argv[6], float, "gap"),
       "device": int(sys.argv[7]),
       "change_summary": existing.get("change_summary"),
       "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
fd, tmp = tempfile.mkstemp(dir=str(path.parent))
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, ensure_ascii=False, sort_keys=True)
os.replace(tmp, path)
PY
}

push_curves() { # best-effort live line (never stalls the guardian)
  "$PY" "$ART/scripts/push_curves.py" --artifacts "$ART" \
    >/dev/null 2>>"$VDIR/.chart_push.err" || true
}

release_device() { # idempotent terminal sweep (§7.6: no double release)
  python3 "$ART/scripts/device_alloc.py" release \
      --artifacts "$ART" --idx "$DEVICE" >/dev/null \
    || { wlog "WARN: device lock release failed for idx=$DEVICE (the report sweep covers it)"; return 0; }
  wlog "stage=device_release idx=$DEVICE"
}

append_history() { # append_history <outcome> <kwarg-json> — typed builder only
  python3 - "$ART" "$VID" "$1" "$2" <<'PY'
import json, sys
from pathlib import Path
art = Path(sys.argv[1])
sys.path.insert(0, str(art / "scripts"))
from history_lib import append_terminal

append_terminal(art / "history.jsonl", sys.argv[2],
                outcome=sys.argv[3], **json.loads(sys.argv[4]))
PY
}

# ── the terminal tail (§7.4) ──────────────────────────────────────────────────
# status + shard + push + aggregate + release + history row + rules marker.
# The shard lands BEFORE the aggregate so the derived ledger already carries
# the terminal row it collects.
finalize_terminal() { # finalize_terminal <outcome> <stage> <epoch> <metric> <gap> <streak> <stopped> <history-kwargs-json>
  local outcome="$1" stage="$2" epoch="$3" metric="$4" gap="$5" streak="$6" stopped="$7" kwargs="$8"
  write_status "$stage" "$epoch" "$metric" "$gap" "$streak" "$stopped"
  update_shard "$outcome" "$epoch" "$metric" "$gap"
  push_curves
  python3 "$ART/scripts/ledger_aggregate.py" --artifacts "$ART" >/dev/null 2>&1 \
    || wlog "WARN: ledger aggregate failed at terminal (the next trigger point converges — shard data is intact)"
  release_device
  append_history "$outcome" "$kwargs" \
    || fatal "history append_terminal failed for vid=$VID outcome=$outcome (no terminal row was written)"
  printf '{"vid": "%s", "outcome": "%s", "ts": "%s"}\n' \
    "$VID" "$outcome" "$(date -u +%FT%TZ)" > "$RULES_PENDING"
  wlog "stage=terminal outcome=$outcome stage_name=$stage epoch=$epoch stopped_at=$stopped gap=$gap streak=$streak"
  print_status
}

finalize_probe_insufficient() { # finalize_probe_insufficient <stage-name> <max-retries-hit> [epoch]
  local st="$1" epoch="${3:-null}" retries
  case "$2" in True|true) retries="true" ;; *) retries="false" ;; esac
  finalize_terminal probe_insufficient failed "$epoch" null null null null \
    "{\"stage\": \"$st\", \"max_retries_hit\": $retries}"
}

# ── §7.3 terminal eval chain (the baseline finalizer's, on the variant) ──────
resolve_ckpt() { # the LAST ckpt per the contract's recorded rule
  python3 - "$TRAIN_DIR" "$CKPT_RULE" <<'PY'
import glob, os, sys
from pathlib import Path
pattern = sys.argv[2].replace("{out_dir}", sys.argv[1])
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

extract_metric() { # extract_metric <log> — the metric per the contracts rule
  python3 - "$ART/contracts.json" "$1" <<'PY'
import json, re, sys
from pathlib import Path
rule = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["eval"]["metric_extraction"]
text = Path(sys.argv[2]).read_text(encoding="utf-8", errors="replace")
if rule["kind"] == "stdout_regex":
    m = re.search(rule["pattern"], text, re.MULTILINE)
    if not m:
        raise SystemExit(f"FATAL: metric regex did not match in {sys.argv[2]}")
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

finalize_natural() { # rc == 0 — final check, eval chain, verdict, terminal.
  # Exits on every terminal path; RETURNS 0 only on the waiting path.
  # The baseline's full-acc anchor may still be pending (the baseline trains
  # asynchronously too): WAIT for it, never judge against a guessed number.
  # Bounded wait: a baseline that already reached ITS terminal state without
  # producing the anchor (train_final.json present, anchor absent — the
  # baseline training failed, or the workspace is torn) will never produce
  # it; waiting forever would hold the card with no verdict possible, so the
  # variant closes as probe_insufficient naming the root cause.
  if [ ! -s "$BASELINE_FULL" ]; then
    if [ -f "$BASELINE_FINAL" ]; then
      wlog "stage=final_eval verdict=probe_insufficient (baseline reached train_final without baseline_full_acc.json — the comparison anchor is unreachable)"
      finalize_probe_insufficient baseline_anchor_unavailable False "$EPOCHS"
      exit 0
    fi
    wlog "stage=final_eval_waiting (baseline_full_acc.json not yet on disk)"
    write_status waiting null null null null null
    update_shard training null null null
    push_curves
    print_status
    return 0
  fi

  # final check: the log must prove exactly the rendered epoch count
  if ! "$PY" "$ART/scripts/metric_curve.py" extract \
        --contract "$ART/contracts.json" --log "$TLOG" \
        --out "$METRICS.tmp" --expected-epochs "$EPOCHS" >/dev/null 2>&1 \
     || ! mv -f "$METRICS.tmp" "$METRICS"; then
    rm -f "$METRICS.tmp"
    wlog "stage=final_check verdict=probe_insufficient (actual epochs != rendered $EPOCHS)"
    finalize_probe_insufficient final_check False "$(max_epoch)"
    exit 0
  fi
  local ckpt acc base_full verdict gap
  ckpt="$(resolve_ckpt)" || { wlog "stage=full_eval verdict=probe_insufficient (last ckpt unresolvable)"; \
    finalize_probe_insufficient final_eval False "$EPOCHS"; exit 0; }
  if ! ( cd "$ART" && bash "$ART/scripts/render_run.sh" \
        --template "$ART/templates/run_eval.template.sh" \
        --out "$EDIR/.final_eval.rendered.sh" \
        --set ckpt="$ckpt" --set log="$EDIR/final_eval.log" \
        --set shadow_dir="$VDIR/shadow" --set shadow_pkgs="$SHADOW_PKGS" \
        --set project_root="$PROJ_ROOT" --set python="$PY" ); then
    wlog "stage=full_eval verdict=probe_insufficient (eval render failed)"
    finalize_probe_insufficient final_eval False "$EPOCHS"; exit 0
  fi
  if ! bash "$EDIR/.final_eval.rendered.sh" >>"$EDIR/final_eval.log" 2>&1; then
    wlog "stage=full_eval verdict=probe_insufficient (eval run rc != 0)"
    finalize_probe_insufficient final_eval False "$EPOCHS"; exit 0
  fi
  acc="$(extract_metric "$EDIR/final_eval.log")" || { \
    wlog "stage=full_eval verdict=probe_insufficient (metric extraction failed)"; \
    finalize_probe_insufficient final_eval False "$EPOCHS"; exit 0; }
  base_full="$(python3 -c '
import json, sys
from pathlib import Path
doc = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(float(doc["baseline_full_acc"]))' "$BASELINE_FULL")" || { \
    wlog "stage=full_eval verdict=probe_insufficient (baseline_full_acc unreadable)"; \
    finalize_probe_insufficient final_eval False "$EPOCHS"; exit 0; }
  # §7.3: write the record with within_budget null FIRST, then let the
  # verdict call compute + backfill it
  python3 - "$FINAL_ACC" "$VID" "$acc" "$base_full" "$DIRECTION" "$ART/contracts.json" <<'PY' || { \
    wlog "stage=full_eval verdict=probe_insufficient (final_acc.json write failed)"; \
    finalize_probe_insufficient final_eval False "$EPOCHS"; exit 0; }
import json, os, sys, tempfile
from pathlib import Path
doc = {"vid": sys.argv[2], "final_acc": float(sys.argv[3]),
       "baseline_full_acc": float(sys.argv[4]), "metric_direction": sys.argv[5],
       "full_train_budget": json.loads(Path(sys.argv[6]).read_text(encoding="utf-8"))["full_train_budget"],
       "within_budget": None}
path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=str(path.parent))
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, sort_keys=True)
os.replace(tmp, path)
PY
  "$PY" "$ART/scripts/verdict_decide.py" final-budget --artifacts "$ART" --vid "$VID" \
    >/dev/null || { wlog "stage=full_eval verdict=probe_insufficient (final-budget verdict failed)"; \
    finalize_probe_insufficient final_eval False "$EPOCHS"; exit 0; }
  verdict="$(python3 -c '
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["within_budget"])' "$FINAL_ACC")"
  # direction-normalized final gap (positive = worse) — the single-source
  # formula metric_curve owns (compare / early stop / verdict all share it)
  gap="$(python3 - "$ART" "$base_full" "$acc" "$DIRECTION" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "scripts"))
import metric_curve

print(metric_curve.normalize_loss(float(sys.argv[2]), float(sys.argv[3]),
                                  sys.argv[4]))
PY
)" || fatal "computing the final gap failed"
  wlog "stage=full_eval acc=$acc baseline_full=$base_full within_budget=$verdict gap=$gap ckpt=$ckpt"
  if [ "$verdict" = "True" ]; then
    finalize_terminal success done "$EPOCHS" "$acc" "$gap" null "$EPOCHS" \
      "{\"gap\": $gap, \"stopped_at_epoch\": $EPOCHS, \"final_acc\": $acc}"
  else
    finalize_terminal accuracy_fail done "$EPOCHS" "$acc" "$gap" null "$EPOCHS" \
      "{\"gap\": $gap, \"stopped_at_epoch\": $EPOCHS}"
  fi
  exit 0
}

# ── §7.1 crash re-launch (train group dead WITHOUT an rc file) ───────────────
relaunch_train() { # rc 0 = relaunched; rc 1 = budget exhausted
  local attempts
  attempts=$(( $(cat "$ATTEMPTS" 2>/dev/null || echo 0) + 1 ))
  echo "$attempts" > "$ATTEMPTS"
  [ "$attempts" -gt 3 ] && return 1
  wlog "stage=relaunch attempt=$attempts (train group died without rc)"
  # the crashed attempt's log is archived (from-scratch resume: the fresh log
  # restarts at epoch 1 — appending would break contiguity parsing)
  [ -s "$TLOG" ] && mv -f "$TLOG" "$TRAIN_DIR/train.crash${attempts}.log"
  # wipe PARTIAL checkpoints per the contract's rule — never the control files
  python3 - "$TRAIN_DIR" "$CKPT_RULE" <<'PY'
import glob, os, sys
pattern = sys.argv[2].replace("{out_dir}", sys.argv[1])
for hit in glob.glob(pattern):
    os.remove(hit)
PY
  rm -f "$TRC" "$METRICS"
  setsid bash -c '
      echo $$ > "$1"; shift
      bash "$1" > "$2" 2>&1
      echo $? > "$3"
    ' bash "$TPID" "$RENDERED" "$TLOG" "$TRC" \
    </dev/null >>"$TRAIN_DIR/wrapper.log" 2>&1 &
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    { group_alive "$(pid_from_file)" && cmdline_matches "$(pid_from_file)" "train.rendered.sh"; } && break
    sleep 0.3
  done
  # the guardian owns the claim from here (it outlives the training and does
  # the terminal release); a failed adopt strands the claim on the dying
  # wrapper — fail loud rather than supervise an unowned card
  if ! python3 "$ART/scripts/device_alloc.py" adopt \
        --artifacts "$ART" --vid "$VID" --pid "$$" >/dev/null; then
    fatal "device lock adopt failed for vid=$VID pid=$$ after relaunch (claim stranded — see devices/)"
  fi
  # from-scratch training: the progress ledger restarts with it
  write_status training 0 null null 0 null
  update_shard training 0 null null
  wlog "stage=relaunch attempt=$attempts pid=$(pid_from_file) ok"
  return 0
}

# ── §7.2 early-stop kill (attribution first) ───────────────────────────────────
early_stop_kill() { # early_stop_kill <gap> <streak> <epoch>; rc 1 = the training
  # died by itself first (the next cycle reads its rc)
  local gap="$1" streak="$2" epoch="$3" pid grace frozen
  pid="$(pid_from_file)"
  if ! group_alive "$pid"; then
    return 1
  fi
  if ! cmdline_matches "$pid" "train.rendered.sh"; then
    fatal "refusing to kill pid $pid — /proc cmdline does not reference 'train.rendered.sh' (pid reuse or wrong pid file: $TPID); no terminal written (torn workspace)"
  fi
  wlog "stage=early_stop streak=$streak gap=$gap: TERM process group $pid"
  kill -TERM "-$pid" 2>/dev/null || true
  grace=0
  while group_alive "$pid" && [ "$grace" -lt 10 ]; do
    sleep 1; grace=$((grace + 1))
  done
  if group_alive "$pid"; then
    wlog "stage=early_stop group survived ${grace}s grace — KILL process group $pid"
    kill -KILL "-$pid" 2>/dev/null || true
    grace=0
    while group_alive "$pid" && [ "$grace" -lt 5 ]; do
      sleep 1; grace=$((grace + 1))
    done
  fi
  # the log is frozen — re-parse for the TERMINAL depth (lines written between
  # the kill decision and the group's death are real trained epochs)
  frozen="$(max_epoch)" || fatal "re-parsing the frozen log $TLOG after killing group $pid failed"
  if [ "$frozen" -lt "$epoch" ]; then
    fatal "frozen log re-parse found max epoch $frozen < the epoch the kill decided on ($epoch) — inconsistent log state"
  fi
  finalize_terminal accuracy_fail killed "$frozen" null "$gap" "$streak" "$frozen" \
    "{\"gap\": $gap, \"stopped_at_epoch\": $frozen, \"over_budget_streak\": $streak}"
  exit 0
}

# ── one supervision cycle (exits on terminal/FATAL; returns to keep looping) ──
supervise_cycle() {
  # §7.6 terminal replay: never restart, never re-kill, never re-release
  local stage
  stage="$(stage_now)" || exit 2
  if [ -n "$stage" ]; then
    wlog "terminal already present (stage=$stage) — replaying, nothing to do"
    print_status
    exit 0
  fi

  # rc written: the wrapper finished — its rc is the branch selector
  if [ -s "$TRC" ]; then
    local rc
    rc="$(cat "$TRC")"
    wlog "stage=train_exit rc=$rc"
    if [ "$rc" -ne 0 ]; then
      finalize_probe_insufficient train False "$(max_epoch)"
      exit 0
    fi
    finalize_natural     # exits on its terminal paths; returns only while waiting
    return 0
  fi

  # crash scene: group dead WITHOUT an rc file -> re-launch <= 3. Liveness is
  # group-alive only here — the SAME model the v5 stopper used for its waiting
  # branches (attribution is the KILL-time check: an unattributed live pid is
  # refused there, never killed; relaunching here on it would race a second
  # training against the kill decision).
  if ! group_alive "$(pid_from_file)"; then
    if ! relaunch_train; then
      wlog "stage=relaunch_exhausted attempts=3"
      finalize_probe_insufficient relaunch_exhausted True "$(max_epoch)"
      exit 0
    fi
    print_status
    return 0
  fi

  # alive: incremental curve (FULL re-parse of the current attempt log; atomic
  # replace only on change — an unchanged file keeps idempotent pushes stable)
  local curve_ok=""
  if "$PY" "$ART/scripts/metric_curve.py" extract \
        --contract "$ART/contracts.json" --log "$TLOG" \
        --out "$METRICS.tmp" >/dev/null 2>&1; then
    if [ ! -s "$METRICS" ] || ! cmp -s "$METRICS.tmp" "$METRICS"; then
      mv -f "$METRICS.tmp" "$METRICS"
    else
      rm -f "$METRICS.tmp"
    fi
    curve_ok="1"
  else
    rm -f "$METRICS.tmp"   # early-state log (no epoch line yet) — retry next cycle
  fi

  # per-epoch judgment scan (one count per NEW epoch, warmup skipped)
  local scan epoch streak gap metric
  scan="$(python3 - "$ART" "$VID" "$EPOCHS" "$DIRECTION" "$BUDGET" <<'PY'
import json, sys
from pathlib import Path
art, vid = Path(sys.argv[1]), sys.argv[2]
epochs, direction, budget = int(sys.argv[3]), sys.argv[4], float(sys.argv[5])
sys.path.insert(0, str(art / "scripts"))
import metric_curve

def emit(**doc):
    print(json.dumps(doc, sort_keys=True))
    raise SystemExit(0)

status_path = art / "variants" / vid / "train_status.json"
prev_epoch, prev_streak = 0, 0
try:
    if status_path.is_file():
        doc = json.loads(status_path.read_text(encoding="utf-8"))
        prev_epoch = int(doc.get("epoch") or 0)
        prev_streak = int(doc.get("over_budget_streak") or 0)
except Exception as exc:
    raise SystemExit(f"FATAL: train_status.json unparseable: {exc}")

try:
    base = metric_curve.load_curve(art / "baseline" / "baseline_metrics.jsonl")
    cand = metric_curve.load_curve(art / "variants" / vid / "metrics" / "metrics.jsonl")
except metric_curve.MetricCurveError as exc:
    emit(epoch=prev_epoch, streak=prev_streak, gap=None, metric=None,
         stop=False, skip=f"curve not ready: {exc}")

base_m = {int(r["epoch"]): float(r["metric"]) for r in base}
cand_m = {int(r["epoch"]): float(r["metric"]) for r in cand}
common = sorted(set(base_m) & set(cand_m))
if not common:
    emit(epoch=prev_epoch, streak=prev_streak, gap=None, metric=None,
         stop=False, skip="no common epoch with the baseline yet")

warmup = -(-epochs // 10)          # ceil(0.1 x E)
streak, gap, metric, upto, stop = prev_streak, None, None, prev_epoch, False
for e in common:
    if e <= prev_epoch:
        continue
    if e <= warmup:                # §7.2 warmup: never judged, never counted
        upto = e
        continue
    b, c = base_m[e], cand_m[e]
    loss = metric_curve.normalize_loss(b, c, direction)
    streak = 0 if loss <= budget else streak + 1
    gap, metric, upto = loss, c, e
    if streak >= 10:
        stop = True
        break
emit(epoch=upto, streak=streak, gap=gap, metric=metric, stop=stop)
PY
)" || fatal "the per-epoch judgment scan failed"

  read -r epoch streak gap metric <<< "$(python3 -c '
import json, sys
d = json.loads(sys.argv[1])
print(" ".join("null" if d[k] is None else str(d[k])
               for k in ("epoch", "streak", "gap", "metric")))' "$scan")"

  if [ "$curve_ok" = "1" ] \
     && [ "$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("stop"))' "$scan")" = "True" ]; then
    early_stop_kill "$gap" "$streak" "$epoch"
    # a race let the training die first — fall through to the side effects
  fi
  if [ "$curve_ok" = "1" ]; then
    write_status training "$epoch" "$metric" "$gap" "$streak" null
    update_shard training "$epoch" "$metric" "$gap"
  else
    # transient extract failure: KEEP the recorded progress (a null rewrite
    # would silently reset the streak and postpone the early stop)
    write_status training keep keep keep keep null
    update_shard training keep keep keep
  fi
  push_curves
  wlog "alive epoch=$epoch streak=$streak gap=$gap"
  print_status
  return 0
}

supervise_cycle
if [ -n "$ONCE" ]; then
  exit 0
fi
# detached guardian: one cycle every 10s (§7.2)
while true; do
  sleep 10
  supervise_cycle
done
