"""tests/events/test_tape_append_batch.py —— Tape.append_batch 单次 write 原子化（B1）。

覆盖 SPEC §6 / ADR v3 I2：
  - batch 返连续 seq（与逐条 append 一致）
  - 单次 ``_fh.write`` + 单次 ``flush``（grep/mock 实证，B1 闭环）
  - 坏事件 → 全批 fail loud，**不分配 seq、不落字节**（无 seq 间隙、无悬空态）
  - 落盘行格式与逐条 ``append`` 一致（每行一个完整 JSON Event）
  - 空 list 返空（no-op）
  - SIGKILL 等价模拟：mock write 抛 → tape 保持 0 行（无 1-2 条悬空）
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from orca.events.tape import Tape
from orca.schema import Event


def _run(coro):
    return asyncio.run(coro)


def _event_data(type: str = "node_started", **payload) -> dict:
    return {
        "type": type,
        "timestamp": 1.0,
        "node": payload.pop("_node", "n"),
        "session_id": "s1",
        "data": payload,
    }


def _make_tape(tmp_path: Path, run_id: str = "r1", **kw) -> Tape:
    return Tape(tmp_path / "events.jsonl", run_id=run_id, **kw)


# ── seq 连续 / 单次 write 原子化 ─────────────────────────────────────────────


def test_append_batch_returns_continuous_seq(tmp_path):
    tape = _make_tape(tmp_path)
    try:
        seqs = _run(tape.append_batch([
            _event_data("node_started", x=1),
            _event_data("route_taken", x=2),
            _event_data("node_completed", x=3),
        ]))
        assert seqs == [1, 2, 3]
        assert tape.last_seq() == 3
    finally:
        tape.close()


def test_append_batch_continues_after_append(tmp_path):
    """逐条 append + append_batch 混用，seq 连续。"""
    tape = _make_tape(tmp_path)
    try:
        s1 = _run(tape.append(_event_data("node_started", x=1)))
        seqs = _run(tape.append_batch([
            _event_data("route_taken", x=2),
            _event_data("node_completed", x=3),
        ]))
        assert s1 == 1
        assert seqs == [2, 3]
        assert tape.last_seq() == 3
    finally:
        tape.close()


def test_append_batch_writes_all_lines_in_single_write_call(tmp_path):
    """B1 闭环：整批一次 ``_fh.write``（mock 实证）。"""
    tape = _make_tape(tmp_path)
    try:
        # 强制打开 _fh（让 mock 命中真实句柄）
        _run(tape.append(_event_data("workflow_started", _node=None)))  # 占位
        with patch.object(tape._fh, "write", wraps=tape._fh.write) as mock_write:
            with patch.object(tape._fh, "flush", wraps=tape._fh.flush) as mock_flush:
                _run(tape.append_batch([
                    _event_data("node_started", x=1),
                    _event_data("route_taken", x=2),
                    _event_data("node_completed", x=3),
                ]))
        # 整批一次 write（不是 3 次）+ 一次 flush
        assert mock_write.call_count == 1
        assert mock_flush.call_count == 1
        written = mock_write.call_args[0][0]
        # 一次 write 含 3 行（最后有 \n）
        assert written.count("\n") == 3
    finally:
        tape.close()


def test_append_batch_line_format_matches_append(tmp_path):
    """append_batch 落盘行与逐条 append 格式一致（每行一个完整 JSON Event）。"""
    path = tmp_path / "events.jsonl"
    tape = _make_tape(tmp_path)
    try:
        _run(tape.append(_event_data("node_started", x=1)))
        _run(tape.append_batch([
            _event_data("route_taken", x=2),
            _event_data("node_completed", x=3),
        ]))
    finally:
        tape.close()
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3
    for ln in lines:
        obj = json.loads(ln)
        Event(**obj)  # 每行是合法 Event
    assert json.loads(lines[0])["type"] == "node_started"
    assert json.loads(lines[1])["type"] == "route_taken"
    assert json.loads(lines[2])["type"] == "node_completed"


# ── 坏事件 fail loud / 无 seq 间隙 ─────────────────────────────────────────


def test_append_batch_bad_event_no_partial_write(tmp_path):
    """整批任一事件非法 → 全批不落盘，last_seq 不变（无 seq 间隙）。"""
    path = tmp_path / "events.jsonl"
    tape = _make_tape(tmp_path)
    try:
        bad = _event_data("not_a_real_event_type")  # 非 EventType
        with pytest.raises(Exception):  # pydantic ValidationError
            _run(tape.append_batch([
                _event_data("node_started", x=1),
                bad,
                _event_data("node_completed", x=3),
            ]))
        # 坏事件不留任何字节
        assert tape.last_seq() == 0
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        assert text == "" or text.strip() == ""
    finally:
        tape.close()


def test_append_batch_empty_list_is_noop(tmp_path):
    tape = _make_tape(tmp_path)
    try:
        seqs = _run(tape.append_batch([]))
        assert seqs == []
        assert tape.last_seq() == 0
    finally:
        tape.close()


def test_append_batch_after_close_raises(tmp_path):
    tape = _make_tape(tmp_path)
    tape.close()
    with pytest.raises(RuntimeError):
        _run(tape.append_batch([_event_data()]))


# ── SIGKILL 等价：write 抛 → 不留悬空 ────────────────────────────────────────


def test_append_batch_atomic_on_write_failure(tmp_path):
    """模拟 SIGKILL 中断 write：tape 保持先前状态，无 1-2 条悬空（B1 反例 B 闭环）。

    注：现实中本地 FS 小 write 实践上原子（SPEC §6 POSIX 措辞订正）。本测试模拟
    「write 抛异常」的等价场景（如磁盘满 / 进程在 write 前 SIGKILL），断言不留悬空。
    """
    path = tmp_path / "events.jsonl"
    tape = _make_tape(tmp_path)
    try:
        # 先成功落一条
        _run(tape.append(_event_data("node_started", x=1)))
        assert tape.last_seq() == 1

        # batch 的 write 抛 IOError（模拟中断）
        original_write = tape._fh.write
        def boom(data):
            raise IOError("simulated signal/IO failure")
        tape._fh.write = boom
        try:
            with pytest.raises(IOError):
                _run(tape.append_batch([
                    _event_data("route_taken", x=2),
                    _event_data("node_completed", x=3),
                ]))
        finally:
            tape._fh.write = original_write

        # tape 仍是 1 条（不增加悬空行）；last_seq 不变
        text = path.read_text(encoding="utf-8")
        assert text.count("\n") == 1
        assert tape.last_seq() == 1
    finally:
        tape.close()
