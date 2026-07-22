"""test_conftest_cleanup.py —— recent_run_cleanup 的数据安全属性单测（Rule 9）。

cleanup fixture 做 ``shutil.rmtree``，其**核心意图**是「绝不删用户既有老 run」。本文件用 tmp
目录 + ``os.utime`` 伪造新/旧 mtime，钉死 mtime-backoff + exclude_ids 的安全属性——防
``_RECENCY_SEC`` 比较或 exclude 逻辑回归导致用户活跃 run 被误删。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tests.e2e_redesign import conftest as cfg


@pytest.fixture
def fake_runs_dir(tmp_path: Path, monkeypatch) -> Path:
    """把 conftest 的 RUNS_DIR + WORKFLOWS 指向 tmp，造可控的 run 目录。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(cfg, "RUNS_DIR", runs)
    monkeypatch.setattr(cfg, "WORKFLOWS", {"quant-ptq-sweep": "quant-ptq-sweep.yaml"})
    return runs


def _make_run(parent: Path, name: str, *, age_s: float) -> Path:
    """造一个 run 目录，mtime 设为 age_s 秒前。"""
    d = parent / name
    d.mkdir()
    d.mkdir(exist_ok=True)
    # mtime = now - age_s
    target = time.time() - age_s
    os.utime(d, (target, target))
    return d


def test_recent_run_dir_within_window_cleaned(fake_runs_dir: Path, monkeypatch) -> None:
    """近窗口（<600s）的 run 目录 → 被 rmtree。"""
    run = _make_run(fake_runs_dir, "quant-ptq-sweep-recent", age_s=60)
    # exclude_ids 空（无用户 run 需保护）
    monkeypatch.setattr(cfg, "_user_protected_run_ids", lambda: set())
    cfg._cleanup_recent(time.time() - cfg._RECENCY_SEC, exclude_ids=set())
    assert not run.exists(), "近窗口 run 目录应被清理"


def test_old_run_dir_outside_window_preserved(fake_runs_dir: Path, monkeypatch) -> None:
    """超窗口（>600s，如用户 2 天前的 kd-nas）的 run 目录 → **绝不删**。"""
    run = _make_run(fake_runs_dir, "quant-ptq-sweep-old", age_s=200000)  # ~2.3 天
    monkeypatch.setattr(cfg, "_user_protected_run_ids", lambda: set())
    cfg._cleanup_recent(time.time() - cfg._RECENCY_SEC, exclude_ids=set())
    assert run.exists(), "超窗口老 run 目录绝不能删（用户数据保护）"


def test_exclude_ids_protects_even_if_recent(fake_runs_dir: Path, monkeypatch) -> None:
    """exclude_ids 内的 run 即使近窗口也不删（双保险：status 查到的活跃 run）。"""
    run = _make_run(fake_runs_dir, "quant-ptq-sweep-active", age_s=10)
    monkeypatch.setattr(cfg, "_user_protected_run_ids",
                        lambda: {"quant-ptq-sweep-active"})
    cfg._cleanup_recent(time.time() - cfg._RECENCY_SEC,
                        exclude_ids={"quant-ptq-sweep-active"})
    assert run.exists(), "exclude_ids 内的 run 不应被删"


def test_wf_run_dirs_only_returns_dirs_not_files(fake_runs_dir: Path) -> None:
    """_wf_run_dirs 过滤掉 .jsonl / .jsonl.lock 等同名文件（rmtree 文件会 raise）。"""
    (fake_runs_dir / "quant-ptq-sweep-x.jsonl").write_text("{}", encoding="utf-8")
    (fake_runs_dir / "quant-ptq-sweep-x.jsonl.lock").write_text("{}", encoding="utf-8")
    d = fake_runs_dir / "quant-ptq-sweep-dir"
    d.mkdir()
    dirs = cfg._wf_run_dirs()
    assert d in dirs
    assert all(p.is_dir() for p in dirs), "只应返目录，不含 .jsonl 文件"


def test_mixed_recent_and_old_only_recent_cleaned(fake_runs_dir: Path, monkeypatch) -> None:
    """混合场景：近 run 删、老 run 留、exclude run 留——一次 cleanup 的正确分类。"""
    recent = _make_run(fake_runs_dir, "quant-ptq-sweep-new", age_s=30)
    old = _make_run(fake_runs_dir, "quant-ptq-sweep-2days", age_s=200000)
    excluded = _make_run(fake_runs_dir, "quant-ptq-sweep-protected", age_s=30)
    monkeypatch.setattr(cfg, "_user_protected_run_ids", set())
    cfg._cleanup_recent(time.time() - cfg._RECENCY_SEC,
                        exclude_ids={"quant-ptq-sweep-protected"})
    assert not recent.exists(), "近 run 应删"
    assert old.exists(), "老 run 应留"
    assert excluded.exists(), "exclude run 应留"
