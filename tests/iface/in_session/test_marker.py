"""tests/iface/in_session/test_marker.py —— v3 §7.2 精简 marker 测试。

覆盖 SPEC v3 §7.2（m11 精简）：
  - ``ActivationMarker`` 只 3 字段（``run_id`` / ``model`` / ``no_output_count``）。
  - ``write_marker`` + ``read_marker`` 往返（round-trip）。
  - 半写态容忍（损坏 JSON → None + warn，不崩；调用方 passthrough）。
  - 旧版 marker（含已删的 tape_path/yaml/owner/session_id 残留）→ 容忍，按 3 字段读。
  - ``marker_path(rundir, run_id)`` O(1) 定位（文件名固定 ``orca-<run_id>.json``）。
  - ``clear_marker`` 幂等。
  - ``no_output_count`` RMW 持久化（N2 测试支撑）。

删 ``find_marker_by_run_id`` 扫描（v3 §7.2 改 O(1) ``marker_path`` 直定位）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from orca.iface.in_session.marker import (
    ActivationMarker,
    clear_marker,
    marker_path,
    read_marker,
    write_marker,
)


def _marker(run_id: str = "r1", **kw) -> ActivationMarker:
    return ActivationMarker(
        run_id=run_id,
        model=kw.get("model", "deepseek/deepseek-v4-flash"),
        no_output_count=kw.get("no_output_count", 0),
    )


def test_marker_only_three_fields():
    """v3 §7.2：ActivationMarker 只 3 字段（run_id/model/no_output_count）。"""
    m = _marker()
    # dataclass 字段集恰好 = 这 3 个（无 tape_path/yaml/owner/session_id）。
    assert set(m.__dataclass_fields__.keys()) == {"run_id", "model", "no_output_count"}


def test_marker_roundtrip(tmp_path):
    mp = marker_path(tmp_path, "r1")
    m = _marker()
    write_marker(mp, m)
    out = read_marker(mp)
    assert out is not None
    assert out.run_id == m.run_id
    assert out.model == m.model
    assert out.no_output_count == 0


def test_marker_path_fixed_filename_o1(tmp_path):
    """v3 §7.2：文件名固定 ``orca-<run_id>.json``，``next``/``stop`` O(1) 直定位。"""
    mp = marker_path(tmp_path, "r-abc-123456")
    assert mp.name == "orca-r-abc-123456.json"
    assert mp.parent == tmp_path


def test_read_marker_half_write_tolerant(tmp_path, caplog):
    """SPEC §2.4：半写态（不完整 JSON）→ 返 None + warn，不崩。"""
    mp = marker_path(tmp_path, "r1")
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text('{"run_id":"r1","model":', encoding="utf-8")  # 半写

    with caplog.at_level(logging.WARNING):
        out = read_marker(mp)
    assert out is None
    assert any("读失败" in rec.message for rec in caplog.records)


def test_read_marker_missing_run_id_returns_none(tmp_path, caplog):
    """缺 run_id（核心字段）→ None + warn（无法标识 run）。"""
    mp = marker_path(tmp_path, "r1")
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({"model": "x", "no_output_count": 0}), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        out = read_marker(mp)
    assert out is None


def test_read_marker_legacy_fields_tolerated(tmp_path):
    """v3 §7.2：旧版 marker 含已删字段（tape_path/yaml/owner/session_id）→ 容忍，按 3 字段读。

    防止「精简后旧 marker 全部读不了」——read_marker 只取已知 3 字段，忽略历史残留。
    """
    mp = marker_path(tmp_path, "r1")
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({
        "run_id": "r1",
        "tape_path": "/legacy/tape.jsonl",   # 已删字段
        "yaml": "/legacy/wf.yaml",            # 已删字段
        "owner": "r1",                        # 已删字段
        "session_id": "sess-x",               # 已删字段
        "model": "deepseek/v4",
        "no_output_count": 2,
    }), encoding="utf-8")

    out = read_marker(mp)
    assert out is not None
    assert out.run_id == "r1"
    assert out.model == "deepseek/v4"
    assert out.no_output_count == 2


def test_read_marker_no_file_returns_none(tmp_path):
    mp = marker_path(tmp_path, "nope")
    assert read_marker(mp) is None


def test_clear_marker_idempotent(tmp_path):
    mp = marker_path(tmp_path, "r1")
    write_marker(mp, _marker())
    clear_marker(mp)
    assert not mp.exists()
    # 再次清不报错
    clear_marker(mp)


def test_no_output_count_increment_persists(tmp_path):
    """RMW：no_output_count 自增后 read 拿到新值（N2 测试支撑）。"""
    mp = marker_path(tmp_path, "r1")
    m = _marker(no_output_count=0)
    write_marker(mp, m)
    # RMW
    read_back = read_marker(mp)
    assert read_back is not None
    read_back.no_output_count += 1
    write_marker(mp, read_back)
    final = read_marker(mp)
    assert final is not None
    assert final.no_output_count == 1
