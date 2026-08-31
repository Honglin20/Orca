#!/usr/bin/env bash
# check_contracts.sh — po_contract gate. Two modes:
#
#   --reuse-check : exit 0 (REUSE) iff contracts.json exists, parses, is
#                   viable, carries the CURRENT workflow version's fields
#                   (full_train_budget / train.ckpt_per_epoch /
#                   probe_cap_mechanism=stop-at-k — an older workspace fails
#                   loud with a fresh_start hint) AND every recorded entry
#                   sha256 still matches the file on disk. Any drift -> exit 1
#                   (contracts must be rebuilt; sha drift is never silently
#                   accepted). Template presence/token checks run in GATE mode
#                   only — a missing template on the reuse path fails loud
#                   downstream at the baseline chain's artifact check, never
#                   silently. Hard errors -> 2.
#   (default)     : full validation gate for the finished contract stage —
#                   schema completeness (incl. v4: ckpt addressability, the
#                   full_train_budget value-level fingerprint, the admission
#                   clause in `reason`, stop-at-k cap mechanism, the single
#                   identical probe/full training pipeline), entry sha
#                   anti-drift, template token contract, tier-B adapted-entry
#                   presence, numeric fields, dry-run/dual-ckpt/export
#                   evidence, interpreter flag check.
#                   Fail -> exit 1 (fix and re-run); hard errors -> 2.
#
# Environment: ORCA_ARTIFACTS_DIR (required).
# All findings go to stderr; nothing is written.
set -uo pipefail

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (check_contracts.sh)}"
MODE="${1:-gate}"
cd "$ART" || { echo "FATAL: artifacts dir unreachable: $ART" >&2; exit 2; }

python3 - "$ART" "$MODE" <<'PY'
import hashlib, json, os, subprocess, sys, tempfile
from pathlib import Path

art = Path(sys.argv[1])
mode = sys.argv[2]  # "gate" | "--reuse-check"
problems = []

# E3-07: the admission clause's canonical home is the po_contract agent
# document; this constant substring is what the recorded contracts.json
# `reason` must carry verbatim (a test pins sh <-> agent.md textual sync).
ADMISSION_CLAUSE = "训练须按给定轮数精确执行"

def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

cpath = art / "contracts.json"
if not cpath.is_file():
    print("check_contracts: contracts.json missing", file=sys.stderr)
    sys.exit(1)
try:
    c = json.loads(cpath.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"check_contracts: contracts.json not valid JSON: {exc}", file=sys.stderr)
    sys.exit(1)

def need(obj, dotted):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            problems.append(f"contracts.json missing key '{dotted}'")
            return None
        cur = cur[part]
    return cur

