"""tests/events/test_tape.py —— Tape append-only JSONL 持久化（唯一真相源）。

覆盖 SPEC §6.2：append 写行+flush / seq 单调（写时分配）/ seq==文件行序（Lock 覆盖
seq+write+flush 整体）/ replay 顺序一致 / 末尾残行容忍 / resume 清残行（截断+warning）
/ json_safe（bytes/Path/未知类型）。

注：本仓库 dev 依赖仅 pytest（无 pytest-asyncio），异步测试统一用 ``asyncio.run``，
保持零新增依赖（SPEC §6.9「保持最小」）。

测试覆盖意图（非仅行为）：验证「seq 序 == 文件行序」这一并发不变量；验证 resume
不接坏行（截断至最后有效行）；验证残行不抛（fail-soft 读）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from orca.events.tape import Tape, _json_safe


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_tape(tmp_path: Path, run_id: str = "r1", **kw) -> Tape:
    return Tape(tmp_path / "events.jsonl", run_id=run_id, **kw)


def _event_data(type: str = "node_started", **payload) -> dict:
    """构造不含 seq 的 event 字段 dict（type/timestamp/node/session_id/data）。

    额外 kwargs 进 data（``**payload`` 避免与字段名 ``data`` 冲突）。
    """
    return {
        "type": type,
        "timestamp": 1.0,
        "node": "n",
        "session_id": "s1",
        "data": payload,
    }


def _run(coro):
    """跑一个 async 测试体（替代 pytest-asyncio）。"""
    return asyncio.run(coro)


# ── append / replay / seq 单调 ───────────────────────────────────────────────


def test_append_returns_monotonic_seq(tmp_path):
    tape = _make_tape(tmp_path)
    try:
        s1 = _run(tape.append(_event_data()))
        s2 = _run(tape.append(_event_data()))
        s3 = _run(tape.append(_event_data()))
        assert (s1, s2, s3) == (1, 2, 3)
        assert tape.last_seq() == 3
    finally:
        tape.close()


def test_append_writes_one_line_and_flush(tmp_path):
    path = tmp_path / "events.jsonl"
    tape = _make_tape(tmp_path)
    try:
        _run(tape.append(_event_data(text="hello")))
        # flush 后立即读得到（不依赖 close）
        text = path.read_text(encoding="utf-8")
        assert text.count("\n") == 1
        obj = json.loads(text.strip())
        assert obj["seq"] == 1
        assert obj["data"]["text"] == "hello"
    finally:
        tape.close()


def test_replay_preserves_order(tmp_path):
    tape = _make_tape(tmp_path)
    try:
        for i in range(5):
            _run(tape.append(_event_data(idx=i)))
    finally:
        tape.close()

    tape2 = _make_tape(tmp_path)  # 文件已存在，非 resume 续写
    try:
        events = list(tape2.replay())
        assert [e.seq for e in events] == [1, 2, 3, 4, 5]
        assert [e.data["idx"] for e in events] == [0, 1, 2, 3, 4]
    finally:
        tape2.close()


def test_replay_since_seq(tmp_path):
    tape = _make_tape(tmp_path)
    try:
        for _ in range(5):
            _run(tape.append(_event_data()))
        events = list(tape.replay(since_seq=3))
        assert [e.seq for e in events] == [4, 5]
    finally:
        tape.close()


# ── seq 序 == 文件行序（并发 append，Lock 覆盖整体）──────────────────────────


def test_concurrent_append_seq_equals_line_order(tmp_path):
    """并发 append：Lock 覆盖「seq 分配 + write + flush」整体 → seq 序 == 文件行序。

    这是 SPEC §6.2 / §11 决策 3 的核心不变量。若 Lock 未覆盖整体，并发下 seq 序与
    落盘行序可能错位（先分配 seq 的任务后被 flush）。
    """
    tape = _make_tape(tmp_path)
    try:
        # 50 个并发 append，每个前加随机量 perturbation delay 强制调度交错。
        # 关键不变量不只是「seq 集合 == 1..N」，而是「**每行都是合法独立 JSON**」——
        # 若 Lock 未覆盖 write+flush 整体，并发 write 会在同一行产生两个 JSON 拼接
        # （坏行）。故本测试同时校验「无坏行」+「seq==行序」。
        async def _append_with_delay(i):
            await asyncio.sleep((50 - i) * 0.0001)  # i=0 睡最久，最后到锁
            await tape.append(_event_data(i=i))

        async def _burst():
            await asyncio.gather(*[_append_with_delay(i) for i in range(50)])

        _run(_burst())
    finally:
        tape.close()

    # 读回文件：每行必须独立 parse 为合法 JSON（无并发写交错产生的坏行），
    # 且 seq == 行序（1..50）。
    raw = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    lines = [ln for ln in raw.split("\n") if ln.strip()]
    assert len(lines) == 50, f"应有 50 行，实际 {len(lines)}（可能有坏行合并）"
    seqs = []
    for idx, line in enumerate(lines):
        obj = json.loads(line)  # 若 Lock 范围错，这里可能抛（两 JSON 拼接）
        seqs.append(obj["seq"])
    assert seqs == list(range(1, 51)), "seq 序必须 == 文件行序"
    # 同时 replay 也保序
    tape2 = _make_tape(tmp_path)
    try:
        assert [e.seq for e in tape2.replay()] == list(range(1, 51))
    finally:
        tape2.close()


def test_invalid_event_does_not_create_seq_gap(tmp_path):
    """坏事件（非法 type）被拒后不留 seq 间隙（SPEC §3.2/§11 决策3）。

    valid(seq=1) → invalid(拒，不分配 seq) → valid(seq=2，非 3)。
    保证「seq 序 == 文件行序」：坏事件不占 seq。
    """
    import pytest
    from pydantic import ValidationError

    tape = _make_tape(tmp_path)
    try:
        s1 = _run(tape.append(_event_data()))  # seq=1
        with pytest.raises((ValidationError, ValueError)):
            _run(tape.append({**_event_data(), "type": "bogus"}))  # 拒
        s3 = _run(tape.append(_event_data()))  # seq=2（非 3）
        assert (s1, s3) == (1, 2)
        assert tape.last_seq() == 2
        # 文件只有 2 行（坏事件未落盘）
        lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
    finally:
        tape.close()


# ── 残行容忍（fail-soft 读）──────────────────────────────────────────────────


def test_replay_tolerates_trailing_partial_line(tmp_path):
    """末尾残行（崩溃截断）被跳过，不抛异常（SPEC §6.2 残行容忍）。"""
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps({"seq": 1, "type": "node_started", "timestamp": 1.0,
                    "node": "a", "session_id": None, "data": {}}) + "\n"
        + json.dumps({"seq": 2, "type": "node_completed", "timestamp": 1.0,
                      "node": "a", "session_id": None, "data": {}}) + "\n"
        + '{"seq": 3, "type": "node_started", "timestamp": 1.0, "node":',  # 残行
        encoding="utf-8",
    )
    tape = _make_tape(tmp_path)
    try:
        events = list(tape.replay())
        assert [e.seq for e in events] == [1, 2]  # 残行被跳过
    finally:
        tape.close()


def test_replay_tolerates_middle_partial_line(tmp_path, caplog):
    """中间残行（异常情况）记 warning 跳过，不阻断后续有效行。"""
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps({"seq": 1, "type": "node_started", "timestamp": 1.0,
                    "node": "a", "session_id": None, "data": {}}) + "\n"
        + "NOT JSON\n"
        + json.dumps({"seq": 3, "type": "node_completed", "timestamp": 1.0,
                      "node": "a", "session_id": None, "data": {}}) + "\n",
        encoding="utf-8",
    )
    tape = _make_tape(tmp_path)
    try:
        with caplog.at_level(logging.WARNING):
            events = list(tape.replay())
        assert [e.seq for e in events] == [1, 3]
        assert any("不是合法 JSON" in r.message for r in caplog.records)
    finally:
        tape.close()


# ── resume 清残行（截断 + warning）───────────────────────────────────────────


def test_resume_truncates_trailing_partial_and_continues(tmp_path, caplog):
    """resume：崩溃残行被截断（不接坏行），新事件从 last_seq+1 续写（SPEC §3.2/§6.2）。

    反模式：把新行接到残行后面 → 产生坏行。本测试确保截断至最后有效行。
    """
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps({"seq": 1, "type": "node_started", "timestamp": 1.0,
                    "node": "a", "session_id": None, "data": {}}) + "\n"
        + json.dumps({"seq": 2, "type": "node_completed", "timestamp": 1.0,
                      "node": "a", "session_id": None, "data": {}}) + "\n"
        + '{"seq": 3, "type": "node_started", "timestamp": 1.0, "node":',  # 残行
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        tape = Tape(path, run_id="r1", resume=True)
        try:
            assert tape.last_seq() == 2  # 残行不计入，从有效事件重算
            # 新事件从 last_seq+1 续写
            s3 = _run(tape.append(_event_data()))
            assert s3 == 3
        finally:
            tape.close()
    # 截断记了 warning（不静默）
    assert any("截断末尾" in r.message for r in caplog.records)

    # 文件应是 3 个完整行（残行被截断，新行接在最后有效行后）
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3
    assert all(json.loads(line)["seq"] in (1, 2, 3) for line in lines)


def test_resume_no_partial_when_clean(tmp_path):
    """resume 但末尾无残行：不截断，last_seq 正常，从 last_seq+1 续写。"""
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps({"seq": 1, "type": "node_started", "timestamp": 1.0,
                    "node": "a", "session_id": None, "data": {}}) + "\n"
        + json.dumps({"seq": 2, "type": "node_completed", "timestamp": 1.0,
                      "node": "a", "session_id": None, "data": {}}) + "\n",
        encoding="utf-8",
    )
    tape = Tape(path, run_id="r1", resume=True)
    try:
        assert tape.last_seq() == 2
        s3 = _run(tape.append(_event_data()))
        assert s3 == 3
    finally:
        tape.close()


def test_resume_all_partial_clears_file(tmp_path, caplog):
    """resume 但文件全是残行（无任何有效事件）：清空，从 seq=1 重新开始。"""
    path = tmp_path / "events.jsonl"
    path.write_text("GARBAGE NOT JSON\nALSO GARBAGE\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        tape = Tape(path, run_id="r1", resume=True)
        try:
            assert tape.last_seq() == 0
            s1 = _run(tape.append(_event_data()))
            assert s1 == 1
        finally:
            tape.close()
    assert any("未发现任何有效事件行" in r.message for r in caplog.records)


def test_resume_same_run_id_append_mode(tmp_path):
    """resume 同 run_id = 追加模式重开（SPEC §6.2 resume 验收）。"""
    path = tmp_path / "events.jsonl"
    tape = Tape(path, run_id="r1")
    try:
        _run(tape.append(_event_data()))
        _run(tape.append(_event_data()))
    finally:
        tape.close()

    tape2 = Tape(path, run_id="r1", resume=True)
    try:
        assert tape2.last_seq() == 2
        s3 = _run(tape2.append(_event_data()))
        assert s3 == 3
    finally:
        tape2.close()
    # 原有 2 行未动，新行追加
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3


def test_non_resume_reopen_with_partial_warns(tmp_path, caplog):
    """非 resume 重开但末尾有残行：记 warning（不截断，提醒用 resume，review M4 修复）。

    fail loud（SPEC §6.0 铁律4）：与 resume 路径同源的「坏行」风险须可见。
    非 resume 不截断（调用方未要求 crash recovery），但 warning 提醒。
    """
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps({"seq": 1, "type": "node_started", "timestamp": 1.0,
                    "node": "a", "session_id": None, "data": {}}) + "\n"
        + '{"seq": 2, "type": "node_started", "timestamp"',  # 残行
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        tape = Tape(path, run_id="r1")  # 非 resume
        try:
            assert tape.last_seq() == 1  # 残行不计入
        finally:
            tape.close()
    assert any("非 resume 重开但末尾存在不完整行" in r.message for r in caplog.records)


# ── json_safe ────────────────────────────────────────────────────────────────


def test_json_safe_bytes_path_unknown():
    """bytes/Path/未知类型经 _json_safe 转 JSON 安全形态（SPEC §6.2 json_safe）。"""
    assert _json_safe(b"hello") == "hello"
    assert _json_safe(Path("/tmp/x")) == "/tmp/x"
    assert _json_safe({"a": b"x", "p": Path("/y")}) == {"a": "x", "p": "/y"}
    assert _json_safe([b"a", Path("/b"), 1, "s"]) == ["a", "/b", 1, "s"]
    # 未知类型 → repr（lossy 但不丢）
    obj = object()
    assert _json_safe(obj) == repr(obj)
    # bytes 解码失败 → repr
    bad = b"\xff\xfe"
    assert _json_safe(bad) == repr(bad)


def test_append_json_safe_serializes_bytes_and_path(tmp_path):
    """append 含 bytes/Path 的 data：经 _json_safe 落盘为纯 JSON（不抛 TypeError）。"""
    tape = _make_tape(tmp_path)
    try:
        _run(
            tape.append(
                {"type": "custom", "timestamp": 1.0, "node": "n", "session_id": "s",
                 "data": {"blob": b"raw", "path": Path("/x/y"),
                          "nested": {"b": b"z"}}}
            )
        )
    finally:
        tape.close()
    line = (tmp_path / "events.jsonl").read_text(encoding="utf-8").strip()
    obj = json.loads(line)
    assert obj["data"]["blob"] == "raw"
    assert obj["data"]["path"] == "/x/y"
    assert obj["data"]["nested"]["b"] == "z"


# ── close 后 append fail loud ────────────────────────────────────────────────


def test_append_after_close_raises(tmp_path):
    import pytest

    tape = _make_tape(tmp_path)
    tape.close()
    with pytest.raises(RuntimeError, match="已 close"):
        _run(tape.append(_event_data()))
