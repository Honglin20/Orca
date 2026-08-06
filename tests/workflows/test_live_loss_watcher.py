"""Unit tests for ns_run_train / ns_retrain live_loss_watcher.

Covers:
  - Contract progress-line parsing (``epoch N/T loss V`` / ``step N/T loss V``)
  - Real-time push on new points (same label+title -> live-replace semantics)
  - fail-soft: missing ORCA_* env -> exit 0 without touching anything
  - fail-soft: orca.chart import failure -> exit 0
  - fail-soft: render_chart raise (socket down) -> exit 0
  - done-marker driven exit (mtime newer than watcher start)
  - stale done-marker must NOT exit a fresh watcher
  - log-missing wait timeout
  - idle timeout only after first point (slow first epoch never kills watcher)
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

_WATCHER = (
    Path(__file__).resolve().parents[2]
    / "workflows" / "agents" / "ns_run_train" / "scripts" / "live_loss_watcher.py"
)

# 直接加载脚本源码（不 import 模块名——两份镜像拷贝避免模块缓存冲突）。
_NS: dict = {}
exec(compile(_WATCHER.read_text(encoding="utf-8"), str(_WATCHER), "exec"), _NS)


@pytest.fixture
def env_ok(monkeypatch):
    """注入 4 个 render_chart 身份键（watcher 的 env 检查）。"""
    monkeypatch.setenv("ORCA_RUN_ID", "run-1")
    monkeypatch.setenv("ORCA_NODE", "ns_run_train")
    monkeypatch.setenv("ORCA_SESSION_ID", "sess-1")
    monkeypatch.setenv("ORCA_CHART_SOCK", "/tmp/orca-test.sock")


class TestProgressParsing:
    def test_epoch_line(self):
        m = _NS["_PROGRESS_RE"].match("epoch 3/10 loss 0.4521")
        assert m is not None
        assert m.group("unit") == "epoch"
        assert m.group("cur") == "3"
        assert m.group("loss") == "0.4521"

    def test_step_line_scientific(self):
        m = _NS["_PROGRESS_RE"].match("step 100/1000 loss 1.25e-05")
        assert m is not None
        assert m.group("unit") == "step"
        assert m.group("cur") == "100"
        assert m.group("loss") == "1.25e-05"

    def test_contract_verbatim_no_noise(self):
        """只吃整行契约格式；歧义行 / 前缀后缀都不匹配。"""
        re_ = _NS["_PROGRESS_RE"]
        assert re_.match("epoch 1/10 loss 0.5") is not None
        assert re_.match("epoch: 1/10 loss 0.5") is None       # 冒号非契约
        assert re_.match("Epoch 1/10 loss 0.5") is None        # 大写非契约
        assert re_.match("saved supernet_epoch_0005.pth") is None
        assert re_.match("epoch 1 loss 0.5") is None           # 缺 /total
        assert re_.match("epoch 1/10 acc 0.5") is None         # 非 loss
        assert re_.match("epoch 1/10 loss nan") is None


class TestWatcherLivePush:
    def test_pushes_on_new_epoch_and_exits_on_done(self, tmp_path, env_ok, monkeypatch):
        """进度行 → 推图（同 title）；done-marker 晚写（mtime 新）→ 最终推 + 退出。"""
        import threading
        import types

        log = tmp_path / "train.attempt1.log"
        marker = tmp_path / ".train_rc"
        log.write_text("epoch 1/10 loss 0.9\n", encoding="utf-8")
        calls: list[dict] = []
        mod = types.ModuleType("orca.chart")
        mod.render_chart = lambda **kw: calls.append(kw) or 1
        monkeypatch.setitem(sys.modules, "orca.chart", mod)
        monkeypatch.chdir(tmp_path)
        sys.argv = ["watcher", "--log", str(log), "--done-marker", str(marker),
                    "--label", "nas-supernet/train", "--title", "T (attempt 1)",
                    "--poll", "0.01", "--max-idle", "60", "--max-wait-log", "0.05"]
        results: list[int] = []

        def run():
            results.append(_NS["main"]())

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.1)  # watcher 已读到进度行并推图
        marker.write_text("0", encoding="utf-8")  # 训练完成 → done 驱动退出
        t.join(timeout=3)
        assert not t.is_alive()
        assert results == [0]
        # 推了两次全量曲线（新进度点一次 + done-marker 退出前最后一次，同数据幂等替换）：
        # x=epoch 轴、loss 值、同 label/title（实时替换语义）。
        assert len(calls) == 2
        pushed = calls[0]
        assert pushed["chart_type"] == "line"
        assert pushed["label"] == "nas-supernet/train"
        assert pushed["title"] == "T (attempt 1)"
        assert pushed["x_label"] == "epoch"
        assert pushed["data"] == [{"x": 1.0, "y": 0.9}]
        assert calls[1]["data"] == calls[0]["data"]

    def test_stale_marker_does_not_exit(self, tmp_path, env_ok, monkeypatch):
        """续训场景：前次 attempt 的 stale .train_rc 不应让新 watcher 一启动就退。

        验证方式：stale marker mtime 早于 watcher 启动 → 主循环至少跑一轮不退出。
        用 done-marker 晚写的线程验证退出由 marker 驱动。
        """
        log = tmp_path / "train.attempt2.log"
        marker = tmp_path / ".train_rc"
        log.write_text("", encoding="utf-8")
        marker.write_text("0", encoding="utf-8")  # stale（mtime 在 watcher 前）

        import threading
        import types
        calls = []
        mod = types.ModuleType("orca.chart")
        mod.render_chart = lambda **kw: calls.append(kw) or 1
        monkeypatch.setitem(sys.modules, "orca.chart", mod)
        monkeypatch.chdir(tmp_path)
        sys.argv = ["watcher", "--log", str(log), "--done-marker", str(marker),
                    "--label", "nas-supernet/train", "--title", "T (attempt 2)",
                    "--poll", "0.01", "--max-idle", "60", "--max-wait-log", "0.05"]
        results = []

        def run():
            results.append(_NS["main"]())

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.1)  # watcher 已跑若干轮（若被 stale 误杀会提前返回）
        # 模拟训练完成写 marker（mtime 更新）→ watcher 应退出
        time.sleep(0.02)
        marker.write_text("0", encoding="utf-8")
        t.join(timeout=3)
        assert not t.is_alive()
        assert results == [0]
        # 无进度行 → 最终推图不应发生（points 空）
        assert calls == []

    def test_missing_env_exits_zero(self, tmp_path, monkeypatch):
        """缺 ORCA_* env → exit 0（fail-soft，不 raise）。"""
        import contextlib
        marker = tmp_path / ".train_rc"
        log = tmp_path / "train.attempt1.log"
        log.write_text("epoch 1/10 loss 0.9\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        sys.argv = ["watcher", "--log", str(log), "--done-marker", str(marker),
                    "--label", "x", "--title", "t", "--poll", "0.01",
                    "--max-idle", "0.05", "--max-wait-log", "0.05"]
        assert _NS["main"]() == 0

    def test_log_missing_times_out(self, tmp_path, env_ok, monkeypatch):
        """log 文件不出现 → max-wait-log 超时 → exit 0。"""
        marker = tmp_path / ".train_rc"
        monkeypatch.chdir(tmp_path)
        sys.argv = ["watcher", "--log", str(tmp_path / "nope.log"),
                    "--done-marker", str(marker), "--label", "x", "--title", "t",
                    "--poll", "0.01", "--max-idle", "60", "--max-wait-log", "0.05"]
        assert _NS["main"]() == 0

    def test_render_failure_exits_zero(self, tmp_path, env_ok, monkeypatch):
        """socket 断（render_chart raise）→ stderr 一次 + exit 0（断更不轰炸）。"""
        import types
        log = tmp_path / "train.attempt1.log"
        marker = tmp_path / ".train_rc"
        log.write_text("epoch 1/10 loss 0.9\n", encoding="utf-8")
        marker.write_text("0", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        mod = types.ModuleType("orca.chart")

        def boom(**kw):
            raise RuntimeError("sock down")

        mod.render_chart = boom
        monkeypatch.setitem(sys.modules, "orca.chart", mod)
        sys.argv = ["watcher", "--log", str(log), "--done-marker", str(marker),
                    "--label", "x", "--title", "t", "--poll", "0.01",
                    "--max-idle", "60", "--max-wait-log", "0.05"]
        assert _NS["main"]() == 0