# ── schema completeness (gate mode only; reuse just needs viable + shas) ─────
if mode != "--reuse-check":
    for key in ("viable", "reason", "proxy_budget", "probe_cap_mechanism",
                "exemptions"):
        need(c, key)
    need(c, "interpreter.sys_executable")
    need(c, "shadow.shadow_pkgs")
    mf = need(c, "model_facts") or {}
    for key in ("module", "factory", "dummy_inputs"):
        if isinstance(mf, dict) and key not in mf:
            problems.append(f"model_facts missing '{key}'")
    for section in ("train", "eval", "export"):
        if section not in c or not isinstance(c[section], dict):
            problems.append(f"contracts.json missing section '{section}'")
    pattern = None
    if isinstance(c.get("train"), dict):
        for key in ("tier", "entry", "entry_sha256", "flags", "ckpt_output_rule",
                    "ckpt_per_epoch", "epoch_metric_extraction",
                    "train_epochs_full"):
            if key not in c["train"]:
                problems.append(f"train contract missing '{key}'")
        if c["train"].get("tier") not in ("A", "B"):
            problems.append(f"train.tier must be A or B, got {c['train'].get('tier')!r}")
        if not isinstance(c["train"].get("train_epochs_full"), int) \
                or c["train"].get("train_epochs_full", 0) < 1:
            problems.append("train.train_epochs_full must be an int >= 1")
        # ckpt addressability: the rule stays a glob/pattern string; the
        # boolean is what finalizer/probe branch on (k-th ckpt eval vs
        # curve-only judgment)
        if not isinstance(c["train"].get("ckpt_per_epoch"), bool):
            problems.append("train.ckpt_per_epoch must be a boolean "
                            "(true: per-epoch addressable ckpts / false: rolling "
                            "or undecidable)")
        epoch_rule = c["train"].get("epoch_metric_extraction")
        pattern = epoch_rule.get("pattern") if isinstance(epoch_rule, dict) else None
        if not isinstance(pattern, str) or not pattern:
            problems.append("train.epoch_metric_extraction.pattern must be a non-empty string")
        else:
            import re
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                problems.append(f"train.epoch_metric_extraction.pattern is invalid: {exc}")
            else:
                if "epoch" not in regex.groupindex or "metric" not in regex.groupindex:
                    problems.append("train.epoch_metric_extraction.pattern needs named groups epoch and metric")
                else:
                    # boundary anchor (best-effort functional check on a
                    # canonical sample): the char right after a matched metric
                    # must not be a digit — a pattern that can stop mid-number
                    # (0.1234 truncated to 0.12) silently corrupts every curve
                    sample = "epoch 1 metric=0.1234567890\n"
                    m = regex.search(sample)
                    if m:
                        end = m.end("metric")
                        if end < len(sample) and sample[end].isdigit():
                            problems.append(
                                "train.epoch_metric_extraction.pattern can match "
                                "a metric followed by another digit (mid-number "
                                "truncation) — anchor the metric group with an "
                                "end-of-line/non-digit boundary")
    # admission clause (E3-07): the single-source sentence lives in the
    # po_contract agent document; this constant substring must appear in the
    # recorded top-level reason
    if not isinstance(c.get("reason"), str) \
            or ADMISSION_CLAUSE not in c.get("reason", ""):
        problems.append(f"contracts.json top-level reason must contain the "
                        f"admission clause {ADMISSION_CLAUSE!r}")
    # full training budget: the value-level fairness fingerprint (baseline
    # and every full-budget render must carry identical values)
    fbt = c.get("full_train_budget")
    if not isinstance(fbt, dict):
        problems.append("contracts.json missing 'full_train_budget' "
                        "(epochs/seed/data fingerprint)")
    else:
        for key in ("epochs", "seed", "data"):
            if key not in fbt:
                problems.append(f"full_train_budget missing '{key}'")
        if not isinstance(fbt.get("epochs"), int) or fbt.get("epochs", 0) < 1:
            problems.append("full_train_budget.epochs must be an int >= 1")
        if not isinstance(fbt.get("seed"), int):
            problems.append("full_train_budget.seed must be an int")
        fdata = fbt.get("data")
        if not isinstance(fdata, dict) \
                or fdata.get("dataset_knob") is not None \
                or fdata.get("data_value") is not None:
            problems.append("full_train_budget.data must be the null pair "
                            "{dataset_knob: null, data_value: null} (full-data "
                            "training, value-level fingerprint)")
    if isinstance(c.get("eval"), dict):
        for key in ("tier", "entry", "entry_sha256", "flags", "ckpt_container",
                    "metric_extraction", "metric_direction"):
            if key not in c["eval"]:
                problems.append(f"eval contract missing '{key}'")
        if c["eval"].get("metric_direction") not in ("higher_better", "lower_better"):
            problems.append("eval.metric_direction must be higher_better|lower_better")
        if c["eval"].get("tier") not in ("A", "B"):
            problems.append(f"eval.tier must be A or B, got {c['eval'].get('tier')!r}")
        if not isinstance(c["eval"].get("ckpt_container"), str) \
                or not c["eval"].get("ckpt_container"):
            problems.append("eval.ckpt_container must be a non-empty string "
                            "(bare | wrapper:<key>)")
    if isinstance(c.get("export"), dict):
        for key in ("entry", "entry_sha256", "generated", "argv_facts"):
            if key not in c["export"]:
                problems.append(f"export contract missing '{key}'")
    # proxy_budget: the single source of the fairness invariant — field set and
    # types are pinned; a missing knob/value is null, never absent.
    pb = c.get("proxy_budget")
    if not isinstance(pb, dict):
        problems.append("proxy_budget must be an object")
    else:
        for key in ("epochs", "dataset_knob", "data_value", "max_steps", "seed"):
            if key not in pb:
                problems.append(f"proxy_budget missing '{key}'")
        if not isinstance(pb.get("epochs"), int) or pb.get("epochs", 0) < 1:
            problems.append("proxy_budget.epochs must be an int >= 1")
        if pb.get("dataset_knob") is not None and not isinstance(pb.get("dataset_knob"), str):
            problems.append("proxy_budget.dataset_knob must be a string or null")
        if pb.get("data_value") is None and pb.get("dataset_knob") is not None:
            problems.append("proxy_budget.dataset_knob set but data_value is null "
                            "(a discovered knob must carry a selected value)")
        if pb.get("dataset_knob") is None and pb.get("data_value") is not None:
            problems.append("proxy_budget.data_value set but dataset_knob is "
                            "null (a value with no discovered knob to feed is "
                            "a meaningless combination)")
        if pb.get("max_steps") is not None and not isinstance(pb.get("max_steps"), int):
            problems.append("proxy_budget.max_steps must be an int or null")
        if not isinstance(pb.get("seed"), int):
            problems.append("proxy_budget.seed must be an int")
        if pb.get("dataset_knob") is not None or pb.get("data_value") is not None \
                or pb.get("max_steps") is not None:
            problems.append("proxy_budget must be epoch-only: dataset_knob/data_value/max_steps are null")
        if c.get("probe_cap_mechanism") != "stop-at-k":
            problems.append("probe_cap_mechanism must be stop-at-k "
                            "(full-epoch render + external stop at epoch k)")
        # the knob declared in the budget must be the one recorded in train.flags
        train_knob = (c.get("train") or {}).get("flags", {}).get("data_knob")
        if pb.get("dataset_knob") != (train_knob or None):
            problems.append(f"proxy_budget.dataset_knob {pb.get('dataset_knob')!r} "
                            f"disagrees with train.flags.data_knob {train_knob!r}")
    if not isinstance(c.get("exemptions"), list):
        problems.append("exemptions must be a list")
    if not isinstance(c.get("viable"), bool):
        problems.append("viable must be a boolean")

    # interpreter flag check evidence (-S/-E would kill the injection)
    if (c.get("interpreter") or {}).get("flags_check") != "pass":
        problems.append("interpreter.flags_check != 'pass' (-S/-E detection must pass)")

    # tier B -> adapted entries must exist on disk
    for section in ("train", "eval"):
        if isinstance(c.get(section), dict) and c[section].get("tier") == "B":
            entry = Path(c[section].get("entry", ""))
            if not entry.is_file():
                problems.append(f"{section} tier B but adapted entry missing: {entry}")

    # quick-run / dual-ckpt / export evidence (measured, not asserted)
    for ev, must in (
        ("contract_work/train_quickrun.json", lambda d:
            d.get("status") == "runs_minimal_budget"),
        ("contract_work/eval_dual_ckpt.json", lambda d: d.get("moved") is True),
        ("contract_work/export_check.json", lambda d: d.get("loaded") is True),
        ("contract_work/proxy_budget_selection.json", lambda d: bool(d.get("rationale"))),
    ):
        p = art / ev
        if not p.is_file():
            problems.append(f"measured evidence missing: {ev}")
        else:
            try:
                if not must(json.loads(p.read_text(encoding="utf-8"))):
                    problems.append(f"measured evidence not satisfied: {ev}")
            except Exception as exc:
                problems.append(f"measured evidence unreadable: {ev}: {exc}")

    # end-to-end epoch extraction: the pattern must parse the REAL 2-epoch
    # quickrun log into exactly 2 epochs CONTIGUOUS FROM 1. The syntax /
    # named-group / boundary checks above cannot see a 0-based epoch base - a
    # pattern that matches "epoch 0, 1, ..." passes them but breaks every
    # downstream consumer (metric_curve extract in the baseline finalizer,
    # stop_at_epoch in the probe) only AFTER the full training has already
    # run. Deterministic re-run here, never the analyst's own claim.
    if pattern is not None:
        qr_path = art / "contract_work" / "train_quickrun.json"
        try:
            qr = json.loads(qr_path.read_text(encoding="utf-8"))
            qr_log = qr.get("train_log") if isinstance(qr, dict) else None
        except Exception as exc:
            problems.append(f"train_quickrun evidence unreadable for the "
                            f"epoch-extraction check: {exc}")
            qr_log = None
        if not isinstance(qr_log, str) or not qr_log:
            problems.append("contract_work/train_quickrun.json must record "
                            "'train_log' (absolute path to the captured "
                            "2-epoch quickrun stdout) - the gate re-runs "
                            "metric_curve extract on it")
        else:
            mc = art / "scripts" / "metric_curve.py"
            if not mc.is_file():
                problems.append("scripts/metric_curve.py missing - the gate "
                                "cannot verify epoch extraction end-to-end")
            else:
                log = Path(qr_log) if os.path.isabs(qr_log) else art / qr_log
                fd, tmp_out = tempfile.mkstemp(
                    prefix=".extract_check_", suffix=".jsonl",
                    dir=str(art / "contract_work"))
                os.close(fd)
                try:
                    try:
                        proc = subprocess.run(
                            [sys.executable, str(mc), "extract",
                             "--log", str(log), "--pattern", pattern,
                             "--out", tmp_out, "--expected-epochs", "2"],
                            capture_output=True, text=True, timeout=30)
                    except subprocess.TimeoutExpired:
                        problems.append(
                            "train.epoch_metric_extraction.pattern end-to-end "
                            "extraction timed out on the quickrun log")
                    else:
                        if proc.returncode != 0:
                            problems.append(
                                "train.epoch_metric_extraction.pattern failed "
                                "end-to-end extraction on the quickrun log "
                                "(2 epochs, contiguous from 1): "
                                f"{(proc.stderr or proc.stdout).strip()}")
                finally:
                    try:
                        os.unlink(tmp_out)
                    except OSError:
                        pass

    # template token contract (downstream renders via render_run.sh --set ...;
    # tokens are <<k>>, never {{k}} — agent.md prompts are Jinja2-rendered).
    # Required tokens mirror the Step 5 spec exactly: a template missing
    # <<seed>> would run unseeded training, missing <<python>> would dodge the
    # pinned interpreter — both must fail HERE, not silently downstream.
    # <<ckpt>> is FORBIDDEN in the two training templates (train-from-scratch:
    # no checkpoint is ever loaded), and <<data_value>> appears in the probe
    # template IFF proxy_budget.dataset_knob was discovered (a token without a
    # --set would fail every downstream render; a knob without the token would
    # silently train on the full dataset and break the fairness invariant).
    for tname, tokens in (
        ("run_probe_finetune.template.sh",
         ("<<python>>", "<<epochs>>", "<<out_dir>>", "<<seed>>")),
        ("run_full_finetune.template.sh",
         ("<<python>>", "<<epochs>>", "<<out_dir>>", "<<seed>>")),
        ("run_eval.template.sh", ("<<python>>", "<<ckpt>>", "<<log>>")),
        ("export_onnx.template.sh", ("<<python>>", "<<out>>", "<<seed>>")),
    ):
        tpath = art / "templates" / tname
        if not tpath.is_file():
            problems.append(f"run template missing: templates/{tname}")
        else:
            body = tpath.read_text(encoding="utf-8")
            for tok in tokens:
                if tok not in body:
                    problems.append(f"templates/{tname} lacks required token {tok}")
            if tname != "run_eval.template.sh" and "<<ckpt>>" in body:
                problems.append(f"templates/{tname} carries a <<ckpt>> token — "
                                f"training always starts from scratch")
    knob = (c.get("proxy_budget") or {}).get("dataset_knob") \
        if isinstance(c.get("proxy_budget"), dict) else None
    for tname, want_data_token in (
        ("run_probe_finetune.template.sh", bool(knob)),
        ("run_full_finetune.template.sh", False),
    ):
        tpath = art / "templates" / tname
        if not tpath.is_file():
            continue
        has_token = "<<data_value>>" in tpath.read_text(encoding="utf-8")
        if want_data_token and not has_token:
            problems.append(f"templates/{tname} lacks <<data_value>> although "
                            f"proxy_budget.dataset_knob={knob!r} was discovered")
        if not want_data_token and has_token:
            problems.append(f"templates/{tname} carries <<data_value>> although "
                            f"no data knob is recorded (full-data training)")
    # step-cap token, symmetric: a pinned max_steps with no <<max_steps>> token
    # would silently render an untruncated training (render_run drops unused
    # --set values), and a token with no pinned value fails every render.
    pb_max_steps = (c.get("proxy_budget") or {}).get("max_steps") \
        if isinstance(c.get("proxy_budget"), dict) else None
    probe_tpl = art / "templates" / "run_probe_finetune.template.sh"
    if probe_tpl.is_file():
        has_ms_token = "<<max_steps>>" in probe_tpl.read_text(encoding="utf-8")
        if pb_max_steps is not None and not has_ms_token:
            problems.append("templates/run_probe_finetune.template.sh lacks "
                            "<<max_steps>> although proxy_budget.max_steps="
                            f"{pb_max_steps!r} is pinned (truncation would vanish)")
        if pb_max_steps is None and has_ms_token:
            problems.append("templates/run_probe_finetune.template.sh carries "
                            "<<max_steps>> although proxy_budget.max_steps is null")

    # v4 single training pipeline: baseline, every variant, and the winner
    # all render at FULL epochs (the probe depth is an external stop), so the
    # probe/full template pair — if kept as two files — must be byte-identical
    # (same data pipeline; a divergence would silently break the fairness
    # invariant depending on which template a node happened to render)
    probe_tpl = art / "templates" / "run_probe_finetune.template.sh"
    full_tpl = art / "templates" / "run_full_finetune.template.sh"
    if probe_tpl.is_file() and full_tpl.is_file():
        if probe_tpl.read_bytes() != full_tpl.read_bytes():
            problems.append("templates/run_probe_finetune.template.sh and "
                            "run_full_finetune.template.sh differ — v4 has ONE "
                            "training pipeline (full-epoch render + external "
                            "stop-at-k); regenerate both from the same source")

