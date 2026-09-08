#!/usr/bin/env bash
# render_run.sh — render a run template into an executable script.
#
# The agent only picks parameters (--set k=v); ALL injection plumbing is
# assembled here so no run script is ever hand-copied:
#   1. render orca_inject/header.env (shadow dir/pkgs, project root, the
#      target interpreter's os.pathsep — probed, never hardcoded);
#   2. export the header and run assert_shadow.py FIRST, in the exact same
#      invocation form the entry command will use;
#   3. substitute every <<k>> token from --set; any token left unreplaced
#      fails loud (an empty placeholder in a training command is worse than
#      no command).
#
# Token syntax is <<k>>, NEVER {{k}}: agent.md bodies and template examples are
# Jinja2-rendered by the engine — a {{k}} token would be parsed as a prompt
# variable (validation error + StrictUndefined crash at render time).
#
# Env/defaults (each overridable by --set of the same name):
#   shadow_dir / shadow_pkgs — from ORCA_SHADOW_DIR / ORCA_SHADOW_PKGS or --set
#   project_root — from --set ONLY. Deliberately no env fallback: the
#   engine already exports a same-purpose project-root env var (it resolves
#   to the Orca repository root, not the user project), so an env fallback
#   here would render run scripts anchored to the wrong project. The sole
#   workspace source of the user project root is readiness/readiness.json.
#   python — from --set python / ORCA_PYTHON / python3
#
# stdout: single-line JSON {"script": path}; logs to stderr.
#
# NOTE: run this from the DEPLOYED layout ($ORCA_ARTIFACTS_DIR/scripts/), where
# orca_inject/header.env sits at ../orca_inject/ relative to this script.
set -euo pipefail
# bash >= 5.2 expands & in ${var//pat/repl} to the matched text
# (patsub_replacement, on by default) — token values must substitute
# LITERALLY, so a legal '&' in a path can never re-inject a token
shopt -u patsub_replacement 2>/dev/null || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ART="${ORCA_ARTIFACTS_DIR:?FATAL: ORCA_ARTIFACTS_DIR not set (render_run.sh)}"
HEADER_SKEL="$SCRIPT_DIR/../orca_inject/header.env"

TEMPLATE=""
OUT=""
declare -A SETMAP=()
while [ $# -gt 0 ]; do
  case "$1" in
    --template) TEMPLATE="${2:?--template needs a value}"; shift 2 ;;
    --out)      OUT="${2:?--out needs a value}"; shift 2 ;;
    --set)
      pair="${2:?--set needs K=V}"
      [[ "$pair" == *=* ]] || { echo "FATAL: --set expects K=V, got $pair" >&2; exit 2; }
      key="${pair%%=*}"
      # keys feed a bash glob pattern below — a glob char in the key would
      # silently match (and corrupt) OTHER tokens; reject non-identifiers loud
      [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || {
        echo "FATAL: --set key must be an identifier ([A-Za-z_][A-Za-z0-9_]*), got '$key'" >&2; exit 2; }
      SETMAP["$key"]="${pair#*=}"
      shift 2 ;;
    *) echo "FATAL: unknown arg $1" >&2; exit 2 ;;
  esac
done

[ -n "$TEMPLATE" ] && [ -f "$TEMPLATE" ] || { echo "FATAL: template not found: $TEMPLATE" >&2; exit 2; }
[ -f "$HEADER_SKEL" ] || { echo "FATAL: header skeleton missing: $HEADER_SKEL" >&2; exit 2; }

getval() { # getval <token> <env_var> <default>
  local token="$1" envvar="$2" default="$3"
  if [ -n "${SETMAP[$token]+x}" ]; then printf '%s' "${SETMAP[$token]}"; return; fi
  if [ -n "${!envvar:-}" ]; then printf '%s' "${!envvar}"; return; fi
  printf '%s' "$default"
}

SHADOW_DIR="$(getval shadow_dir ORCA_SHADOW_DIR '')"
SHADOW_PKGS="$(getval shadow_pkgs ORCA_SHADOW_PKGS '')"
# no env fallback for project_root — see the header comment (why)
PROJECT_ROOT="${SETMAP[project_root]-}"
PY="$(getval python ORCA_PYTHON python3)"
[ -n "$SHADOW_DIR" ] || { echo "FATAL: shadow_dir not set (--set shadow_dir=... or ORCA_SHADOW_DIR)" >&2; exit 2; }
[ -n "$SHADOW_PKGS" ] || { echo "FATAL: shadow_pkgs not set (--set shadow_pkgs=... or ORCA_SHADOW_PKGS)" >&2; exit 2; }
[ -n "$PROJECT_ROOT" ] || { echo "FATAL: project_root not set (--set project_root=... — no env fallback, see header comment)" >&2; exit 2; }
command -v "$PY" >/dev/null 2>&1 || [ -x "$PY" ] || { echo "FATAL: python interpreter not found: $PY" >&2; exit 2; }

