#!/usr/bin/env bash
# check_contracts.sh — po_contract gate (v7). Two modes:
#
#   --reuse-check : exit 0 (REUSE) iff contracts.json exists, parses, is
#                   viable=true, carries the CURRENT workflow version's
#                   fields (full_train_budget / train.ckpt_per_epoch /
#                   probe_cap_mechanism=stop-at-k / profile block /
#                   admission_clause_ack — an older workspace fails loud
#                   with a fresh_start hint, exit 3) AND every recorded
#                   entry sha256 still matches the file on disk (any drift
#                   -> exit 1: contracts must be rebuilt; sha drift is
#                   never silently accepted; a viable=false workspace is
#                   also exit 3 — it must not be reused as if it passed).
#                   Optional --profile-chip/--profile-precision/
#                   --profile-core-num compare the recorded profiling
#                   configuration with the CURRENT workflow inputs (a
#                   measurement-config drift needs fresh_start, exit 3 —
#                   cycles measured under a different configuration cannot
#                   be compared).
#                   Hard errors -> 2.
#   (default)     : full validation gate for the finished contract stage —
#                   schema completeness (v7: ckpt addressability, the
#                   full_train_budget fingerprint, admission_clause_ack,
#                   the epoch-only proxy budget, the single training
#                   pipeline, the profile block, the early_stop block,
#                   sitecustomize_merge), entry sha anti-drift, template
#                   token contract, tier-B adapted-entry presence, numeric
#                   fields, dry-run/dual-ckpt/export evidence, interpreter
#                   flag check.
#                   Fail -> exit 1 (fix and re-run); hard errors -> 2.
#
# Environment: ORCA_ARTIFACTS_DIR (required).
# All findings go to stderr; nothing is written.
set -uo pipefail

ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (check_contracts.sh)}"
MODE="gate"
PROFILE_CHIP=""; PROFILE_PRECISION=""; PROFILE_CORE_NUM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --reuse-check) MODE="--reuse-check"; shift ;;
    --profile-chip) PROFILE_CHIP="${2:?}"; shift 2 ;;
    --profile-precision) PROFILE_PRECISION="${2:?}"; shift 2 ;;
    --profile-core-num) PROFILE_CORE_NUM="${2:?}"; shift 2 ;;
    *) echo "FATAL: unknown argument $1" >&2; exit 2 ;;
  esac
done
cd "$ART" || { echo "FATAL: artifacts dir unreachable: $ART" >&2; exit 2; }

python3 - "$ART" "$MODE" "$PROFILE_CHIP" "$PROFILE_PRECISION" "$PROFILE_CORE_NUM" <<'PY'
import hashlib, json, os, subprocess, sys, tempfile
from pathlib import Path

art = Path(sys.argv[1])
mode = sys.argv[2]           # "gate" | "--reuse-check"
prof_chip, prof_precision, prof_core = sys.argv[3], sys.argv[4], sys.argv[5]

VERSION_KEYS = {
    "full_train_budget": lambda c: isinstance(c.get("full_train_budget"), dict),
    "train.ckpt_per_epoch": lambda c: isinstance((c.get("train") or {}).get("ckpt_per_epoch"), bool),
    "probe_cap_mechanism": lambda c: c.get("probe_cap_mechanism") == "stop-at-k",
    "profile": lambda c: isinstance(c.get("profile"), dict),
    "admission_clause_ack": lambda c: c.get("admission_clause_ack") is True,
    "early_stop": lambda c: isinstance(c.get("early_stop"), dict),
}

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

problems = []
version_problems = []

def need(obj, dotted):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            problems.append(f"contracts.json missing key '{dotted}'")
            return None
        cur = cur[part]
    return cur

# ── v7 field presence (both modes — the version discriminator) ───────────────
for key, check in VERSION_KEYS.items():
    if not check(c):
        version_problems.append(key)

# ── profile block (the mfu dispatch configuration, recorded from inputs) ─────
if isinstance(c.get("profile"), dict):
    prof = c["profile"]
    if prof.get("chip") not in ("6613", "1951"):
        problems.append(f"profile.chip must be '6613'|'1951', got {prof.get('chip')!r}")
    if prof.get("precision") not in ("INT8", "INT16", "AMP"):
        problems.append(f"profile.precision must be INT8|INT16|AMP, got {prof.get('precision')!r}")
    if prof.get("core_num") not in (1, 2, 4) or isinstance(prof.get("core_num"), bool):
        problems.append(f"profile.core_num must be 1|2|4, got {prof.get('core_num')!r}")
    if mode == "--reuse-check" and any((prof_chip, prof_precision, prof_core)):
        drift = {"chip": (prof.get("chip"), prof_chip),
                 "precision": (prof.get("precision"), prof_precision),
                 "core_num": (prof.get("core_num"), prof_core)}
        diff = {k: v for k, v in drift.items()
                if str(v[0]) != str(v[1]) and v[1] != ""}
        if diff:
            version_problems.append(
                f"profile config drift vs the current workflow inputs: {diff} — "
                f"cycles measured under a different configuration cannot be "
                f"compared; rebuild with fresh_start")

# ── early_stop block (the watchdog threshold source, §7.2) ───────────────────
if isinstance(c.get("early_stop"), dict):
    es = c["early_stop"]
    for key in ("warmup_frac", "streak_frac"):
        v = es.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 < v < 1:
            problems.append(f"early_stop.{key} must be a fraction in (0, 1), got {v!r}")
else:
    problems.append("early_stop must be an object {warmup_frac, streak_frac}")

