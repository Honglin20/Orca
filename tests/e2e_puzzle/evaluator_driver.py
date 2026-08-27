"""evaluator_driver.py -- LLM driver for the puzzle evaluator recall test.

Builds a self-contained prompt for one evaluator on one fixture case (evaluator
body + search_space.yaml + manifest.yaml + flat model source + candidate
catalog, all inlined) and returns the evaluator's verdict text via whichever LLM
backend is available:

  1. anthropic SDK (Claude) -- when ``ANTHROPIC_API_KEY`` is set and the
     ``anthropic`` package is importable. Model overridable via
     ``ORCA_EVAL_MODEL`` (default a recent Sonnet).
  2. opencode headless (``opencode run --model deepseek/deepseek-v4-flash``) --
     the project's standard test backend; used when deepseek auth is present.

When neither path is usable, :func:`llm_available` returns False and the recall
test skips. The recall *number* is therefore environment-gated; the deterministic
fixture-integrity test (in ``test_puzzle_evaluator_recall.py``) is the always-on
guard.

Everything is inlined into the prompt so the judge needs no tool calls -- this
keeps the verdict deterministic and independent of the host's tool layer.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SUBAGENTS_DIR = REPO_ROOT / "workflows" / "puzzle" / "subagents"
CATALOG_PATH = REPO_ROOT / "workflows" / "puzzle" / "agents" / "_puzzle_scripts" / "candidate_catalog.yaml"

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
OPENCODE_MODEL = "deepseek/deepseek-v4-flash"
OPENCODE_TIMEOUT = 180  # seconds per case


def evaluator_md_path(evaluator_name: str) -> Path:
    return SUBAGENTS_DIR / f"{evaluator_name}.md"


def _deepseek_auth_present() -> bool:
    p = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    if not p.exists():
        return False
    try:
        import json

        d = json.loads(p.read_text(encoding="utf-8"))
        return isinstance(d, dict) and "deepseek" in d
    except Exception:
        return False


def _anthropic_ready() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    return importlib.util.find_spec("anthropic") is not None


def llm_available() -> bool:
    """True iff at least one LLM backend can actually run a completion."""
    if _anthropic_ready():
        return True
    return bool(shutil.which("opencode")) and _deepseek_auth_present()


def _backend_label() -> str:
    if _anthropic_ready():
        return f"anthropic ({os.environ.get('ORCA_EVAL_MODEL', DEFAULT_ANTHROPIC_MODEL)})"
    return f"opencode ({OPENCODE_MODEL})"


def build_prompt(evaluator_name: str, case_dir: Path, expected: dict) -> str:
    """Assemble the self-contained judge prompt for one case."""
    md = evaluator_md_path(evaluator_name).read_text(encoding="utf-8")
    search_space = (case_dir / "search_space.yaml").read_text(encoding="utf-8")
    manifest = (case_dir / "manifest.yaml").read_text(encoding="utf-8")
    flat_rel = expected.get("flat_relpath", "")
    flat_src = ""
    if flat_rel:
        flat_path = case_dir.parent / flat_rel
        if flat_path.is_file():
            flat_src = flat_path.read_text(encoding="utf-8")
    catalog = CATALOG_PATH.read_text(encoding="utf-8")

    return f"""You are the {evaluator_name} subagent. Your operating instructions (body) are delimited by <evaluator-body></evaluator-body>. Follow its Procedure exactly and return only the verdict described in its Output section.

<evaluator-body>
{md}
</evaluator-body>

<inputs>
<search_space.yaml>
{search_space}
</search_space.yaml>

<manifest.yaml>
{manifest}
</manifest.yaml>

<flat-model-source path="{flat_rel}">
{flat_src}
</flat-model-source>

<candidate_catalog.yaml>
{catalog}
</candidate_catalog.yaml>
</inputs>

Audit the inputs per your body and return the verdict now (LGTM on its own line, or the markdown bullet error list). Do not include any analysis narration.
"""


def run_evaluator(evaluator_name: str, case_dir: Path, expected: dict) -> str:
    """Run one evaluator on one case and return the raw verdict text.

    Raises ``RuntimeError`` if the chosen backend returns no usable text or
    surfaces a backend fault (auth / balance / timeout) -- the caller turns
    that into a skip so the recall number is re-measured when the backend
    recovers.
    """
    prompt = build_prompt(evaluator_name, case_dir, expected)
    if _anthropic_ready():
        text = _run_anthropic(prompt)
    elif shutil.which("opencode") and _deepseek_auth_present():
        text = _run_opencode(prompt)
    else:
        raise RuntimeError("no LLM backend available")
    return _strip_sentinel(text, evaluator_name)


def _strip_sentinel(text: str, evaluator_name: str) -> str:
    """Drop the leading ``[subagent:<name> v1 <sentinel>]`` echo line.

    The evaluator body instructs the model to echo its sentinel as the first
    line; the recall test's ``startswith("LGTM")`` scorer needs the verdict
    itself on line 1, so strip the echo when present.
    """
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("[subagent:"):
        lines = lines[1:]
    out = "\n".join(lines).strip()
    if not out:
        raise RuntimeError(
            f"evaluator {evaluator_name} returned only a sentinel echo; no verdict"
        )
    return out


# Signatures that indicate the backend itself failed (rather than producing a
# verdict). Surfacing these as RuntimeError lets the recall test skip instead
# of scoring a balance/auth error as a recall miss.
_BACKEND_FAULT_REASONS = (
    "insufficient balance",
    "insufficient_quota",
    "rate limit",
    "rate_limit",
    "unauthorized",
    "invalid api key",
    "authentication",
    "401",
    "403",
    "429",
)


def _looks_like_backend_fault(text: str) -> bool:
    low = text.lower()
    return any(sig in low for sig in _BACKEND_FAULT_REASONS)


def _run_anthropic(prompt: str) -> str:
    import anthropic  # lazy import; only required when this backend is chosen

    model = os.environ.get("ORCA_EVAL_MODEL", DEFAULT_ANTHROPIC_MODEL)
    client = anthropic.Anthropic()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=2048,
            system=(
                "You are a strict neural architecture search code reviewer. "
                "Follow the evaluator procedure exactly. Output only the verdict."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # auth / rate-limit / network -- skip, not fail
        raise RuntimeError(f"anthropic backend error: {e}") from e
    text = "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    )
    if not text.strip() or _looks_like_backend_fault(text):
        raise RuntimeError(f"anthropic returned no usable verdict: {text[:200]!r}")
    return text.strip()


def _run_opencode(prompt: str) -> str:
    try:
        proc = subprocess.run(
            ["opencode", "run", "--model", OPENCODE_MODEL, prompt],
            capture_output=True,
            text=True,
            timeout=OPENCODE_TIMEOUT,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"opencode timed out after {OPENCODE_TIMEOUT}s") from e
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    combined = f"{out}\n{err}"
    if _looks_like_backend_fault(combined):
        raise RuntimeError(f"opencode backend fault: {combined[:200]!r}")
    if proc.returncode != 0 or not out:
        raise RuntimeError(
            f"opencode returned no usable verdict (rc={proc.returncode}); "
            f"stderr={err[:200]!r}"
        )
    return out


def backend_label() -> str:
    return _backend_label()
