"""Smoke tests for monitor_until_done.sh + launch.sh static gates (C1/C2).

C1: monitor_until_done.sh five stdout states + cheap liveness + token wildcard.
C2: launch.sh ATTEMPT_BUDGET_EXHAUSTED removed + rc file cleanup + unlimited.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_TRAIN_SCRIPTS = _REPO / "workflows" / "nas-supernet" / "agents" / "ns_run_train" / "scripts"
_RETRAIN_SCRIPTS = _REPO / "workflows" / "nas-supernet" / "agents" / "ns_retrain" / "scripts"
_SEARCH_SCRIPTS = _REPO / "workflows" / "nas-supernet" / "agents" / "ns_run_search" / "scripts"


# ---------------------------------------------------------------------------
# C1: monitor_until_done.sh bash -n + structure
# ---------------------------------------------------------------------------


class TestMonitorBashSyntax:
    def test_train_monitor_syntax(self):
        """bash -n passes for ns_run_train monitor."""
        result = subprocess.run(
            ["bash", "-n", str(_TRAIN_SCRIPTS / "monitor_until_done.sh")],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"

    def test_retrain_monitor_syntax(self):
        """bash -n passes for ns_retrain monitor."""
        result = subprocess.run(
            ["bash", "-n", str(_RETRAIN_SCRIPTS / "monitor_until_done.sh")],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"

    def test_train_launch_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(_TRAIN_SCRIPTS / "launch.sh")],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"

    def test_retrain_launch_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(_RETRAIN_SCRIPTS / "launch.sh")],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"


# ---------------------------------------------------------------------------
# C2: launch.sh static gates — ATTEMPT_BUDGET_EXHAUSTED removed, rc cleanup added
# ---------------------------------------------------------------------------


class TestLaunchStaticGates:
    def test_train_no_attempt_budget_exhausted(self):
        """ATTEMPT_BUDGET_EXHAUSTED removed from ns_run_train launch.sh."""
        content = (_TRAIN_SCRIPTS / "launch.sh").read_text()
        assert "ATTEMPT_BUDGET_EXHAUSTED" not in content, \
            "ATTEMPT_BUDGET_EXHAUSTED must be removed (unlimited self-heal)"

    def test_retrain_no_attempt_budget_exhausted(self):
        """ATTEMPT_BUDGET_EXHAUSTED removed from ns_retrain launch.sh."""
        content = (_RETRAIN_SCRIPTS / "launch.sh").read_text()
        assert "ATTEMPT_BUDGET_EXHAUSTED" not in content

    def test_train_no_n3_cap(self):
        """N>3 cap removed from ns_run_train launch.sh."""
        content = (_TRAIN_SCRIPTS / "launch.sh").read_text()
        assert '-gt 3' not in content, "N>3 cap must be removed"

    def test_retrain_no_n3_cap(self):
        """N>3 cap removed from ns_retrain launch.sh."""
        content = (_RETRAIN_SCRIPTS / "launch.sh").read_text()
        assert '-gt 3' not in content

    def test_train_rc_file_cleanup(self):
        """ns_run_train launch.sh rm list includes .train_rc (NEW-1)."""
        content = (_TRAIN_SCRIPTS / "launch.sh").read_text()
        assert ".train_rc" in content, \
            "launch.sh must clean .train_rc to prevent stale-rc false exit detection"

    def test_retrain_rc_file_cleanup(self):
        """ns_retrain launch.sh rm list includes .retrain_rc (NEW-1)."""
        content = (_RETRAIN_SCRIPTS / "launch.sh").read_text()
        assert ".retrain_rc" in content


# ---------------------------------------------------------------------------
# C1: monitor_until_done.sh structure — token matching + cheap liveness
# ---------------------------------------------------------------------------


class TestMonitorStructure:
    def test_train_uses_train_paths(self):
        """ns_run_train monitor uses runs/train/ + .train_* paths."""
        content = (_TRAIN_SCRIPTS / "monitor_until_done.sh").read_text()
        assert "runs/train/" in content
        assert ".train_pid" in content
        assert ".train_rc" in content

    def test_retrain_uses_retrain_paths(self):
        """ns_retrain monitor uses runs/retrain/ + .retrain_* paths."""
        content = (_RETRAIN_SCRIPTS / "monitor_until_done.sh").read_text()
        assert "runs/retrain/" in content
        assert ".retrain_pid" in content
        assert ".retrain_rc" in content

    def test_both_use_wildcard_case(self):
        """Both monitors use *COMPLETE*/*INCOMPLETE* suffix wildcards (match TRAIN_*/RETRAIN_*)."""
        for script in [_TRAIN_SCRIPTS / "monitor_until_done.sh",
                       _RETRAIN_SCRIPTS / "monitor_until_done.sh"]:
            content = script.read_text()
            assert "*COMPLETE*" in content
            assert "*INCOMPLETE*" in content
            assert "*STUCK*" in content
            assert "STILL_RUNNING" in content
            assert "GATE_SKIP" in content

    def test_both_cheap_liveness(self):
        """Both monitors use kill -0 + rc file check (not status.sh/torch.load every 60s)."""
        for script in [_TRAIN_SCRIPTS / "monitor_until_done.sh",
                       _RETRAIN_SCRIPTS / "monitor_until_done.sh"]:
            content = script.read_text()
            assert "kill -0" in content
            assert "RC_EXISTS" in content

    def test_both_set_u(self):
        """Both monitors use set -u (not set -e) for fail-soft."""
        for script in [_TRAIN_SCRIPTS / "monitor_until_done.sh",
                       _RETRAIN_SCRIPTS / "monitor_until_done.sh"]:
            content = script.read_text()
            assert "set -u" in content
            # set -e should NOT appear as an active command (fail-soft: all branches echo + exit 0).
            # Check only non-comment lines for actual "set -e" command.
            active_lines = [l.strip() for l in content.split("\n")
                           if l.strip() and not l.strip().startswith("#")]
            has_set_e = any(l.startswith("set -e") and not l.startswith("set -eu") for l in active_lines)
            assert not has_set_e, "monitor must NOT use set -e as active command (fail-soft required)"

    def test_both_stuck_regex_has_error_token(self):
        """NEW-5: STUCK regex includes 'error' token + tolerant tail anchor [:.:]."""
        for script in [_TRAIN_SCRIPTS / "monitor_until_done.sh",
                       _RETRAIN_SCRIPTS / "monitor_until_done.sh"]:
            content = script.read_text()
            # The regex should include "error" as a token
            assert "error" in content
            # Tail anchor should tolerate [:.]  (period/colon after token)
            # Look for the grep pattern
            assert "[:.:]" in content or "[[:space:],).:]" in content, \
                "STUCK regex tail anchor should tolerate [:.:]"


# ---------------------------------------------------------------------------
# C2: agent.md static gates — no CRON, no 3-attempt, no ATTEMPT_BUDGET
# ---------------------------------------------------------------------------


class TestAgentMdStaticGates:
    def test_train_no_cron_in_tools(self):
        content = (_REPO / "workflows" / "nas-supernet" / "agents" / "ns_run_train" / "agent.md").read_text()
        assert "cron" not in content.lower(), \
            "cron (case-insensitive) must be completely removed from ns_run_train agent.md"

    def test_retrain_no_cron_in_tools(self):
        content = (_REPO / "workflows" / "nas-supernet" / "agents" / "ns_retrain" / "agent.md").read_text()
        assert "cron" not in content.lower(), \
            "cron (case-insensitive) must be completely removed from ns_retrain agent.md"

    def test_yaml_no_cron(self):
        """nas-supernet.yaml should not contain 'cron' (case-insensitive)."""
        content = (_REPO / "workflows" / "nas-supernet" / "workflow.yaml").read_text()
        assert "cron" not in content.lower(), \
            "nas-supernet.yaml must not reference CRON"

    def test_yaml_no_3_attempt(self):
        """nas-supernet.yaml should not contain '3 次' or 'max_retries=3'."""
        content = (_REPO / "workflows" / "nas-supernet" / "workflow.yaml").read_text()
        assert "3 次" not in content
        assert "max_retries=3" not in content

    def test_search_no_3_attempt(self):
        """ns_run_search agent.md should not contain '最多 3 次' or 'N>3 放弃'."""
        content = (_REPO / "workflows" / "nas-supernet" / "agents" / "ns_run_search" / "agent.md").read_text()
        assert "最多 3 次" not in content
        assert "N>3 放弃" not in content
        assert "max_retries=3" not in content

    def test_update_status_md_no_cron(self):
        """NEW-2: update_status_md.sh echo text should not contain 'CRON'."""
        for script in [_TRAIN_SCRIPTS / "update_status_md.sh",
                       _RETRAIN_SCRIPTS / "update_status_md.sh"]:
            content = script.read_text()
            assert "CRON" not in content, \
                f"{script.name} should not reference CRON in echo text"


# ---------------------------------------------------------------------------
# Mirror sync: train ↔ retrain scripts differ only by path prefix
# ---------------------------------------------------------------------------


class TestMirrorSync:
    def test_monitor_same_line_count(self):
        """Monitor scripts should have same number of lines (structural mirror)."""
        train = (_TRAIN_SCRIPTS / "monitor_until_done.sh").read_text()
        retrain = (_RETRAIN_SCRIPTS / "monitor_until_done.sh").read_text()
        assert len(train.splitlines()) == len(retrain.splitlines()), \
            "monitor scripts should have same line count"

    def test_monitor_same_structure(self):
        """Monitor scripts share identical case branches, loop structure, env vars."""
        for key in ["DEADLINE", "INTERVAL", "STALL_POLLS", "ALIVE", "RC_EXISTS",
                     "*COMPLETE*", "*INCOMPLETE*", "*STUCK*", "STILL_RUNNING",
                     "GATE_SKIP", "LAST_SIZE", "kill -0", "set -u"]:
            for script in [_TRAIN_SCRIPTS / "monitor_until_done.sh",
                           _RETRAIN_SCRIPTS / "monitor_until_done.sh"]:
                assert key in script.read_text(), \
                    f"{script.name} missing structural element: {key}"

    def test_monitor_path_prefix_differs(self):
        """Monitor scripts correctly use their respective path prefixes."""
        train = (_TRAIN_SCRIPTS / "monitor_until_done.sh").read_text()
        retrain = (_RETRAIN_SCRIPTS / "monitor_until_done.sh").read_text()
        assert "runs/train/" in train and ".train_pid" in train
        assert "runs/retrain/" in retrain and ".retrain_pid" in retrain

    def test_common_mirror_identity(self):
        """_common.py should be identical between ns_retrain and ns_run_search."""
        retrain_common = (_RETRAIN_SCRIPTS / "_common.py").read_text()
        search_common = (_SEARCH_SCRIPTS / "_common.py").read_text()
        assert retrain_common == search_common, \
            "_common.py must be identical between ns_retrain and ns_run_search mirrors"


# ---------------------------------------------------------------------------
# A3: metrics_bar caption dynamic sample count
# ---------------------------------------------------------------------------


class TestMetricsBarDynamicCaption:
    """A3: metrics_bar caption uses len(records) not hardcoded '640'."""

    def test_caption_contains_record_count(self, tmp_path: Path) -> None:
        """Caption should contain the actual record count, not hardcoded '640'."""
        import json

        records = [
            {"gene": [0], "objs": {"acc": -0.17, "latency": 0.13}, "pareto": True},
            {"gene": [1], "objs": {"acc": -0.20, "latency": 0.32}, "pareto": True},
            {"gene": [2], "objs": {"acc": -0.09, "latency": 0.45}, "pareto": False},
        ]
        for r in records:
            (tmp_path / "search_results.jsonl").open("a").write(json.dumps(r) + "\n")
        (tmp_path / "search_config.yaml").write_text(
            'objs:\n  - "acc"\n  - "latency"\n'
        )
        # Load metrics_bar via exec (same pattern as search_table tests).
        mb_path = _RETRAIN_SCRIPTS / "metrics_bar.py"
        mb_ns: dict = {"__file__": str(mb_path)}
        exec(compile(mb_path.read_text(encoding="utf-8"), str(mb_path), "exec"), mb_ns)

        # Capture push_chart calls to inspect caption.
        calls: list[dict] = []
        mb_ns["push_chart"] = lambda **kw: calls.append(kw)
        old_argv = sys.argv
        sys.argv = ["metrics_bar", "--artifacts-dir", str(tmp_path), "--selected-acc", ""]
        try:
            mb_ns["main"]()
        finally:
            sys.argv = old_argv

        assert len(calls) >= 1, "metrics_bar should push at least one chart"
        caption = calls[-1].get("caption", "")
        assert "3" in caption, f"caption should contain record count 3: {caption}"
        assert "640" not in caption, "caption should NOT contain hardcoded '640'"


# ---------------------------------------------------------------------------
# A6: chart push stderr visibility (>/dev/null 2>&1 → > /dev/null)
# ---------------------------------------------------------------------------


class TestChartPushStderrVisible:
    """A6: chart script calls use '> /dev/null || true' (stderr visible, not '2>&1')."""

    def test_search_agent_no_stderr_suppression(self):
        """ns_run_search agent.md chart calls should not have '2>&1'."""
        content = (_REPO / "workflows" / "nas-supernet" / "agents" / "ns_run_search" / "agent.md").read_text()
        # Find chart script call lines.
        chart_lines = [l for l in content.split("\n")
                      if ("pareto.py" in l or "search_table.py" in l or "latency_dist.py" in l)
                      and "python3" in l]
        assert len(chart_lines) >= 4, "should have at least 4 chart script call lines (Step 0 + Step 2.7)"
        for line in chart_lines:
            assert "2>&1" not in line, \
                f"chart call should not suppress stderr with 2>&1: {line.strip()}"

    def test_retrain_agent_no_stderr_suppression(self):
        """ns_retrain agent.md Step 3.5 chart calls should not have '2>&1'."""
        content = (_REPO / "workflows" / "nas-supernet" / "agents" / "ns_retrain" / "agent.md").read_text()
        chart_lines = [l for l in content.split("\n")
                      if ("metrics_bar.py" in l or "compare_table.py" in l)
                      and "python3" in l]
        assert len(chart_lines) >= 2, "should have at least 2 chart script call lines (Step 3.5)"
        for line in chart_lines:
            assert "2>&1" not in line, \
                f"chart call should not suppress stderr with 2>&1: {line.strip()}"

    def test_progress_watcher_identical(self):
        """progress_watcher.py should be identical between train and retrain."""
        train = (_TRAIN_SCRIPTS / "progress_watcher.py").read_text()
        retrain = (_RETRAIN_SCRIPTS / "progress_watcher.py").read_text()
        assert train == retrain, "progress_watcher.py must be identical between mirrors"
