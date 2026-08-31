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

The report's FIRST section is a disclosure block carrying three verbatim
records: `profile_mode.json` (the profiling configuration every number in
this report was measured under — mode / chip / precision / core_num /
resolved_by), `train_device.json` (the training device backend every
training ran on — backend / device_count / resolved_by), and the deployed
scripts' version stamp (`scripts/.VERSION` manifest hash). No judgement,
just provenance.

## 0. Terminal harvest (wait, never kill — before the state table)

The in-flight set = the baseline finalizer (when `baseline/finalizer.pid`
names a live, attribution-checked pid) plus every variant whose LATEST
`history.jsonl` row has `outcome == "latency_pass"` (no terminal row —
`success` / `accuracy_fail` / `probe_insufficient` / `latency_fail` — in
any version). For each member, ONE bounded poll (≤ 60 s):

- baseline: wait for `baseline/train_final.json`; pid dead → pass;
- variant: read `variants/<vid>/train_status.json` `stage` — a terminal
  stage (`killed` / `done` / `failed`) → pass; `waiting` / `training` /
  `final_eval_waiting` → wait within the poll.

Anything still in flight when the polls top out → **park**: reply with a
status message containing `do not call orca next` (name the awaited
vids/stages and their `watchdog.log` paths) and re-enter next turn. You
NEVER kill a live training to unblock yourself — it owns its card, the
cost is paid, and its terminal row is what keeps it judgment-eligible.

**The ONLY kill path is a platform-external stop** — the run is being torn
down from outside and this node cannot re-enter. Then: kill the baseline
training group (`baseline/train.pid`), the finalizer group, and every
in-flight variant group (`variants/<vid>/train/train.pid`). Every kill is
attribution-GUARDED: the pid's /proc cmdline must reference
`train.rendered.sh` (training wrappers) or `--finalizer` (finalizer)
before signalling — a dead, reused, or unrelated pid is skipped and named
in the disclosure instead of killed. Record `"aborted at terminal"` for
the `reason` (disclosed, never hidden). The harvested-kill disclosure does
NOT change `status`/`stage`; the state table below still derives them from
disk.

## 1. Terminal-state table (first match wins)

Terminal rows are judged over ANY version row (a later row cannot erase a
vid's terminal outcome; `read_latest` per vid is the entry point, and a
terminal row in ANY version makes the vid judged).

