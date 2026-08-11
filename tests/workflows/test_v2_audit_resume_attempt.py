"""S4b ④ —— ns_run_search Step R two-branch attempt N reconstruction.

Verifies the agent.md edits that fix the root cause: a fresh sub-agent resuming mid-search did not
reconstruct the attempt counter ``N``, so it could (a) tail a non-existent log and misjudge a
running search as dead-hung, then (b) ``kill -- -<pgid>`` the live search. The fix (SPEC §2.4)
splits Step R into two branches with distinct N semantics:

  - **RESUME_SEARCH** (search running): ``N = latest-mtime search.attempt*.stdout.log number``
    (Step 2b uses this N immediately to ``tail`` the in-flight log).
  - **RESUME_HEAL** (search dead / not started): ``N = max(existing number) + 1``
    (Step 2a re-detach writes a new log file, never overwriting existing attempt1..attempt${N-1}).

A blanket ``max+1`` is forbidden — when dead attempt logs linger, the max number is not the running
number, and ``tail`` would read a stale log → false dead-hang → wrongful group-kill.

Tests run the actual Step R bash extracted from each ``agent.md`` (v1/v2/v3) against parametrized
fixtures (mock ``runs/search/`` with attempt logs of controlled mtime + ``.search_pid`` of a live
or absent PID). AC-④1 covers the three cases; AC-④2 covers the non-overwrite property.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
AGENTS = REPO / "workflows" / "agents"

RUN_SEARCH_AGENTS = [
    AGENTS / "ns_run_search" / "agent.md",
    AGENTS / "ns2_run_search" / "agent.md",
    AGENTS / "ns3_run_search" / "agent.md",
]


def _extract_step_r_bash(agent_md: Path) -> str:
    """Pull the Step R bash snippet verbatim out of the agent.md.

    The Step R section is the one that detects whether the search is running and emits either
    ``RESUME_SEARCH`` or ``RESUME_HEAL``. Pins the real source (Rule 9).
    """
    text = agent_md.read_text(encoding="utf-8")
    # Find the Step R section (heading may be Chinese or English).
    m = re.search(r"(## Step R\b.*?)(?=\n## |\Z)", text, flags=re.DOTALL)
    assert m, f"{agent_md.name}: '## Step R' section not found"
    section = m.group(1)
    # Grab the first ```bash ... ``` fenced block in the section.
    code = re.search(r"```bash\n(.*?)\n```", section, flags=re.DOTALL)
    assert code, f"{agent_md.name}: Step R has no ```bash fenced block"
    snippet = code.group(1)
    # Sanity: snippet must mention both branches' echo strings.
    assert "RESUME_SEARCH" in snippet and "RESUME_HEAL" in snippet, (
        f"{agent_md.name}: Step R snippet must contain both RESUME_SEARCH and RESUME_HEAL branches"
    )
    return snippet


def _make_attempt_log(runs_search: Path, n: int, mtime_offset: float, content: str | None = None) -> Path:
    """Create ``search.attempt{n}.stdout.log`` with mtime offset relative to now (higher=newer)."""
    p = runs_search / f"search.attempt{n}.stdout.log"
    p.write_text(content if content is not None else f"attempt {n} log\n")
    ts = time.time() + mtime_offset
    os.utime(p, (ts, ts))
    return p


def _run_step_r(snippet: str, artifacts_dir: Path) -> str:
    """Run the Step R bash snippet with ORCA_ARTIFACTS_DIR=artifacts_dir; return stdout."""
    env = os.environ.copy()
    env["ORCA_ARTIFACTS_DIR"] = str(artifacts_dir)
    # bash is required: the snippet uses bash-only syntax (${LAST_N:-0}, [[ ]], kill -0, $(())).
    proc = subprocess.run(
        ["bash", "-c", snippet],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(artifacts_dir),
    )
    assert proc.returncode == 0, (
        f"Step R bash failed rc={proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    return proc.stdout


def _parse_n(stdout: str) -> tuple[int, str]:
    """Return (N, branch) parsed from the Step R stdout (RESUME_SEARCH attempt=N | RESUME_HEAL new_attempt=N)."""
    # Match either RESUME_SEARCH ... attempt=N or RESUME_HEAL ... new_attempt=N.
    m = re.search(r"RESUME_SEARCH\b.*?attempt=(\d+)", stdout)
    if m:
        return int(m.group(1)), "RESUME_SEARCH"
    m = re.search(r"RESUME_HEAL\b.*?new_attempt=(\d+)", stdout)
    if m:
        return int(m.group(1)), "RESUME_HEAL"
    pytest.fail(f"could not parse N from Step R stdout: {stdout!r}")


@pytest.fixture
def artifacts(tmp_path: Path) -> Path:
    """Empty artifacts dir with runs/search/ created (Step R cd's into ORCA_ARTIFACTS_DIR)."""
    (tmp_path / "runs" / "search").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# AC-④1 — three N-reconstruction cases
# ---------------------------------------------------------------------------


class TestACFourOneBranchNReconstruction:
    """AC-④1 (a/b/c): two-branch N reconstruction under different attempt-log + pid states."""

    @pytest.mark.parametrize("agent_md", RUN_SEARCH_AGENTS, ids=["v1", "v2", "v3"])
    def test_a_resume_search_picks_latest_mtime_not_max(
        self, agent_md: Path, artifacts: Path
    ) -> None:
        """AC-④1 (a): RESUME_SEARCH — attempt1 (old/dead) + attempt2 (new/running) + live PID → N=2.

        A blanket max+1 would give N=3 → Step 2b tails non-existent attempt3.log → false dead-hang.
        """
        runs_search = artifacts / "runs" / "search"
        _make_attempt_log(runs_search, 1, mtime_offset=-100, content="old dead attempt\n")
        _make_attempt_log(runs_search, 2, mtime_offset=0, content="in-flight attempt\n")
        # Live PID: spawn a long-sleeping child the snippet can kill -0.
        sleeper = _spawn_sleeper()
        try:
            (runs_search / ".search_pid").write_text(str(sleeper.pid))
            snippet = _extract_step_r_bash(agent_md)
            stdout = _run_step_r(snippet, artifacts)
            n, branch = _parse_n(stdout)
            assert branch == "RESUME_SEARCH", (
                f"{agent_md.name}: live PID must pick RESUME_SEARCH, got {branch!r} (stdout={stdout!r})"
            )
            assert n == 2, (
                f"{agent_md.name}: RESUME_SEARCH must pick latest-mtime (attempt2), got N={n} "
                f"(a blanket max+1 would give N=3 and tail a non-existent log)"
            )
        finally:
            _terminate(sleeper)

    @pytest.mark.parametrize("agent_md", RUN_SEARCH_AGENTS, ids=["v1", "v2", "v3"])
    def test_b_resume_heal_picks_max_plus_one(
        self, agent_md: Path, artifacts: Path
    ) -> None:
        """AC-④1 (b): RESUME_HEAL — attempt1/2/3 all dead + no live PID → N=4.

        Step 2a will re-detach writing attempt4.log, leaving attempt1..3 untouched (AC-④2).
        """
        runs_search = artifacts / "runs" / "search"
        for n in (1, 2, 3):
            _make_attempt_log(runs_search, n, mtime_offset=-100 + n, content=f"dead attempt {n}\n")
        # No .search_pid (or a stale one whose PID doesn't exist).
        (runs_search / ".search_pid").write_text("999999")
        snippet = _extract_step_r_bash(agent_md)
        stdout = _run_step_r(snippet, artifacts)
        n, branch = _parse_n(stdout)
        assert branch == "RESUME_HEAL", (
            f"{agent_md.name}: no live search must pick RESUME_HEAL, got {branch!r} (stdout={stdout!r})"
        )
        assert n == 4, (
            f"{agent_md.name}: RESUME_HEAL must pick max(3)+1=4, got N={n}"
        )

    @pytest.mark.parametrize("agent_md", RUN_SEARCH_AGENTS, ids=["v1", "v2", "v3"])
    def test_c_resume_heal_empty_starts_at_one(
        self, agent_md: Path, artifacts: Path
    ) -> None:
        """AC-④1 (c): no attempt logs + no PID → N=1 (fresh start)."""
        # No logs, no .search_pid at all.
        snippet = _extract_step_r_bash(agent_md)
        stdout = _run_step_r(snippet, artifacts)
        n, branch = _parse_n(stdout)
        assert branch == "RESUME_HEAL", (
            f"{agent_md.name}: empty state must pick RESUME_HEAL, got {branch!r} (stdout={stdout!r})"
        )
        assert n == 1, f"{agent_md.name}: empty state must start at N=1, got N={n}"

    @pytest.mark.parametrize("agent_md", RUN_SEARCH_AGENTS, ids=["v1", "v2", "v3"])
    def test_b_empty_search_pid_file_routes_to_resume_heal(
        self, agent_md: Path, artifacts: Path
    ) -> None:
        """Edge case distinct from stale-PID: ``.search_pid`` file exists but holds an empty string.

        Distinguishes ``cat | head`` returning empty (this case) vs returning a stale non-existent
        PID (test_b). Both must route to RESUME_HEAL — the ``[ -n "$SPID" ]`` guard rejects empty,
        and ``kill -0`` rejects a dead PID. Pins both defenses independently.
        """
        runs_search = artifacts / "runs" / "search"
        for n in (1, 2, 3):
            _make_attempt_log(runs_search, n, mtime_offset=-100 + n, content=f"dead attempt {n}\n")
        # Empty .search_pid (e.g. file created but leader hadn't written its PID before crash).
        (runs_search / ".search_pid").write_text("")
        snippet = _extract_step_r_bash(agent_md)
        stdout = _run_step_r(snippet, artifacts)
        n, branch = _parse_n(stdout)
        assert branch == "RESUME_HEAL", (
            f"{agent_md.name}: empty .search_pid must route to RESUME_HEAL, got {branch!r} (stdout={stdout!r})"
        )
        assert n == 4, f"{agent_md.name}: empty .search_pid + 3 attempts → N=max+1=4, got N={n}"


# ---------------------------------------------------------------------------
# AC-④2 — RESUME_HEAL N=max+1 preserves existing attempt logs
# ---------------------------------------------------------------------------


class TestACFourTwoNoOverwrite:
    """AC-④2: Step 2a re-detach with RESUME_HEAL N=max+1 must not overwrite existing attempt logs."""

    @pytest.mark.parametrize("agent_md", RUN_SEARCH_AGENTS, ids=["v1", "v2", "v3"])
    def test_existing_attempts_untouched_when_new_detach_uses_max_plus_one(
        self, agent_md: Path, artifacts: Path
    ) -> None:
        """Snapshot attempt1/2/3 (mtime + content) → Step R yields N=4 → writing attempt4.log leaves 1/2/3 intact.

        This pins the *property* the two-branch design protects: distinct filenames mean the new
        detach's ``> search.attempt${N}.stdout.log`` redirect creates a new file rather than truncating
        an existing one. A blanket max+1 under RESUME_SEARCH (instead of RESUME_HEAL) would be wrong
        for a different reason (false dead-hang), but here we verify the RESUME_HEAL non-overwrite.
        """
        runs_search = artifacts / "runs" / "search"
        originals: dict[int, tuple[float, str]] = {}
        for n in (1, 2, 3):
            p = _make_attempt_log(runs_search, n, mtime_offset=-100 + n, content=f"dead attempt {n}\n")
            originals[n] = (p.stat().st_mtime, p.read_text())

        # Step R resolves N under RESUME_HEAL (no live PID).
        (runs_search / ".search_pid").write_text("999999")
        snippet = _extract_step_r_bash(agent_md)
        stdout = _run_step_r(snippet, artifacts)
        n, branch = _parse_n(stdout)
        assert branch == "RESUME_HEAL" and n == 4, (
            f"setup invariant failed: expected RESUME_HEAL N=4, got {branch} N={n}"
        )

        # Simulate Step 2a detach's redirect: write to attempt${N}.stdout.log.
        new_log = runs_search / f"search.attempt{n}.stdout.log"
        new_log.write_text("fresh detach output\n")

        # Verify attempt1/2/3 unchanged (content + mtime).
        for k, (orig_mtime, orig_content) in originals.items():
            p = runs_search / f"search.attempt{k}.stdout.log"
            assert p.read_text() == orig_content, f"attempt{k} content was overwritten"
            assert p.stat().st_mtime == orig_mtime, f"attempt{k} mtime changed (overwrite detected)"
        # And the new attempt4.log exists as a distinct file.
        assert new_log.exists(), "new attempt log was not created"
        assert (runs_search / "search.attempt4.stdout.log").read_text() == "fresh detach output\n"


# ---------------------------------------------------------------------------
# Helpers: spawn a long-sleeping child whose PID is valid for kill -0
# ---------------------------------------------------------------------------


def _spawn_sleeper() -> subprocess.Popen:
    """Spawn a child that sleeps long enough for kill -0 to succeed throughout the test."""
    # /dev/null on the WSL side; on Windows bash this is Git Bash which understands /dev/null.
    return subprocess.Popen(
        ["sleep", "60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _terminate(proc: subprocess.Popen) -> None:
    """Best-effort terminate + reap; never raise (test cleanup)."""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
