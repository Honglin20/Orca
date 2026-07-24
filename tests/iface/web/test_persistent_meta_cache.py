"""test_persistent_meta_cache.py —— SPEC §13.3 P0 持久派生缓存单测。

覆盖：
  - 命中：二次 scan 同 tape 走持久缓存（kill 进程后 in-memory 失效也能命中）。
  - 失配重建：mtime / size 变 → 重算 + 写回新 entry。
  - 损坏回退：JSON 非法 → 视为空 + 重建（warn 不崩）。
  - 写回：cache 文件落盘 + 结构合法。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from orca.iface.web.run_manager import RunManager


def _make_tape(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )


def _wf_started(seq: int = 1, run_id: str = "r1") -> dict:
    return {
        "seq": seq,
        "type": "workflow_started",
        "node": None,
        "session_id": None,
        "timestamp": 0.0,
        "data": {
            "inputs": {},
            "node_count": 1,
            "entry": "n1",
            "workflow_name": "wf",
            "topology": {"nodes": [{"name": "n1"}]},
            "run_id": run_id,
        },
    }


def test_persistent_cache_hit_after_inmemory_clear(tmp_path):
    """in-memory 失效后（模拟新进程），持久层命中。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    tape = runs_dir / "r1.jsonl"
    _make_tape(tape, [_wf_started()])

    mgr = RunManager(runs_dir=runs_dir)
    r1 = mgr._scan_meta_overview_cached(tape)
    assert r1[0] == 1  # count

    # 清 in-memory 模拟新进程（持久层仍在）。
    mgr._meta_cache.clear()
    r2 = mgr._scan_meta_overview_cached(tape)
    assert r2 == r1
    # 持久 cache 文件已落盘
    cache_file = runs_dir / ".orca-meta-cache.json"
    assert cache_file.is_file()
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert "r1.jsonl" in data["entries"]


def test_persistent_cache_miss_when_mtime_changes(tmp_path):
    """mtime 变 → 失配 → 重算 + 写回新 entry。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    tape = runs_dir / "r1.jsonl"
    _make_tape(tape, [_wf_started()])

    mgr = RunManager(runs_dir=runs_dir)
    r1 = mgr._scan_meta_overview_cached(tape)
    assert r1[0] == 1

    # 改 tape（追加事件）+ 显式更新 mtime
    _make_tape(
        tape,
        [
            _wf_started(),
            {
                "seq": 2,
                "type": "workflow_completed",
                "node": None,
                "session_id": None,
                "timestamp": 1.0,
                "data": {"elapsed": 1.0, "outputs": {}},
            },
        ],
    )
    # 强制 mtime 变（部分 fs 精度不足）。
    new_mtime = time.time() + 5
    import os as _os
    _os.utime(tape, (new_mtime, new_mtime))

    mgr._meta_cache.clear()
    mgr._persistent_cache_by_runs_dir.clear()
    r2 = mgr._scan_meta_overview_cached(tape)
    assert r2[0] == 2  # 新 count
    # overview run_status 应该是 completed
    assert r2[3]["overview"]["run_status"] == "completed"


def test_persistent_cache_corrupt_recovers(tmp_path):
    """持久 cache 文件损坏 → warn + 视为空 → 重建。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    tape = runs_dir / "r1.jsonl"
    _make_tape(tape, [_wf_started()])

    # 写一份损坏 cache。
    cache_file = runs_dir / ".orca-meta-cache.json"
    cache_file.write_text("{NOT_JSON", encoding="utf-8")

    mgr = RunManager(runs_dir=runs_dir)
    # 第一查：触发 load（warn）+ 重算 + 写回合法 cache。
    r1 = mgr._scan_meta_overview_cached(tape)
    assert r1[0] == 1
    # cache 文件已被覆盖为合法 JSON
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert "r1.jsonl" in data["entries"]


def test_persistent_cache_size_mismatch_invalidates(tmp_path):
    """size 变（未变 mtime）→ 失配重算。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    tape = runs_dir / "r1.jsonl"
    _make_tape(tape, [_wf_started()])

    mgr = RunManager(runs_dir=runs_dir)
    mgr._scan_meta_overview_cached(tape)

    # 篡改持久 cache 的 size 字段（让失配触发）。
    mgr._persistent_cache_by_runs_dir.clear()
    cache_file = runs_dir / ".orca-meta-cache.json"
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    data["entries"]["r1.jsonl"]["size"] = 999999
    cache_file.write_text(json.dumps(data), encoding="utf-8")

    mgr._meta_cache.clear()
    r = mgr._scan_meta_overview_cached(tape)
    assert r[0] == 1  # 重算得到正确 count
