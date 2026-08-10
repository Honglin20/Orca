"""Unit tests for ns_run_train / ns_retrain progress_watcher.

Covers:
  - Per-metric chart separation: each metric key pushes its own chart (distinct
    title, per-metric y_label) instead of a single multi-series hue chart.
  - Same title repeated push -> live-replace semantics (real-time update).
  - fail-soft: missing ORCA_* env -> exit 0 without touching anything
  - fail-soft: render_chart raise (socket down) -> exit 0
  - done-marker driven exit (mtime newer than watcher start)
  - idle timeout only after first point
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

_WATCHER = (
    Path(__file__).resolve().parents[2]
    / "workflows" / "agents" / "ns_retrain" / "scripts" / "progress_watcher.py"
)

_NS: dict = {}
exec(compile(_WATCHER.read_text(encoding="utf-8"), str(_WATCHER), "exec"), _NS)


@pytest.fixture
def env_ok(monkeypatch):
    """注入 4 个 render_chart 身份键（watcher 的 env 检查）。"""
    monkeypatch.setenv("ORCA_RUN_ID", "run-1")
    monkeypatch.setenv("ORCA_NODE", "ns_retrain")
    monkeypatch.setenv("ORCA_SESSION_ID", "sess-1")
    monkeypatch.setenv("ORCA_CHART_SOCK", "/tmp/orca-test.sock")


class TestPushPerMetric:
    def test_each_metric_pushes_own_chart(self):
        """每个指标一个独立图：title 带指标名、y_label=指标名、各数据只含自身点。"""
        calls: list[dict] = []

        def fake_render_chart(**kw):
            calls.append(kw)

        series = {
            "loss": [(1, 0.0491), (2, 0.0327)],
            "test_acc": [(1, 0.9809), (2, 0.9814)],
        }
        import argparse

        args = argparse.Namespace(title="Retrain Metrics (attempt 1)", label="nas-supernet/retrain")
        assert _NS["_push"](args, fake_render_chart, series) is True
        assert len(calls) == 2
        by_title = {c["title"]: c for c in calls}
        loss_chart = by_title["Retrain Metrics (attempt 1): loss"]
        acc_chart = by_title["Retrain Metrics (attempt 1): test_acc"]
        assert loss_chart["chart_type"] == "line"
        assert loss_chart["y_label"] == "loss"
        assert loss_chart["data"] == [{"x": 1, "y": 0.0491}, {"x": 2, "y": 0.0327}]
        assert acc_chart["y_label"] == "test_acc"
        assert acc_chart["data"] == [{"x": 1, "y": 0.9809}, {"x": 2, "y": 0.9814}]
        # 不再推单张 hue=series 全量图。
        assert all("hue" not in c for c in calls)

    def test_empty_series_no_push(self):
        import argparse

        calls: list[dict] = []

        def fake_render_chart(**kw):
            calls.append(kw)

        args = argparse.Namespace(title="t", label="nas-supernet/retrain")
        assert _NS["_push"](args, fake_render_chart, {}) is True
        assert calls == []

    def test_render_failure_returns_false(self):
        import argparse

        def boom(**kw):
            raise RuntimeError("sock down")

        series = {"loss": [(1, 0.1)]}
        args = argparse.Namespace(title="t", label="nas-supernet/retrain")
        assert _NS["_push"](args, boom, series) is False


class TestWatcherLivePush:
    def test_pushes_each_metric_and_exits_on_done(self, tmp_path, env_ok, monkeypatch):
        """progress.jsonl 多指标 → 各自推图；done-marker 晚写 → 最终推 + 退出。"""
        import threading
        import types

        progress = tmp_path / "progress.jsonl"
        marker = tmp_path / ".retrain_rc"
        progress.write_text(
            '{"step": 1, "metrics": {"loss": 0.0491, "test_acc": 0.9809}}\n',
            encoding="utf-8",
        )
        calls: list[dict] = []
        mod = types.ModuleType("orca.chart")
        mod.render_chart = lambda **kw: calls.append(kw) or 1
        monkeypatch.setitem(sys.modules, "orca.chart", mod)
        monkeypatch.chdir(tmp_path)
        sys.argv = ["watcher", "--progress", str(progress), "--done-marker", str(marker),
                    "--label", "nas-supernet/retrain", "--title", "R (attempt 1)",
                    "--poll", "0.01", "--max-idle", "60", "--max-wait", "0.05"]
        results: list[int] = []

        def run():
            results.append(_NS["main"]())

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.1)
        marker.write_text("0", encoding="utf-8")
        t.join(timeout=3)
        assert not t.is_alive()
        assert results == [0]
        # 每指标一张图（loss + test_acc 各 1），done 前最后一次幂等重推 → 共 4 次。
        titles = {c["title"] for c in calls}
        assert titles == {"R (attempt 1): loss", "R (attempt 1): test_acc"}
        assert all(c["chart_type"] == "line" for c in calls)

    def test_missing_env_exits_zero(self, tmp_path, monkeypatch):
        """缺 ORCA_* env → exit 0（fail-soft，不 raise）。"""
        progress = tmp_path / "progress.jsonl"
        marker = tmp_path / ".retrain_rc"
        progress.write_text('{"step": 1, "metrics": {"loss": 0.1}}\n', encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        sys.argv = ["watcher", "--progress", str(progress), "--done-marker", str(marker),
                    "--label", "x", "--title", "t", "--poll", "0.01",
                    "--max-idle", "0.05", "--max-wait", "0.05"]
        assert _NS["main"]() == 0

    def test_render_failure_exits_zero(self, tmp_path, env_ok, monkeypatch):
        """socket 断（render_chart raise）→ stderr 一次 + exit 0（断更不轰炸）。"""
        import types

        progress = tmp_path / "progress.jsonl"
        marker = tmp_path / ".retrain_rc"
        progress.write_text('{"step": 1, "metrics": {"loss": 0.1}}\n', encoding="utf-8")
        marker.write_text("0", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        mod = types.ModuleType("orca.chart")

        def boom(**kw):
            raise RuntimeError("sock down")

        mod.render_chart = boom
        monkeypatch.setitem(sys.modules, "orca.chart", mod)
        sys.argv = ["watcher", "--progress", str(progress), "--done-marker", str(marker),
                    "--label", "x", "--title", "t", "--poll", "0.01",
                    "--max-idle", "60", "--max-wait", "0.05"]
        assert _NS["main"]() == 0