# os.pathsep OF THE TARGET INTERPRETER — ';' on native Windows, ':' elsewhere.
PATHSEP="$("$PY" -c 'import os; print(os.pathsep)')" || { echo "FATAL: cannot probe os.pathsep via $PY" >&2; exit 2; }

# ── assemble the rendered script ─────────────────────────────────────────────
TPL_NAME="$(basename "$TEMPLATE")"
OUT="${OUT:-$ART/${TPL_NAME%.sh}.rendered.sh}"
mkdir -p "$(dirname "$OUT")"   # callers legitimately target not-yet-existing run dirs

# Header rendering goes through the target interpreter (plain str.replace):
# sed substitutions break on paths containing |/&/\, and this must survive any
# legal path.
HEADER="$(ORCA_HDR_SRC="$HEADER_SKEL" \
  ORCA_HDR_shadow_dir="$SHADOW_DIR" \
  ORCA_HDR_shadow_pkgs="$SHADOW_PKGS" \
  ORCA_HDR_orca_inject_dir="$ART/orca_inject" \
  ORCA_HDR_project_root="$PROJECT_ROOT" \
  ORCA_HDR_pathsep="$PATHSEP" \
  "$PY" -c '
import os
src = open(os.environ["ORCA_HDR_SRC"], encoding="utf-8").read()
for token in ("shadow_dir", "shadow_pkgs", "orca_inject_dir", "project_root", "pathsep"):
    src = src.replace("<<" + token + ">>", os.environ["ORCA_HDR_" + token])
print(src, end="")
')"

{
  echo '#!/usr/bin/env bash'
  echo '# Generated by render_run.sh — do not edit; re-render instead.'
  echo 'set -euo pipefail'
  # unbuffered python for the whole run: the baseline finalizer re-parses the
  # training log INCREMENTALLY per poll cycle — a block-buffered epoch line
  # would sit in the stdio buffer until process exit and starve the live curve
  echo 'export PYTHONUNBUFFERED=1'
  echo
  echo "# ---- injection header (rendered from orca_inject/header.env) ----"
  echo "$HEADER"
  echo
  echo 'PY="'"$PY"'"'
  echo "# ---- runtime shadow assertion (same interpreter + header as the entry) ----"
  echo '"$PY" "'"$ART"'/scripts/assert_shadow.py"'
  # ORCA_RUN_PROJECT_ROOT (a workflow-private name): the engine's own
  # project-root env var resolves to the Orca repository root — the run
  # script must consume its own header-exported anchor instead of ever
  # reading or shadowing the engine's var.
  echo 'cd "$ORCA_RUN_PROJECT_ROOT"'
  echo
  echo "# ---- template body: $TPL_NAME ----"
} > "$OUT"

# token substitution: --set values first, then builtin <<python>> / <<artifacts>>
BODY="$(cat "$TEMPLATE")"
for key in "${!SETMAP[@]}"; do
  BODY="${BODY//<<$key>>/${SETMAP[$key]}}"
done
BODY="${BODY//<<python>>/$PY}"
BODY="${BODY//<<artifacts>>/$ART}"
printf '%s\n' "$BODY" >> "$OUT"

chmod +x "$OUT"

# fail loud on any unreplaced token (comment lines are skipped: a template may
# legitimately document its token names in a prose comment; the suffix allows
# '-' so a mis-named <<max-steps>> is caught HERE, not as a runtime heredoc)
if LEFT="$(grep -vE '^\s*#' "$OUT" | grep -oE '<<[A-Za-z_][A-Za-z0-9_-]*>>' || true)" && [ -n "$LEFT" ]; then
  echo "FATAL: unreplaced template tokens in $OUT:" >&2
  echo "$LEFT" >&2
  rm -f "$OUT"
  exit 2
fi

echo "render_run: wrote $OUT (pathsep='$PATHSEP' python=$PY)" >&2
"$PY" -c 'import json, sys; print(json.dumps({"script": sys.argv[1]}))' "$OUT"
