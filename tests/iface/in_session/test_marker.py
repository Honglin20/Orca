"""tests/iface/in_session/test_marker.py —— 激活 marker 原子写 + 半写容忍 + run_id 查找。

覆盖 SPEC §5 / §2.4：
  - ``write_marker`` + ``read_marker`` 往返（round-trip）
  - 半写态容忍（损坏 JSON → None + warn，不崩；调用方 passthrough）
  - ``find_marker_by_run_id`` 线性扫描定位（opencode owner=sessionID 与 CC owner=run_id 都能查）
  - ``clear_marker`` 幂等
  - 字段缺失（旧版标记）→ None
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from orca.iface.in_session.marker import (
    ActivationMarker,
    clear_marker,
    find_marker_by_run_id,
    marker_path,
    read_marker,
    write_marker,
)


def _marker(run_id: str = "r1", owner: str = "owner1", **kw) -> ActivationMarker:
    return ActivationMarker(
        run_id=run_id,
        tape_path=f"/tmp/{run_id}.jsonl",
        yaml=f"/wf/{run_id}.yaml",
        owner=owner,
        model=kw.get("model", "deepseek/deepseek-v4-flash"),
        session_id=kw.get("session_id", "sess-x"),
        no_output_count=kw.get("no_output_count", 0),
    )


def test_marker_roundtrip(tmp_path):
    mp = marker_path(tmp_path, "owner1")
    m = _marker()
    write_marker(mp, m)
    out = read_marker(mp)
    assert out is not None
    assert out.run_id == m.run_id
    assert out.tape_path == m.tape_path
    assert out.yaml == m.yaml
    assert out.owner == m.owner
    assert out.no_output_count == 0


def test_read_marker_half_write_tolerant(tmp_path, caplog):
    """SPEC §2.4：半写态（不完整 JSON）→ 返 None + warn，不崩。"""
    mp = marker_path(tmp_path, "owner1")
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text('{"run_id":"r1","tape_path":', encoding="utf-8")  # 半写

    with caplog.at_level(logging.WARNING):
        out = read_marker(mp)
    assert out is None
    assert any("读失败" in rec.message for rec in caplog.records)


def test_read_marker_missing_field_tolerant(tmp_path, caplog):
    """旧版字段缺失 / 手改坏 → None + warn。"""
    mp = marker_path(tmp_path, "owner1")
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({"run_id": "r1"}), encoding="utf-8")  # 字段缺失

    with caplog.at_level(logging.WARNING):
        out = read_marker(mp)
    assert out is None


def test_read_marker_no_file_returns_none(tmp_path):
    mp = marker_path(tmp_path, "nope")
    assert read_marker(mp) is None


def test_find_marker_by_run_id_finds_opencode_owner(tmp_path):
    """opencode owner=sessionID ≠ run_id：扫描靠 run_id 字段。"""
    m = _marker(run_id="r-abc", owner="sessionID-xyz")
    write_marker(marker_path(tmp_path, "sessionID-xyz"), m)
    found = find_marker_by_run_id(tmp_path, "r-abc")
    assert found is not None
    assert read_marker(found).run_id == "r-abc"


def test_find_marker_by_run_id_finds_cc_owner(tmp_path):
    """CC owner=run_id（同 field 值）：扫描仍按 run_id 字段。"""
    m = _marker(run_id="r-xyz", owner="r-xyz")
    write_marker(marker_path(tmp_path, "r-xyz"), m)
    found = find_marker_by_run_id(tmp_path, "r-xyz")
    assert found is not None


def test_find_marker_by_run_id_returns_none_when_missing(tmp_path):
    assert find_marker_by_run_id(tmp_path, "nope") is None
    # 空目录
    assert find_marker_by_run_id(Path("/tmp/empty-orca-dir-xyz"), "nope") is None


def test_clear_marker_idempotent(tmp_path):
    mp = marker_path(tmp_path, "owner1")
    write_marker(mp, _marker())
    clear_marker(mp)
    assert not mp.exists()
    # 再次清不报错
    clear_marker(mp)


def test_no_output_count_increment_persists(tmp_path):
    """RMW：no_output_count 自增后 read 拿到新值（N2 测试支撑）。"""
    mp = marker_path(tmp_path, "owner1")
    m = _marker(no_output_count=0)
    write_marker(mp, m)
    # RMW
    read_back = read_marker(mp)
    read_back.no_output_count += 1
    write_marker(mp, read_back)
    final = read_marker(mp)
    assert final.no_output_count == 1
