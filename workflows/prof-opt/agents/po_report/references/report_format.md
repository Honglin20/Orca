# Report Format And Terminal-State Protocol

The report node is the single terminal reporter: every path (success and
every failure mode) converges here. **Zero cross-node output references** —
the terminal state is derived ONLY from the workspace on disk. Paths are
relative to the workspace root (`$ORCA_ARTIFACTS_DIR`) unless absolute. Angle-bracket
placeholders (`<project-root>`) are
runtime values from your node prompt's input anchors — substitute the actual
values. Write-back is a fixed behavior, not an input: it runs on every
success terminal; nothing you substitute controls it.

Build the report with ONE python script you write at entry:
`$ORCA_ARTIFACTS_DIR/report_builder.py` (English identifiers, `pathlib`,
stdlib only — the optional live chart push may import the orchestrator's
chart module when its env is present). The script implements this document
mechanically, is safe to re-run (write-back is idempotent by the same-content
rule), and prints the final single-line JSON on stdout. Your reply is that
line verbatim.

The report's FIRST section is a disclosure block carrying two verbatim
records: `profile_mode.json` (the profiling configuration every number in
this report was measured under — mode / chip / precision / core_num /
resolved_by) and the deployed scripts' version stamp (`scripts/.VERSION`
manifest hash). No judgement, just provenance.

## 0. Terminal harvest (before the state table)

Read `baseline/finalizer.pid`: dead → pass (the baseline's terminal state is
whatever `baseline/train_final.json` says); alive → wait ≤ 60 s for the
terminal state, then kill the baseline training group (`baseline/train.pid`),
the finalizer group, and every in-flight variant group
(`variants/*/train/train.pid`). Every kill is attribution-GUARDED: the pid's
/proc cmdline must reference `train.rendered.sh` (training wrappers) or
`--finalizer` (finalizer) before signalling — a dead, reused, or unrelated
pid is skipped and named in the disclosure instead of killed. Record
`"aborted at terminal"` for the `reason` (disclosed, never hidden). The
harvested-kill disclosure does NOT change `status`/`stage`; the state table
below still derives them from disk.

## 1. Terminal-state table (first match wins)

| # | disk condition | status | stage |
|---|---|---|---|
| 1 | `project_manifest.md` OR `shadow/` OR `BASELINE.lock` missing | failed | flatten |
| 2 | `contracts.json` missing, or its recorded viability flag is false | failed | contract |
| 3 | the baseline early chain is incomplete (`base/bottleneck_report.json` or `baseline/train.rendered.sh` missing), OR `baseline_status.md` records the chain as failed, OR `baseline/train_final.json` exists with `status: failed` (three-state read of the baseline training terminal: failed → attribute via its `stage`; missing → the loop ended before the baseline finished — not itself a failure, disclosed under `baseline.ref_acc`) | failed | baseline |
| 4 | `rounds/` has no numeric directory | failed | propose |
| 5 | `final/` exists AND `final/final_acc.json` missing | failed | full-train |
| 6 | `final/final_acc.json` exists AND its `within_budget` is true | success | full-train |
| 6b | `final/final_acc.json` exists AND its `within_budget` is false | failed | full-train |
| 7 | current round incomplete (`.round_advanced` missing or its `round` != max numeric directory under `rounds/`): see inner attribution below | failed | <inner> |
| 8 | round complete AND `best.json` exists AND no `final/`: `exhausted` flag of the last round's proposals.json is `true` → stage `full-train`; `false` → stage `gate` | failed | full-train / gate |
| 9 | round complete AND no `best.json` | failed | gate |

Row 6b is the honest out-of-budget terminal: the full training ran and the
final metric missed the accuracy budget — report `failed` with the gap in
`final` and the cause in `reason` (no write-back happens on this row).
Row 8's tiebreaker: an exhausted last round can never route back into the
loop, so an advanced best with no full-train artifacts died on the
full-training path; a non-exhausted round could have looped, and the gate is
the decision point that left no disk trace — state in `reason` that the two
are indistinguishable from disk alone. Row 9 covers the honest terminal
failure: rounds ran, nothing was ever advanced (or nothing was even
proposable), the loop ended with no winner.

**Inner attribution for row 7** (within the incomplete round `R`,
`rounds/<RRR>/`; the proposal loop — propose, implement, latency recheck —
closed inside ONE node):

