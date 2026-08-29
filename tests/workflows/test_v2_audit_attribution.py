"""S4b ③ —— expand unsupported attribution marker (double signal).

Verifies the agent.md edits that fix the root cause: terminal-state attribution depended on an
LLM-generated free-text substring in ``supernet_summary.md``. The fix adds a structured on-disk
marker ``.ns_expand_unsupported.flag`` (content ``'true'``) written by the expand node's
unsupported branch and read (content-check, not isfile) by the report node, with the summary
substring retained as a fallback for in-flight runs predating the marker.

Two layers are tested:
  1. **Structural** — agent.md content has the rm protocol (Step 0) + marker write (Step 1.5) +
     double-signal read; v1 (``ns_expand_supernet``) is NOT touched (Q1: no terminal report node
     consumes the marker there, so adding it would be dead code).
  2. **Behavioral** — AC-③2 4-case matrix (marker × summary substring) by extracting the report
     python heredoc from ``ns2_report/agent.md`` and ``ns3_report/agent.md`` and running it against
     parametrized fixtures. Pins the real source (Rule 9), not a hand-copied clone.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

NS2_EXPAND = REPO / "workflows" / "nas-supernet-v2" / "agents" / "ns2_expand_supernet" / "agent.md"
NS3_EXPAND = REPO / "workflows" / "nas-supernet-v3" / "agents" / "ns3_expand_supernet" / "agent.md"
NS1_EXPAND = REPO / "workflows" / "nas-supernet" / "agents" / "ns_expand_supernet" / "agent.md"
NS2_REPORT = REPO / "workflows" / "nas-supernet-v2" / "agents" / "ns2_report" / "agent.md"
NS3_REPORT = REPO / "workflows" / "nas-supernet-v3" / "agents" / "ns3_report" / "agent.md"

MARKER = ".ns_expand_unsupported.flag"


# ---------------------------------------------------------------------------
# 1. Structural: agent.md content (the contract the agent reads at runtime)
# ---------------------------------------------------------------------------


def _step(text: str, heading_regex: str) -> str:
    """Return the section body under a heading, up to the next same-level heading.

    Looks for ``## <heading>`` and reads until the next ``## `` at the same indent.
    """
    m = re.search(rf"(## {heading_regex}.*?)(?=\n## |\Z)", text, flags=re.DOTALL)
    assert m, f"heading matching {heading_regex!r} not found"
    return m.group(1)


class TestExpandMarkerProtocol:
    """Step 0 must ``rm`` the marker before reuse-check; Step 1.5 must write it on unsupported."""

    @pytest.mark.parametrize("agent_md", [NS2_EXPAND, NS3_EXPAND], ids=["v2", "v3"])
    def test_step0_rm_before_reuse_check(self, agent_md: Path) -> None:
        """rm protocol (Q5/Q6): marker is cleared before reuse logic, unconditionally."""
        text = agent_md.read_text(encoding="utf-8")
        step0 = _step(text, r"Step 0[^\n]*")
        assert "rm -f .ns_expand_unsupported.flag" in step0 or (
            "rm -f" in step0 and MARKER in step0
        ), f"{agent_md.name}: Step 0 must rm the marker before reuse-check (Q5/Q6)"

        # The rm must come BEFORE the MISSING=/reuse logic (not only inside a reuse-hit branch).
        rm_idx = step0.find("rm -f")
        missing_idx = step0.find("MISSING=")
        assert rm_idx != -1 and missing_idx != -1 and rm_idx < missing_idx, (
            f"{agent_md.name}: rm must precede the MISSING= reuse check"
        )

    @pytest.mark.parametrize("agent_md", [NS2_EXPAND, NS3_EXPAND], ids=["v2", "v3"])
    def test_step1_5_unsupported_branch_writes_marker(self, agent_md: Path) -> None:
        """Q11: only the unsupported branch writes the marker; Q4: content is 'true'; Q10: non-blocking."""
        text = agent_md.read_text(encoding="utf-8")
        # Find the Stop-unsupported instruction (Step 1 item 5).
        m = re.search(r"Stop unsupported NAS branches.*?(?=\n\d+\.|\n### |\Z)", text, flags=re.DOTALL)
        assert m, f"{agent_md.name}: 'Stop unsupported NAS branches' instruction not found"
        stop_section = m.group(0)
        assert "printf 'true'" in stop_section, (
            f"{agent_md.name}: unsupported branch must write marker via printf 'true' (Q4)"
        )
        assert MARKER in stop_section, f"{agent_md.name}: marker filename missing in write instruction"
        # Q10: write failure must be non-blocking (2>/dev/null + WARN, no exit).
        assert "2>/dev/null" in stop_section, (
            f"{agent_md.name}: marker write must suppress stderr (best-effort, non-blocking, Q10)"
        )

    def test_v1_not_included(self) -> None:
        """Q1 Blocker: v1 (ns_expand_supernet) has no terminal report consumer → marker is dead code."""
        text = NS1_EXPAND.read_text(encoding="utf-8")
        assert MARKER not in text, (
            "v1 ns_expand_supernet must NOT reference the marker — no terminal report node reads it "
            "(Q1: adding it is dead code)"
        )

    @pytest.mark.parametrize("agent_md", [NS2_EXPAND, NS3_EXPAND], ids=["v2", "v3"])
    def test_only_one_marker_write_site(self, agent_md: Path) -> None:
        """Q11 reverse pin: exactly one ``printf 'true' > .../marker`` write site in the whole agent.md.

        Forward assertion (test_step1_5_unsupported_branch_writes_marker) checks the write is inside
        the Stop-unsupported section. This reverse assertion catches a future regression where a
        second write site is added elsewhere (e.g. supported path, manifest write) — which would
        violate Q11 "only the unsupported branch writes the marker".
        """
        text = agent_md.read_text(encoding="utf-8")
        # Count write-to-marker instructions (printf 'true' followed by the marker path on the same
        # or continuation line). Tolerate the ``\`` line-continuation the snippet uses.
        writes = len(re.findall(r"printf\s+'true'[^`]*?\.ns_expand_unsupported\.flag", text, flags=re.DOTALL))
        assert writes == 1, (
            f"{agent_md.name}: expected exactly 1 marker write site (Q11), found {writes}"
        )


class TestReportDoubleSignalReadsMarker:
    """The report heredoc must read the marker (content 'true') and keep summary substring as fallback."""

    @pytest.mark.parametrize("agent_md", [NS2_REPORT, NS3_REPORT], ids=["v2", "v3"])
    def test_report_reads_marker_and_summary(self, agent_md: Path) -> None:
        text = agent_md.read_text(encoding="utf-8")
        # The report python must reference the marker path AND keep the substring fallback.
        assert MARKER in text, f"{agent_md.name}: report must read the marker"
        assert '"No supported match"' in text or "'No supported match'" in text, (
            f"{agent_md.name}: report must keep the summary substring fallback for in-flight runs"
        )
        # Content check (read_text == "true"), not isfile (DRY with fidelity flag).
        assert '== "true"' in text or '== \'true\'' in text, (
            f"{agent_md.name}: marker read must be a content check (== \"true\"), not isfile (Q4)"
        )


# ---------------------------------------------------------------------------
# 2. Behavioral: AC-③2 4-case matrix on the extracted report python heredoc
# ---------------------------------------------------------------------------


def _extract_report_python(agent_md: Path) -> str:
    """Pull the report terminal-state python heredoc verbatim out of the agent.md.

    Pins the real source (Rule 9): the test runs the exact python the agent runs.
    """
    text = agent_md.read_text(encoding="utf-8")
    m = re.search(r"python3 - <<'PYEOF'\n(.*?)\nPYEOF", text, flags=re.DOTALL)
    assert m, f"{agent_md.name}: report python heredoc (PYEOF) not found"
    return m.group(1)


def _run_report_python(src: str, artifacts_dir: Path) -> dict:
    """Execute the extracted report python in a subprocess with ORCA_ARTIFACTS_DIR set."""
    env = os.environ.copy()
    env["ORCA_ARTIFACTS_DIR"] = str(artifacts_dir)
    # Windows may not have python3 on PATH; prefer sys.executable's base name.
    proc = subprocess.run(
        [sys.executable, "-c", src],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"report python failed rc={proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    # stdout is the one-line JSON report.
    line = proc.stdout.strip().splitlines()[-1]
    return json.loads(line)


@pytest.fixture
def base_artifacts(tmp_path: Path) -> Path:
    """Artifacts that pass the flatten_failed gate so we can isolate the unsupported branch.

    flatten_failed fires when ``not has_supernet and (not has_flat or not has_manifest)``. To NOT
    fire it we provide both a flat file and a manifest (so ``has_flat and has_manifest``). No other
    terminal-state files are present, so the only branch that can fire is unsupported or the default.
    """
    (tmp_path / "model_flat.py").write_text("# fake flat\n")
    (tmp_path / "project_manifest.md").write_text("# fake manifest\n")
    return tmp_path


def _write_summary(ad: Path, has_unsupported_substring: bool) -> None:
    """Write a supernet_summary.md; toggle the literal 'No supported match' substring."""
    if has_unsupported_substring:
        body = "## Model Type\n- Model Type: No supported match\n- Reason: hybrid\n"
    else:
        body = "## Model Type\n- Model Type: cnn\n- Reason: pure conv stack\n"
    (ad / "supernet_summary.md").write_text(body)


def _write_marker(ad: Path, content: str | None) -> None:
    if content is None:
        return
    (ad / MARKER).write_text(content)


# AC-③2 4-case matrix. marker content × summary substring → expected stage/status.
CASES = [
    # (marker_content, summary_has_substring, expected_stage, expected_status, description)
    ("true", True, "expand", "failed", "both signals fire (marker + substring)"),
    ("true", False, "expand", "failed", "marker alone (LLM forgot the substring)"),
    (None, True, "expand", "failed", "substring fallback (in-flight run pre-marker)"),
    (None, False, "report", "failed", "neither signal → default stage=report (no false positive)"),
]


@pytest.mark.parametrize("agent_md", [NS2_REPORT, NS3_REPORT], ids=["v2", "v3"])
@pytest.mark.parametrize(
    "marker_content,summary_substring,expected_stage,expected_status,desc",
    CASES,
    ids=[c[-1] for c in CASES],
)
def test_ac_3_2_double_signal_matrix(
    agent_md: Path,
    base_artifacts: Path,
    marker_content: str | None,
    summary_substring: bool,
    expected_stage: str,
    expected_status: str,
    desc: str,
) -> None:
    """AC-③2: the 4-case (marker × summary) matrix pins which signal attributes unsupported."""
    _write_summary(base_artifacts, summary_substring)
    _write_marker(base_artifacts, marker_content)
    src = _extract_report_python(agent_md)
    report = _run_report_python(src, base_artifacts)
    assert report["stage"] == expected_stage, (
        f"{agent_md.name} / {desc}: expected stage={expected_stage}, got stage={report['stage']!r} "
        f"status={report['status']!r} reason={report.get('reason')!r}"
    )
    assert report["status"] == expected_status, (
        f"{agent_md.name} / {desc}: expected status={expected_status}, got {report['status']!r}"
    )


@pytest.mark.parametrize("agent_md", [NS2_REPORT, NS3_REPORT], ids=["v2", "v3"])
def test_non_true_marker_content_does_not_fire(agent_md: Path, base_artifacts: Path) -> None:
    """Q4 edge: a marker file that exists but does not hold 'true' must NOT fire unsupported.

    Defends against residue from a half-written / corrupted marker. Content check, not isfile.
    """
    _write_summary(base_artifacts, has_unsupported_substring=False)
    _write_marker(base_artifacts, "false")  # stale / corrupt content
    src = _extract_report_python(agent_md)
    report = _run_report_python(src, base_artifacts)
    assert report["stage"] == "report", (
        f"non-'true' marker content must not trigger unsupported; got stage={report['stage']!r}"
    )


@pytest.mark.parametrize("agent_md", [NS2_REPORT, NS3_REPORT], ids=["v2", "v3"])
def test_unsupported_wins_over_stale_retrain_rc(agent_md: Path, base_artifacts: Path) -> None:
    """Cross-branch priority: marker + stale ``.retrain_rc=1`` co-exist → unsupported (stage=expand) wins.

    The report python judges terminal state by if/elif first-match order. ``unsupported`` is position 2
    (after flatten_failed), ``retrain_failed`` is position 3. A stale ``.retrain_rc`` from a prior
    attempt must NOT mask a fresh unsupported judgment. Pins the order contract so a future refactor
    that swaps the branches won't silently regress attribution.
    """
    _write_summary(base_artifacts, has_unsupported_substring=False)
    _write_marker(base_artifacts, "true")
    # Plant stale retrain-failed signals that WOULD fire retrain_failed if unsupported didn't.
    retrain_dir = base_artifacts / "runs" / "retrain"
    retrain_dir.mkdir(parents=True)
    (retrain_dir / ".retrain_rc").write_text("1")
    (base_artifacts / "retrain_status.md").write_text("# retrain failed status\n")
    src = _extract_report_python(agent_md)
    report = _run_report_python(src, base_artifacts)
    assert report["stage"] == "expand", (
        f"unsupported must win over stale .retrain_rc (first-match order); got stage={report['stage']!r}"
    )
    assert report["status"] == "failed"