| # | disk condition | status | stage |
|---|---|---|---|
| 1 | `project_manifest.md` OR `shadow/` OR `BASELINE.lock` missing | failed | flatten |
| 2 | `contracts.json` missing, or its recorded viability flag is false | failed | contract |
| 3 | the baseline early chain is incomplete (`base/bottleneck_report.json` or `baseline/train.rendered.sh` missing), OR `baseline_status.md` records the chain as failed, OR `baseline/train_final.json` exists with `status: failed` | failed | baseline |
| 4 | `rounds/` has no numeric directory | failed | propose |
| 5 | any vid has a `success` row (after Step 0's harvest every in-flight training is terminal) | success | probe |
| 6 | no success AND some vid's latest row is `latency_pass` while its training left NO terminal record (no terminal row, `train_status.json` missing or non-terminal, watchdog dead/absent — a torn launch, not a wait) | failed | probe |
| 7 | no success AND `round_state current`'s round ≥ `max_rounds` (the hard cap ended the loop without a winner) | failed | gate |
| 8 | otherwise (no success, nothing in flight or torn, below the cap — the loop ended without any exit condition leaving a disk trace) | failed | gate |

Row 5's stage is `probe` because the winner's training and final eval were
launched and judged by the probe pipeline's detached watchdogs — the
`reason` must name the winner and its gap/makespan so the success is
self-describing. Row 6 is the honest torn-launch terminal: a variant was
released to train but neither its watchdog nor its history ever recorded
an outcome — attribute it, name the vid. Row 7 vs row 8 are close: the cap
is re-derived from the current round versus the input cap, and only the
gate's own run log knows the exact decision — say so in `reason` when the
distinction matters.

`reason` (one or two sentences): for success, the winner and its final
budget verdict; for failures, what the matched row's condition shows
(e.g. "baseline training failed at the final check: actual epochs <
rendered", "round 4's variant r4-01 was released to train but left no
terminal record — torn launch").

## 2. Field assembly

- `status` / `stage` / `reason`: from the table above (plus the harvest
  disclosure when it fired).
- **winner (§9)**: among vids with a `success` row, pick the smallest
  `gap` from the success row; ties broken by the smallest
  `makespan_cycles` in the vid's merged history snapshot (the latency
  row's measured value); further ties by vid (lexicographic, pinned so
  the pick is deterministic). The winner object is `{"vid",
  "change_sig", "lineage"}`: `change_sig` from the winner's latest
  history row; `lineage` = the parent chain walked backwards through
  history (`parent_vid` links, oldest first, ending with the winner vid) —
  in v6 the base never advances so `parent_vid` is null and the chain is
  the winner vid alone. **No success row anywhere → `null` and a
  no-promotion disclosure in `reason`** (the report references the
  dashboard for what was tried).
- `baseline`: `ref_acc` = the baseline full-training anchor, three-state
  read of `baseline/baseline_full_acc.json`: `baseline/train_final.json`
  missing → null + disclosure ("baseline training never reached a
  terminal state" — includes the aborted-at-terminal case);
  `train_final.status == "failed"` → null + attribution (quote its
  `stage`); `done` → read `baseline_full_acc` from the file (never from
  anywhere else). `makespan` = `base/origin_anchor.json`'s
  `baseline_makespan_cycles` — the frozen ORIGINAL baseline; when the
  anchor file is absent (pre-baseline terminal) fall back to the current
  `base/profile/profile_summary.json` with an explicit disclosure that the
  value is un-anchored.
- `pretrained_ref_acc`: number from `baseline/pretrained_ref.json` when that
  file exists and parses with a numeric `value`; null otherwise. Reference
  only — never a gate.
- `final` (all from the WINNER's records; zeroed when there is no winner):
  `acc` from `variants/<vid>/eval/final_acc.json`'s `final_acc` (0 when
  absent); `makespan` from the winner's history snapshot
  `makespan_cycles` (referenced, never re-measured; 0 when absent);
  `gap` from the winner's `success` history row (0 when absent);
  `within_budget` = the `within_budget` recorded in the eval record
  (false when absent). Take the direction from `contracts.json`.
- `rounds_completed`: `round_state current`'s round (the max numeric
  directory under `rounds/`; 0 when none).
- `proposals_total`: sum of `len(proposals)` over every
  `rounds/<NNN>/proposals.json`.
- `history_path`: absolute path of `history.jsonl`.
- `write_back`: `{done, files, conflicts}` — `{false, [], []}` unless
  section 3 ran. On terminals with NO winner, the zero-write-back form is
  the honest outcome — the Write-Back section of the report states "no
  success variant — nothing to write back" instead of implying a skip.
- `charts_summary`: comma-joined chart file names, or the exact fixed string
  `none (no rounds recorded)` — no free-form wording (section 4 pins when).
- `artifacts`: absolute paths of the key products that exist: this report's
  markdown, `history.jsonl`, `experiment_ledger.json`,
  `dashboard.html`, the winner's `eval/final_acc.json` and final
  checkpoint / onnx when present, the charts directory.
- `error`: "" on success; the matched failure cause otherwise.

## 3. Write-back (when status == success — write-back is a fixed behavior)

The write-back source is the **WINNER's variant shadow**
(`variants/<winner-vid>/shadow/` — the tree the implementer actually
optimized; in v6 the global `shadow/` stays the untouched baseline copy)
diffed against the user's original files at the same relative paths. User
files are never modified; new files are written beside the originals.

1. **Lock re-verification**: recompute, exactly as `BASELINE.lock` records
   them, the checksums of the user project files the lock covers.
   - A structural-anchor mismatch (model path / pretrained checkpoint / their
     hashes) → `done=false`, one conflict entry describing the anchor
     mismatch, NO files written.
   - A per-file checksum mismatch (the user changed that file during the
     run) → conflict entry `<relpath>: original file changed during the run`,
     that file is not written.
2. **Diff and write**: for every file in the winner's shadow tree, compare
   with the user's file at the same relative path:
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
3. **Deletions**: files the lock covers whose path is ABSENT from the
   winner's shadow tree → conflict entry `<relpath>: deleted in optimized
   structure (not written back)`. (The structural levers only edit files in
   place, so this normally never triggers — report it honestly if it ever
   does.)
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
Also finalize the live charts (best-effort, never blocks the report):
`push_curves.py --artifacts <workspace> --title "(final)" --docs` — the
terminal push so the final curve / pareto / analysis-docs manifest state is
visible even if the daemon saw no mid-run poll; a successful push appends
the `.chart_push.log` audit line.

Write into `charts/`, one self-contained HTML file per chart (inline SVG,
stdlib-only rendering — no external dependencies):

- `rounds_makespan_trend.html` — line chart; x = round 1..R, y = that
  round's reference makespan: the round's best MEASURED makespan (minimum
  `makespan_cycles` over the round's history rows that carry one) or, when
  the round measured nothing, the round's base makespan (any row of the
  round's `base_at_proposal.makespan_cycles`).
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
regardless of the terminal status (a failed run's measured lessons are
the most valuable ones):

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

Sections: Provenance Disclosure (profile_mode.json verbatim +
train_device.json verbatim + scripts .VERSION stamp) · Terminal State
(status/stage/reason, including the harvest disclosure when it fired) ·
Per-Round Table (round, proposals, verdict outcome counts, the round's
variant and its fate, round best makespan) · **Training Outcome
Disclosure** (mechanical counts over every `variants/<vid>/
train_status.json`: early-stopped (`killed`) vs natural completion
(`done`) vs failed launches (`failed`/absent), each with its
`stopped_at_epoch` and `over_budget_streak` where recorded — the
streaming judge's exercise disclosure) · Winner (vid, change signature,
lineage chain, gap, makespan, within_budget; on a no-winner terminal the
explicit "no success variant — no promotion" line referencing the
dashboard for what was tried) · **Fairness Note** (one short paragraph:
the baseline and every variant were trained FROM SCRATCH under the SAME
`full_train_budget` value-level fingerprint (`contracts.json` — epochs /
seed / data); a variant that fell behind the accuracy budget was
early-stopped by the streaming judge and can never be the winner; the
winner completed the full rendered epochs and its final eval was judged
against the baseline full-training anchor. **The epoch count cited here
is `full_train_budget.epochs` read from `contracts.json` — the EFFECTIVE
value the fingerprint carries, never the raw argparse count**) · Baseline
vs Final (baseline makespan / full-training anchor; winner makespan /
accuracy / gap / budget verdict — the baseline side reads
`base/origin_anchor.json`; add a "pretrained reference" line — path only,
explicitly non-gating — when `readiness.json` records a provided
pretrained ckpt) · **Round Conclusions** (one line per round distilled
from that round's `rounds/<NNN>/analysis.md` — the latency lessons
exactly as recorded at round end; then a two-to-three-line cross-round
summary: which lever families delivered vs were falsified, and the
predicted-vs-actual calibration drift; rounds whose analysis file is
absent are skipped, never fabricated) · Accuracy Rules (the run's rule
file summary: rule count, the highest-confidence harmful/benign patterns,
the merge outcome and the mirror paths) · Write-Back (written files,
conflicts, deletions, informational skips of shadow-synthesized files; on
a no-winner terminal: the explicit "no success variant — nothing to
write back" line) · **Dashboard And Docs** (point at `dashboard.html` /
`dashboard.json` and the `prof-opt/docs` analysis-docs manifest — the
portable summary of curves, pareto, gap table, and every variant's
analysis documents; reference the paths, never inline the content) ·
**Enablement Note**: written files carry NEW names — switching the model
import to the new file name is the user's one-time action; the original
files are untouched.

## 6. Emission

The builder prints the single-line JSON (all fields of the node output
schema, exact names). Validate before replying: the line parses as JSON and
carries every schema field; fix the builder and re-run (fix-loop ≤ 3), then
fail loud with a minimal valid JSON (`status=failed`, `stage=report`, filled
`error`, empty collections, `baseline`/`final` zeroed) — never an unparseable
reply.