- `rounds/<RRR>/proposals.json` missing → stage `propose`;
- else some proposal vid has NONE of the three closure states — no
  `variants/<vid>/DONE` marker, no `variants/<vid>/verdict.json`, no
  terminal outcome in its latest history row → stage `propose` (the loop
  died between proposals and implementations);
- else some DONE vid lacks `variants/<vid>/verdict.json` (and its latest
  history row is not terminal) → stage `propose` (died before/at the
  latency recheck);
- else some survivor (latest history `outcome == "latency_pass"`) lacks a
  terminal accuracy outcome → stage `probe`;
- otherwise (nothing outstanding, yet the round never advanced) → stage
  `probe` (the round-end advance never ran).

`reason` (one or two sentences): for success, the winner and the final
budget verdict; for failures, what the matched row's condition shows (e.g.
"baseline training failed at the final check: actual epochs < rendered",
"round 2 stopped inside the proposal loop: 2 DONE variants without
verdicts").

## 2. Field assembly

- `status` / `stage` / `reason`: from the table above (plus the harvest
  disclosure when it fired).
- `winner`: when `best.json` exists — `{"vid", "change_sig", "lineage"}`;
  `change_sig` from the vid's latest history row; `lineage` = the parent
  chain: walk `parent_vid` links backwards through history (oldest first,
  ending with the winner vid; a null parent ends the walk). No `best.json`
  → `null`.
- `baseline`: `proxy_acc` = the baseline FULL curve's metric at epoch k
  (k = `contracts.json` `proxy_budget.epochs`; read
  `baseline/baseline_metrics.jsonl`'s epoch-k row; curve shallower than k,
  file missing, or no epoch-k row → null AND a disclosure in `reason`).
  `ref_acc` = the baseline full-training anchor, three-state read of
  `baseline/baseline_full_acc.json`: `baseline/train_final.json` missing →
  null + disclosure ("baseline training never reached a terminal state" —
  includes the aborted-at-terminal case); `train_final.status == "failed"`
  → null + attribution (quote its `stage`); `done` → read
  `baseline_full_acc` from the file (never from anywhere else). `makespan` =
  `base/origin_anchor.json`'s `baseline_makespan_cycles` — the frozen
  ORIGINAL baseline (the anchor never moves with advances); when the
  anchor file is absent (pre-baseline terminal) fall back to the current
  `base/profile/profile_summary.json` with an explicit disclosure that the
  value is un-anchored.
- `pretrained_ref_acc`: number from `baseline/pretrained_ref.json` when that
  file exists and parses with a numeric `value`; null otherwise. Reference
  only — never a gate.
- `final`: `acc` from `final/final_acc.json` (0 when absent); `makespan`
  from `best.json` (referenced, never re-measured; 0 when absent);
  `gap` = anchor − final.acc for `higher_better`, final.acc − anchor for
  `lower_better`, where anchor = the `baseline_full_acc` recorded
  INSIDE `final/final_acc.json` (the full-train stage pinned the anchor it
  judged against; 0 gap when either side is absent);
  `within_budget` = the `within_budget` recorded in `final/final_acc.json`
  (false when absent). Take the direction from `contracts.json`.
- `rounds_completed`: `.round_advanced.round` when the marker exists, else 0.
- `proposals_total`: sum of `len(proposals)` over every
  `rounds/<NNN>/proposals.json`.
- `history_path`: absolute path of `history.jsonl`.
- `write_back`: `{done, files, conflicts}` — `{false, [], []}` unless
  section 3 ran. On terminal states with NO advanced variant (no
  `best.json`), the zero-write-back form is the honest outcome — the
  Write-Back section of the report states "no advanced variant — nothing to
  write back" instead of implying a skip.
- `charts_summary`: comma-joined chart file names, or the exact fixed string
  `none (no rounds recorded)` — no free-form wording (section 4 pins when).
- `artifacts`: absolute paths of the key products that exist: this report's
  markdown, `best.json`, `history.jsonl`, `experiment_ledger.json`,
  `dashboard.html`, final checkpoint / onnx when present, the charts directory.
- `error`: "" on success; the matched failure cause otherwise.

## 3. Write-back (when status == success — write-back is a fixed behavior)