# ── schema completeness (gate mode only; reuse just needs version + shas) ────
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
    # admission clause (v7 C8): the clause TEXT lives only in the po_contract
    # agent document; contracts.json records the stable boolean ack
    if c.get("admission_clause_ack") is not True:
        problems.append("contracts.json admission_clause_ack must be true (the "
                        "clause text itself lives only in the po_contract agent "
                        "document — single source)")
    # full training budget: the value-level fairness fingerprint (epoch-only,
    # v7 C6 — the data-knob fields are deleted)
    fbt = c.get("full_train_budget")
    if not isinstance(fbt, dict):
        problems.append("contracts.json missing 'full_train_budget' "
                        "(epochs/seed fingerprint)")
    else:
        if set(fbt) != {"epochs", "seed"}:
            problems.append("full_train_budget must carry exactly "
                            "{epochs, seed} (v7: the data-knob pair is deleted)")
        if not isinstance(fbt.get("epochs"), int) or fbt.get("epochs", 0) < 1:
            problems.append("full_train_budget.epochs must be an int >= 1")
        if not isinstance(fbt.get("seed"), int):
            problems.append("full_train_budget.seed must be an int")
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
    # proxy_budget (v7 C6): epoch-only, pinned in the gate — the data-knob /
    # max-steps mechanism is deleted, the current shape is the only legal one
    pb = c.get("proxy_budget")
    if not isinstance(pb, dict):
        problems.append("proxy_budget must be an object")
    else:
        if set(pb) != {"epochs", "seed"}:
            problems.append("proxy_budget must carry exactly {epochs, seed} "
                            "(v7: dataset_knob/data_value/max_steps are deleted)")
        if pb.get("epochs") != 1:
            problems.append(f"proxy_budget.epochs must be exactly 1 "
                            f"(min(1, full epochs); got {pb.get('epochs')!r})")
        if not isinstance(pb.get("seed"), int):
            problems.append("proxy_budget.seed must be an int")
        if c.get("probe_cap_mechanism") != "stop-at-k":
            problems.append("probe_cap_mechanism must be stop-at-k "
                            "(full-epoch render + external stop at epoch k)")
    if not isinstance(c.get("exemptions"), list):
        problems.append("exemptions must be a list")
    if not isinstance(c.get("viable"), bool):
        problems.append("viable must be a boolean")
    # sitecustomize merge (v7 C7): the merge fact is part of the contract
    if not isinstance(c.get("sitecustomize_merge"), dict):
        problems.append("sitecustomize_merge must be an object (the injection "
                        "environment disclosure; no user sitecustomize = the "
                        "empty-merge object)")

    # interpreter flag check evidence (-S/-E would kill the injection)
    if (c.get("interpreter") or {}).get("flags_check") != "pass":
        problems.append("interpreter.flags_check != 'pass' (-S/-E detection must pass)")

    # tier B -> adapted entries must exist on disk
    for section in ("train", "eval"):
        if isinstance(c.get(section), dict) and c[section].get("tier") == "B":
            entry = Path(c[section].get("entry", ""))
            if not entry.is_file():
                problems.append(f"{section} tier B but adapted entry missing: {entry}")

    # quick-run / dual-ckpt / export evidence (measured, not asserted);
    # proxy_budget_selection carries the FULL v7 field set incl. rationale (C5)
    for ev, must in (
        ("contract_work/train_quickrun.json", lambda d:
            d.get("status") == "runs_minimal_budget"),
        ("contract_work/eval_dual_ckpt.json", lambda d:
            d.get("moved") is True),
        ("contract_work/export_check.json", lambda d: d.get("loaded") is True),
        ("contract_work/proxy_budget_selection.json", lambda d:
            set(d) == {"epochs", "seed", "rationale"} and d.get("epochs") == 1
            and isinstance(d.get("seed"), int) and bool(d.get("rationale"))),
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
    # downstream consumer (metric_curve extract in the baseline finalizer and
    # the variant watchdogs) only AFTER the full training has already run. Deterministic re-run here, never the analyst's own claim.
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

    # template token contract (v7 C9: ONE training template — probe and full
    # renders name their own outputs; the byte-identical twin gate is deleted).
    # <<device>> is required in the training template: every training render
    # claims a card through the allocation ledger and binds it with
    # --set device=<idx>; a template without the token silently ignores the
    # allocated card and breaks the ledger's mutual exclusion. <<ckpt>> is
    # FORBIDDEN there (train-from-scratch: no checkpoint is ever loaded).
    for tname, tokens in (
        ("run_full_finetune.template.sh",
         ("<<python>>", "<<epochs>>", "<<out_dir>>", "<<seed>>", "<<device>>")),
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

# ── reuse-mode exit semantics (v7 C3/C4: version/viable/profile drift vs sha
#   drift are DIFFERENT exits — the former needs fresh_start, the latter a
#   plain rebuild) ────────────────────────────────────────────────────────────
if mode == "--reuse-check":
    if not isinstance(c.get("viable"), bool) or c.get("viable") is not True:
        version_problems.append("viable is not true (a failed-contract "
                                "workspace must not be reused as if it passed)")
    if version_problems:
        for key in version_problems:
            print(f"check_contracts: FAIL recorded contracts.json predates the "
                  f"current workflow version or disagrees with it ({key})",
                  file=sys.stderr)
        print("check_contracts: the reusable workspace was built by an older / "
              "differently-configured contract stage — re-run with "
              "fresh_start=true to rebuild the contracts from scratch (never "
              "partially patched)", file=sys.stderr)
        sys.exit(3)
    for p in problems:
        print(f"check_contracts: FAIL {p}", file=sys.stderr)
    if problems:
        sys.exit(1)

# gate mode: the version fields are plain required fields there too
if mode != "--reuse-check":
    problems.extend(version_problems)

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