# ── v4 field presence on the REUSE path (a v3.x workspace lacks them) ────────
if mode == "--reuse-check":
    missing_v4 = []
    if not isinstance(c.get("full_train_budget"), dict):
        missing_v4.append("full_train_budget")
    if not isinstance((c.get("train") or {}).get("ckpt_per_epoch"), bool):
        missing_v4.append("train.ckpt_per_epoch")
    if c.get("probe_cap_mechanism") != "stop-at-k":
        missing_v4.append("probe_cap_mechanism=stop-at-k")
    if missing_v4:
        for key in missing_v4:
            print(f"check_contracts: FAIL recorded contracts.json predates the "
                  f"current workflow version (missing {key})", file=sys.stderr)
        print("check_contracts: the reusable workspace was built by an older "
              "contract stage — re-run with fresh_start=true to rebuild the "
              "contracts from scratch (never partially patched)", file=sys.stderr)
        sys.exit(1)

# ── entry sha anti-drift (both modes — the pinned drift guard) ───────────────
for section in ("train", "eval", "export"):
    sc = c.get(section)
    if isinstance(sc, dict) and sc.get("entry") and sc.get("entry_sha256"):
        entry = Path(sc["entry"])
        if not entry.is_file():
            problems.append(f"{section}.entry missing on disk: {entry}")
        elif sha(entry) != sc["entry_sha256"]:
            problems.append(f"{section}.entry sha256 drift (file changed after "
                            f"contracts were measured): {entry}")

if problems:
    for p in problems:
        print(f"check_contracts: FAIL {p}", file=sys.stderr)
    sys.exit(1)

print(f"check_contracts: PASS ({'reuse' if mode == '--reuse-check' else 'gate'})",
      file=sys.stderr)
sys.exit(0)
PY
rc=$?
if [ $rc -ne 0 ]; then
  echo "FAIL: check_contracts ($MODE)" >&2
fi
exit $rc