The write-back source is the FINAL global shadow (`shadow/` — after the last
round-end advance it is the winner's full tree) diffed against the user's
original files at the same relative paths. User files are never modified;
new files are written beside the originals.

1. **Lock re-verification**: recompute, exactly as `BASELINE.lock` records
   them, the checksums of the user project files the lock covers.
   - A structural-anchor mismatch (model path / pretrained checkpoint / their
     hashes) → `done=false`, one conflict entry describing the anchor
     mismatch, NO files written.
   - A per-file checksum mismatch (the user changed that file during the
     run) → conflict entry `<relpath>: original file changed during the run`,
     that file is not written.
2. **Diff and write**: for every file in the final shadow tree, compare with
   the user's file at the same relative path:
   - content identical → nothing to write;
   - original absent AND the relpath is listed in
     `readiness/readiness.json`'s `shadow_synthesized` array (a file the
     pipeline synthesized with no user original — e.g. a package
     `__init__.py` from the bare-module copy form) → SKIP writing: it is
     pipeline plumbing, not an optimization product. List it in the report's
     Write-Back section informationally as
     `<relpath>: shadow-synthesized, not an optimization product — skipped`
     (NOT a conflict, NOT in `files`).
   - content differs or the original is absent → write the shadow content to
     `<original dir>/<stem>_prof_optimized<suffix>` (keep the directory
     structure; for a file with no original, place
     `<name-stem>_prof_optimized<ext>` at the same relative directory under
     <project-root>).
   - Target exists with different content → conflict entry
     `<target>: exists with different content (not overwritten)`; target
     exists with identical content → count as written (idempotent).
3. **Deletions**: files the lock covers whose path is ABSENT from the final
   shadow tree → conflict entry `<relpath>: deleted in optimized structure
   (not written back)`. (The structural levers only edit files in place, so
   this normally never triggers — report it honestly if it ever does.)
4. **Verify**: after writing, re-read every written file and compare bytes
   with its shadow source; a mismatch → delete that partial file (it is
   ours), drop it from `files`, add a conflict entry
   `<target>: write verification failed`.
5. `done=true` iff at least one file was written OR there was nothing to write
   (an empty diff after content-identical files AND shadow-synthesized skips
   is still a completed write-back with `files=[]`); `done=false` when the
   anchor check failed.

## 4. Charts (history aggregation; best-effort, never blocks the report)

Before rendering charts, invoke the two deterministic shared scripts:
`experiment_ledger.py --artifacts <workspace>` and
`dashboard_snapshot.py --artifacts <workspace>`. Their failure is NOT
best-effort: an unreadable ledger or dashboard is a report-builder failure.
Also finalize the live training-curve chart (best-effort, never blocks the
report): `push_curves.py --artifacts <workspace> --title "(final)"` — the
terminal push so the final curve state is visible even if the daemon saw no
mid-run poll; a successful push appends the `.chart_push.log` audit line.

Write into `charts/`, one self-contained HTML file per chart (inline SVG,
stdlib-only rendering — no external dependencies):

- `rounds_makespan_trend.html` — line chart; x = round 1..R, y = that
  round's reference makespan: the round's best advanced makespan (history
  rows of the round with `outcome == "advanced"`, minimum) or, when nothing
  advanced, the round's base makespan (any row of the round's
  `base_at_proposal.makespan_cycles`).
- `verdict_distribution.html` — bar chart; latest-version outcome → count
  over all vids in history.
- **Pinned, no judgement calls**: no numeric directory under `rounds/`
  (no rounds recorded) → skip BOTH charts and set `charts_summary` to the
  exact fixed string `none (no rounds recorded)`. At least one numeric
  `rounds/<NNN>/` directory → ALWAYS produce BOTH charts (never one, never
  zero) — the deterministic fallbacks above decide the data points.
- **Live push (best-effort)**: when the orchestrator chart env is present
  (`ORCA_RUN_ID`, `ORCA_NODE`, `ORCA_SESSION_ID`, `ORCA_CHART_SOCK`), push
  the same two charts through the orchestrator's chart render API —
  `from orca.chart import render_chart`, lazy-imported inside a try/except
  (it only resolves inside an Orca run):

  ```python
  render_chart(chart_type="line",   # "bar" for the verdict distribution
               data=[{"round": 1, "makespan": 15288}, ...],  # flat records
               label="po_rounds_makespan_trend", title="Rounds makespan trend",
               x="round", y="makespan")
  ```

  All arguments are keyword-only; reusing the same `label` + `title`
  replaces the previously pushed chart. Any import/push failure → one
  stderr line and continue. Never let chart failure change the report JSON
  beyond `charts_summary`.
