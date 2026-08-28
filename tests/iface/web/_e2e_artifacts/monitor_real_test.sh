#!/bin/bash
# Real isolated test of monitor_until_done.sh — drives the ACTUAL deliverable script
# against realistic fake training scenarios. Proves the monitoring-reform contract.
set -u

MONITOR="/mnt/d/Projects/Orca/workflows/nas-supernet/agents/ns_run_train/scripts/monitor_until_done.sh"
TMPROOT="$(mktemp -d)"
echo "TMPROOT=$TMPROOT"

# status.sh invocation logger (cheap-liveness proof)
STATUS_LOG="$TMPROOT/status_invocations.log"
: > "$STATUS_LOG"

setup() {
  local scenario="$1"
  local art="$TMPROOT/$scenario"
  mkdir -p "$art/runs/train" "$art/agent_res/scripts"
  # mock status.sh that logs each invocation + emits configurable token
  cat > "$art/agent_res/scripts/status.sh" <<SH
#!/bin/bash
echo "\$(date +%s) invoked" >> "$STATUS_LOG"
cat "$TMPROOT/$scenario/.status_out"
SH
  chmod +x "$art/agent_res/scripts/status.sh"
  echo "1" > "$art/runs/train/.train_attempt"
  echo "$art"
}

echo ""
echo "=========================================="
echo "SCENARIO B: process exited + rc=0 + status=TRAIN_COMPLETE"
echo "=========================================="
ART="$(setup B)"
echo "TRAIN_COMPLETE ckpt=$ART/runs/train/supernet_best.pth" > "$TMPROOT/B/.status_out"
# no .train_pid (process gone), .train_rc present
echo "0" > "$ART/runs/train/.train_rc"
touch "$ART/runs/train/train.attempt1.log"
invocations_before=$(wc -l < "$STATUS_LOG")
out=$(ORCA_ARTIFACTS_DIR="$ART" ORCA_AGENT_RESOURCES="$ART/agent_res" bash "$MONITOR" 2>&1)
rc=$?
invocations_after=$(wc -l < "$STATUS_LOG")
echo "STDOUT: $out"
echo "exit: $rc"
echo "status.sh invocations: before=$invocations_before after=$invocations_after (delta=$((invocations_after-invocations_before)))"
[[ "$out" == *COMPLETE* ]] && echo "VERDICT: PASS (*COMPLETE* emitted)" || echo "VERDICT: FAIL"

echo ""
echo "=========================================="
echo "SCENARIO C: process exited + status=TRAIN_INCOMPLETE"
echo "=========================================="
ART="$(setup C)"
echo "TRAIN_INCOMPLETE rc=1 no ckpt" > "$TMPROOT/C/.status_out"
echo "1" > "$ART/runs/train/.train_rc"
touch "$ART/runs/train/train.attempt1.log"
out=$(ORCA_ARTIFACTS_DIR="$ART" ORCA_AGENT_RESOURCES="$ART/agent_res" bash "$MONITOR" 2>&1)
rc=$?
echo "STDOUT: $out"
echo "exit: $rc"
[[ "$out" == *INCOMPLETE* ]] && echo "VERDICT: PASS (*INCOMPLETE* emitted)" || echo "VERDICT: FAIL"

echo ""
echo "=========================================="
echo "SCENARIO D: alive process + log has nan -> TRAIN_STUCK"
echo "=========================================="
ART="$(setup D)"
sleep 300 &
FAKEPID=$!
echo "$FAKEPID" > "$ART/runs/train/.train_pid"
# log with nan (no .train_rc — process alive)
printf "epoch 1 loss=2.3\nepoch 2 loss=nan\n" > "$ART/runs/train/train.attempt1.log"
invocations_before=$(wc -l < "$STATUS_LOG")
out=$(ORCA_ARTIFACTS_DIR="$ART" ORCA_AGENT_RESOURCES="$ART/agent_res" bash "$MONITOR" 2>&1)
rc=$?
invocations_after=$(wc -l < "$STATUS_LOG")
echo "STDOUT: $out"
echo "exit: $rc"
echo "status.sh invocations delta=$((invocations_after-invocations_before)) (expect 0 — cheap liveness, no status.sh while alive)"
kill $FAKEPID 2>/dev/null
[[ "$out" == *STUCK* ]] && echo "VERDICT: PASS (*STUCK* emitted, nan detected)" || echo "VERDICT: FAIL"