- `charts_summary` = comma-joined file names including `dashboard.html`
  (+ `; live pushed` when the live push succeeded, `; live push unavailable`
  otherwise).

## 4b. Accuracy-rule merge (ALWAYS, before the markdown)

Hand the workspace's `accuracy_rules.json` to the two permanent homes —
regardless of the terminal status (a failed run's measured lessons are the
most valuable ones):

```bash
python3 "$ORCA_ARTIFACTS_DIR/scripts/rules_pool.py" merge \
  --artifacts "$ORCA_ARTIFACTS_DIR" --project-root "<project-root>"
```

It overwrites the project mirror `<project-root>/docs/prof-opt/
accuracy_rules.json` with the workspace rules and merges them into the
cross-run pool. Best-effort: on any disclosed failure, say so in `reason`
and continue. Then write the human-readable table mirror
`<project-root>/docs/prof-opt/accuracy_rules.md` (one row per rule:
pattern / direction / generality / confidence / evidence rounds / gap /
statement).

## 5. prof_opt_report.md (human-readable, workspace root)

Sections: Profiling Disclosure (profile_mode.json verbatim + scripts
.VERSION stamp) · Terminal State (status/stage/reason) · Per-Round Table
(round,
proposals, verdict outcome counts, accuracy-pass vids, round best makespan) ·
**Stop-Status Disclosure** (mechanical counts over every
`variants/<vid>/train/stop_status.json`: `killed` vs `natural_done`, and how many
record `monitor_failed: true` — the probe monitor's exercise disclosure) ·
Winner (vid, change signature, lineage chain) · **Fairness Note** (one
short paragraph: the baseline and every variant were trained FROM SCRATCH —
the baseline at the full budget, every variant rendered at the SAME full
epoch count and stopped externally at epoch k; both budgets from the single
source `contracts.json` (`full_train_budget` + `proxy_budget`). **The
epoch count cited here is `full_train_budget.epochs` read from
`contracts.json` — the EFFECTIVE value the anchor's fingerprint carries,
never the raw argparse count**; when the anchor file's recorded
`full_train_budget` differs from the current contracts (a stale anchor
should already have failed the fingerprint check upstream — if you are
looking at one, say so), state BOTH values.) · Baseline vs Final
(baseline makespan / curve-at-k proxy accuracy / full-training anchor,
final makespan / accuracy / gap / budget verdict — the baseline side of
the table reads `base/origin_anchor.json` (original baseline makespan /
frozen target line / accuracy budget); add a "pretrained reference" line —
path only, explicitly non-gating — when `readiness.json` records a provided
pretrained ckpt; add a "zero-improvement rounds" count line: rounds that
ran an advance (a `rounds/<NNN>/direction.json` exists) whose round has NO
`advanced` history row — derive it from HISTORY rows (reading
direction.json files alone would double-count, they are overwritten within
a round) · **Round Conclusions** (one line per round distilled from that
round's `rounds/<NNN>/analysis.md` — the latency/accuracy lessons exactly as
recorded at round end; then a two-to-three-line cross-round summary: which
lever families delivered vs were falsified, and the predicted-vs-actual
calibration drift; rounds whose analysis file is absent are skipped, never
fabricated) · Accuracy Rules (the run's rule file summary: rule count, the
highest-confidence harmful/benign patterns, the merge outcome and the
mirror paths) · Write-Back (written files, conflicts,
deletions, informational skips of shadow-synthesized files; on a
no-winner terminal: the explicit "no advanced variant — nothing to
write back" line) ·
**Enablement Note**: written files carry NEW names — switching
the model import to the new file name is the user's one-time action; the
original files are untouched.

## 6. Emission

The builder prints the single-line JSON (all fields of the node output
schema, exact names). Validate before replying: the line parses as JSON and
carries every schema field; fix the builder and re-run (fix-loop ≤ 3), then
fail loud with a minimal valid JSON (`status=failed`, `stage=report`, filled
`error`, empty collections, `baseline`/`final` zeroed) — never an unparseable
reply.