echo ""
echo "=========================================="
echo "SCENARIO E: alive process + log grows -> STILL_RUNNING + status.sh NOT called (cheap liveness)"
echo "=========================================="
ART="$(setup E)"
sleep 300 &
FAKEPID=$!
echo "$FAKEPID" > "$ART/runs/train/.train_pid"
printf "epoch 1 loss=2.3\n" > "$ART/runs/train/train.attempt1.log"
invocations_before=$(wc -l < "$STATUS_LOG")
# short budget: 1 cycle (~60s sleep) then deadline
out=$(ORCA_ARTIFACTS_DIR="$ART" ORCA_AGENT_RESOURCES="$ART/agent_res" ORCA_MONITOR_BUDGET_S=50 bash "$MONITOR" 2>&1)
rc=$?
invocations_after=$(wc -l < "$STATUS_LOG")
echo "STDOUT: $out"
echo "exit: $rc"
echo "status.sh invocations delta=$((invocations_after-invocations_before)) (expect 0 — cheap liveness)"
kill $FAKEPID 2>/dev/null
[[ "$out" == *STILL_RUNNING* ]] && echo "VERDICT: PASS (STILL_RUNNING)" || echo "VERDICT: FAIL"
[[ $((invocations_after-invocations_before)) -eq 0 ]] && echo "VERDICT: PASS (status.sh not called while alive — cheap liveness)" || echo "VERDICT: FAIL (status.sh called while alive — perf contract broken)"

echo ""
echo "=========================================="
echo "SCENARIO F: GATE_SKIP (only train node)"
echo "=========================================="
ART="$(setup F)"
echo "GATE_SKIP no train this run" > "$TMPROOT/F/.status_out"
echo "0" > "$ART/runs/train/.train_rc"
touch "$ART/runs/train/train.attempt1.log"
out=$(ORCA_ARTIFACTS_DIR="$ART" ORCA_AGENT_RESOURCES="$ART/agent_res" bash "$MONITOR" 2>&1)
rc=$?
echo "STDOUT: $out"
echo "exit: $rc"
[[ "$out" == GATE_SKIP* ]] && echo "VERDICT: PASS (GATE_SKIP)" || echo "VERDICT: FAIL"

echo ""
echo "=========================================="
echo "SCENARIO G: status-ambiguous (status.sh returns garbage -> defensive STILL_RUNNING)"
echo "=========================================="
ART="$(setup G)"
echo "garbage-unknown-token" > "$TMPROOT/G/.status_out"
echo "0" > "$ART/runs/train/.train_rc"
touch "$ART/runs/train/train.attempt1.log"
out=$(ORCA_ARTIFACTS_DIR="$ART" ORCA_AGENT_RESOURCES="$ART/agent_res" bash "$MONITOR" 2>&1)
rc=$?
echo "STDOUT: $out"
echo "exit: $rc"
[[ "$out" == "STILL_RUNNING status-ambiguous" ]] && echo "VERDICT: PASS (defensive STILL_RUNNING, non-empty stdout)" || echo "VERDICT: FAIL"

echo ""
echo "=========================================="
echo "SCENARIO H: artifacts-unreachable (bad ORCA_ARTIFACTS_DIR -> STILL_RUNNING, non-empty)"
echo "=========================================="
out=$(ORCA_ARTIFACTS_DIR="/nonexistent/path/xyz" ORCA_AGENT_RESOURCES="/tmp" bash "$MONITOR" 2>&1)
rc=$?
echo "STDOUT: $out"
echo "exit: $rc"
[[ "$out" == *STILL_RUNNING* ]] && echo "VERDICT: PASS (fail-soft STILL_RUNNING)" || echo "VERDICT: FAIL"

echo ""
echo "=== cleanup ==="
rm -rf "$TMPROOT"
echo "DONE"
