"""tests/events/test_adapters_opencode_sqlite.py —— opencode sqlite adapter（SPEC-B v4 §5）。

覆盖意图（contract 锁，无 live spike；fixture DB 驱动）：
  - discover_children：扫父 event 流的 task tool part（completed）→ 提 metadata.sessionId。
  - discover_children fallback：session.parent_id 查询（主路径无 task tool 时）。
  - stream：event 表 seq 游标 → message.part.updated.1 行 → 按 part.type 映射。
  - 映射：reasoning / text / tool(running→call) / tool(completed→result) / step-start / step-finish skip。
  - source_id 用 ``opc:<seq>``（保 tool running/completed 双 event 唯一）。
  - cursor：seq 游标续读。
  - scope：host_session 不匹配 → 空。
  - fail loud：host_session 空 → raise。
  - DB 不存在 → discover/stream 返空。
  - readonly + WAL-safe 连接（mode=ro）。
  - 单 row data 损坏 → skip。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from orca.events.adapters.opencode_sqlite import (
    OpencodeAdapterError,
    OpencodeSqliteAdapter,
)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """构造 opencode schema sqlite DB（event + session 表 + session_parent_idx 索引）。

    schema 简化但与 opencode v1.14+ 实证兼容：
      - ``event(aggregate_id, seq, type, data)`` —— ``aggregate_id`` 是 session id，
        ``seq`` 连续整数；``data`` 是 JSON 串，含 ``part`` 内联。
      - ``session(id, parent_id)`` —— ``parent_id`` 指向宿主 session（fallback 用）。
    """
    db = tmp_path / "session.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE event ("
        "  aggregate_id TEXT NOT NULL,"
        "  seq INTEGER NOT NULL,"
        "  type TEXT NOT NULL,"
        "  data TEXT NOT NULL,"
        "  PRIMARY KEY (aggregate_id, seq)"
        ")"
    )
    conn.execute("CREATE INDEX aggregate_seq_idx ON event(aggregate_id, seq)")
    conn.execute(
        "CREATE TABLE session ("
        "  id TEXT PRIMARY KEY,"
        "  parent_id TEXT"
        ")"
    )
    conn.execute("CREATE INDEX session_parent_idx ON session(parent_id)")
    conn.commit()
    conn.close()
    return db


def _insert_event(conn: sqlite3.Connection, aggregate_id: str, seq: int,
                  data: dict, event_type: str = "message.part.updated.1") -> None:
    """插一行 event。"""
    conn.execute(
        "INSERT INTO event (aggregate_id, seq, type, data) VALUES (?, ?, ?, ?)",
        (aggregate_id, seq, event_type, json.dumps(data)),
    )


def _part(part_type: str, **kw) -> dict:
    """构造 event.data.part JSON。"""
    d = {"type": part_type}
    d.update(kw)
    return d


# ── fail loud ─────────────────────────────────────────────────────────────────


def test_construct_raises_on_empty_host_session(db_path):
    with pytest.raises(OpencodeAdapterError, match="host_session"):
        OpencodeSqliteAdapter("", db_path=db_path)


def test_default_db_path_resolution(monkeypatch, tmp_path):
    """无 db_path 参数 + 无 env → 默认 ``~/.local/share/opencode/session.db``。"""
    fake_home = tmp_path / "fakehome"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("ORCA_OPENCODE_DB", raising=False)
    a = OpencodeSqliteAdapter("h")
    expected = fake_home / ".local" / "share" / "opencode" / "session.db"
    assert a.db_path == expected


def test_env_db_override(monkeypatch, tmp_path):
    """``ORCA_OPENCODE_DB`` env 覆盖默认。"""
    env_db = tmp_path / "env.db"
    monkeypatch.setenv("ORCA_OPENCODE_DB", str(env_db))
    a = OpencodeSqliteAdapter("h")
    assert a.db_path == env_db


# ── discover_children ─────────────────────────────────────────────────────────


def test_discover_children_extracts_session_id_from_task_tool(db_path):
    """父 event 流有 task tool part (completed) → 提 state.metadata.sessionId。"""
    conn = sqlite3.connect(str(db_path))
    _insert_event(conn, "host-session-1", 1, {
        "part": _part("tool", tool="task", callID="c1",
                      state={"status": "completed",
                             "metadata": {"sessionId": "child-aaa"}}),
    })
    conn.commit()
    conn.close()

    a = OpencodeSqliteAdapter("host-session-1", db_path=db_path)
    children = list(a.discover_children("host-session-1", 0))
    assert children == ["child-aaa"]


def test_discover_children_skips_non_completed_task(db_path):
    """task tool 未 completed（running）→ 无 metadata.sessionId → skip。"""
    conn = sqlite3.connect(str(db_path))
    _insert_event(conn, "host-1", 1, {
        "part": _part("tool", tool="task", callID="c1",
                      state={"status": "running"}),
    })
    conn.commit()
    conn.close()

    a = OpencodeSqliteAdapter("host-1", db_path=db_path)
    assert list(a.discover_children("host-1", 0)) == []


def test_discover_children_skips_non_task_tool(db_path):
    """非 task tool（如 bash/read）→ 不视为子 agent。"""
    conn = sqlite3.connect(str(db_path))
    _insert_event(conn, "host-1", 1, {
        "part": _part("tool", tool="bash", callID="c1",
                      state={"status": "completed",
                             "metadata": {"sessionId": "should-not-yield"}}),
    })
    conn.commit()
    conn.close()

    a = OpencodeSqliteAdapter("host-1", db_path=db_path)
    assert list(a.discover_children("host-1", 0)) == []


def test_discover_children_fallback_session_parent_id(db_path):
    """主路径无 task tool → fallback ``SELECT id FROM session WHERE parent_id=?``。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO session (id, parent_id) VALUES (?, ?)",
                 ("child-via-parent", "host-1"))
    conn.commit()
    conn.close()

    a = OpencodeSqliteAdapter("host-1", db_path=db_path)
    children = list(a.discover_children("host-1", 0))
    assert children == ["child-via-parent"]


def test_discover_children_scope_to_host_session(db_path):
    """host_session 不匹配 → 空（scope 铁律）。"""
    a = OpencodeSqliteAdapter("correct", db_path=db_path)
    assert list(a.discover_children("WRONG", 0)) == []


def test_discover_children_missing_db(tmp_path):
    """DB 不存在 → 返空（不抛）。"""
    a = OpencodeSqliteAdapter("h", db_path=tmp_path / "missing.db")
    assert list(a.discover_children("h", 0)) == []


def test_discover_children_dedupes_session_id(db_path):
    """多个 task tool 指向同一 sessionId → 只 yield 一次。"""
    conn = sqlite3.connect(str(db_path))
    _insert_event(conn, "host-1", 1, {
        "part": _part("tool", tool="task", callID="c1",
                      state={"status": "completed", "metadata": {"sessionId": "dup"}}),
    })
    _insert_event(conn, "host-1", 2, {
        "part": _part("tool", tool="task", callID="c2",
                      state={"status": "completed", "metadata": {"sessionId": "dup"}}),
    })
    conn.commit()
    conn.close()

    a = OpencodeSqliteAdapter("host-1", db_path=db_path)
    assert list(a.discover_children("host-1", 0)) == ["dup"]


# ── stream: 映射 ─────────────────────────────────────────────────────────────


def test_stream_maps_reasoning(db_path):
    """part.type=reasoning → thinking payload {text}。"""
    conn = sqlite3.connect(str(db_path))
    _insert_event(conn, "child-1", 1, {"part": _part("reasoning", text="contemplating")})
    conn.commit()
    conn.close()

    a = OpencodeSqliteAdapter("host-1", db_path=db_path)
    events = [raw for raw, _ in a.stream("child-1", 0)]
    assert len(events) == 1
    assert events[0].kind == "thinking"
    assert events[0].payload == {"text": "contemplating"}
    assert events[0].child_id == "child-1"
    assert events[0].source_id == "opc:1"


def test_stream_maps_text(db_path):
    """part.type=text → text payload {text}。"""
    conn = sqlite3.connect(str(db_path))
    _insert_event(conn, "child-1", 1, {"part": _part("text", text="hello")})
    conn.commit()
    conn.close()

    a = OpencodeSqliteAdapter("host-1", db_path=db_path)
    events = [raw for raw, _ in a.stream("child-1", 0)]
    assert len(events) == 1
    assert events[0].kind == "text"
    assert events[0].payload == {"text": "hello"}


def test_stream_maps_tool_running_to_tool_call(db_path):
    """tool part status=running → tool_call payload {tool, args, tool_call_id}。"""
    conn = sqlite3.connect(str(db_path))
    _insert_event(conn, "child-1", 1, {
        "part": _part("tool", tool="bash", callID="c1",
                      state={"status": "running", "input": {"command": "ls"}}),
    })
    conn.commit()
    conn.close()

    a = OpencodeSqliteAdapter("host-1", db_path=db_path)
    events = [raw for raw, _ in a.stream("child-1", 0)]
    assert len(events) == 1
    e = events[0]
    assert e.kind == "tool_call"
    assert e.payload == {"tool": "bash", "args": {"command": "ls"}, "tool_call_id": "c1"}
    assert e.source_id == "opc:1"


def test_stream_maps_tool_completed_to_tool_result(db_path):
    """tool part status=completed → tool_result payload {tool_call_id, result}。"""
    conn = sqlite3.connect(str(db_path))
    _insert_event(conn, "child-1", 1, {
        "part": _part("tool", tool="bash", callID="c1",
                      state={"status": "completed", "output": "file.txt"}),
    })
    conn.commit()
    conn.close()

    a = OpencodeSqliteAdapter("host-1", db_path=db_path)
    events = [raw for raw, _ in a.stream("child-1", 0)]
    assert len(events) == 1
    e = events[0]
    assert e.kind == "tool_result"
    assert e.payload == {"tool_call_id": "c1", "result": "file.txt"}


def test_stream_tool_running_then_completed_yields_call_then_result(db_path):
    """同一 part.id 在 INSERT(running) + UPDATE(completed) 双 event 行 → 两个 RawAgentEvent。

    source_id 用 seq 唯一（part.id 相同，seq 不同）。
    """
    conn = sqlite3.connect(str(db_path))
    _insert_event(conn, "child-1", 1, {
        "part": _part("tool", tool="bash", callID="c1", id="part-9",
                      state={"status": "running", "input": {"command": "ls"}}),
    })
    _insert_event(conn, "child-1", 2, {
        "part": _part("tool", tool="bash", callID="c1", id="part-9",
                      state={"status": "completed", "output": "file.txt"}),
    })
    conn.commit()
    conn.close()

    a = OpencodeSqliteAdapter("host-1", db_path=db_path)
    events = [raw for raw, _ in a.stream("child-1", 0)]
    assert len(events) == 2
    assert events[0].kind == "tool_call"
    assert events[0].source_id == "opc:1"
    assert events[1].kind == "tool_result"
    assert events[1].source_id == "opc:2"
    # tool_call_id 配对（前端 pairToolEvents 用此）。
    assert events[0].payload["tool_call_id"] == events[1].payload["tool_call_id"]


def test_stream_maps_step_start_to_step_boundary(db_path):
    """part.type=step-start → step_boundary payload {phase:"start"}。"""
    conn = sqlite3.connect(str(db_path))
    _insert_event(conn, "child-1", 1, {"part": _part("step-start", reason="tool-calls")})
    conn.commit()
    conn.close()

    a = OpencodeSqliteAdapter("host-1", db_path=db_path)
    events = [raw for raw, _ in a.stream("child-1", 0)]
    assert len(events) == 1
    assert events[0].kind == "step_boundary"
    assert events[0].payload == {"phase": "start"}


def test_stream_skips_step_finish(db_path):
    """part.type=step-finish → skip（usage 在 B2 scope 外，U4 deferred）。"""
    conn = sqlite3.connect(str(db_path))
    _insert_event(conn, "child-1", 1, {
        "part": _part("step-finish", tokens={"input": 100}, cost=0.01),
    })
    conn.commit()
    conn.close()

    a = OpencodeSqliteAdapter("host-1", db_path=db_path)
    assert list(a.stream("child-1", 0)) == []


def test_stream_skips_empty_text(db_path):
    """空 text / 空 reasoning → skip。"""
    conn = sqlite3.connect(str(db_path))
    _insert_event(conn, "child-1", 1, {"part": _part("text", text="")})
    _insert_event(conn, "child-1", 2, {"part": _part("reasoning", text="")})
    _insert_event(conn, "child-1", 3, {"part": _part("text", text="real")})
    conn.commit()
    conn.close()

    a = OpencodeSqliteAdapter("host-1", db_path=db_path)
    events = [raw for raw, _ in a.stream("child-1", 0)]
    assert len(events) == 1
    assert events[0].payload == {"text": "real"}


def test_stream_truncates_long_tool_output(db_path):
    """tool output > 4096 chars → 截断（防喷爆，同 translator）。"""
    long_output = "x" * 5000
    conn = sqlite3.connect(str(db_path))
    _insert_event(conn, "child-1", 1, {
        "part": _part("tool", tool="bash", callID="c1",
                      state={"status": "completed", "output": long_output}),
    })
    conn.commit()
    conn.close()

    a = OpencodeSqliteAdapter("host-1", db_path=db_path)
    events = [raw for raw, _ in a.stream("child-1", 0)]
    assert len(events[0].payload["result"]) < 5000
    assert "…[truncated]" in events[0].payload["result"]


# ── stream: cursor + 边界 ────────────────────────────────────────────────────


def test_stream_cursor_resumes_from_seq(db_path):
    """第二次 stream 从上次最大 seq 续读。"""
    conn = sqlite3.connect(str(db_path))
    _insert_event(conn, "child-1", 1, {"part": _part("text", text="first")})
    _insert_event(conn, "child-1", 2, {"part": _part("text", text="second")})
    conn.commit()
    conn.close()

    a = OpencodeSqliteAdapter("host-1", db_path=db_path)
    events1 = list(a.stream("child-1", 0))
    assert len(events1) == 2
    cursor_after = events1[-1][1]
    assert cursor_after == 2

    # 追加新 event。
    conn = sqlite3.connect(str(db_path))
    _insert_event(conn, "child-1", 3, {"part": _part("text", text="third")})
    conn.commit()
    conn.close()

    events2 = list(a.stream("child-1", cursor_after))
    assert len(events2) == 1
    assert events2[0][0].payload == {"text": "third"}


def test_stream_filters_other_event_types(db_path):
    """非 ``message.part.updated.1`` event 行（如 session.updated.1）→ skip。"""
    conn = sqlite3.connect(str(db_path))
    _insert_event(conn, "child-1", 1, {"part": _part("text", text="keep")})
    # 其它 event type 行（adapter 不读）。
    _insert_event(conn, "child-1", 2, {"info": "noise"}, event_type="session.updated.1")
    _insert_event(conn, "child-1", 3, {"part": _part("text", text="also-keep")})
    conn.commit()
    conn.close()

    a = OpencodeSqliteAdapter("host-1", db_path=db_path)
    events = [raw for raw, _ in a.stream("child-1", 0)]
    assert len(events) == 2
    assert events[0].payload["text"] == "keep"
    assert events[1].payload["text"] == "also-keep"


def test_stream_missing_db(tmp_path):
    """DB 不存在 → 返空。"""
    a = OpencodeSqliteAdapter("h", db_path=tmp_path / "missing.db")
    assert list(a.stream("child-x", 0)) == []


def test_stream_skips_corrupt_rows(db_path):
    """row data 非 JSON → skip（不阻塞）。"""
    conn = sqlite3.connect(str(db_path))
    _insert_event(conn, "child-1", 1, {"part": _part("text", text="valid")})
    # 直接写损坏 JSON 行。
    conn.execute(
        "INSERT INTO event (aggregate_id, seq, type, data) VALUES (?, ?, ?, ?)",
        ("child-1", 2, "message.part.updated.1", "not-valid-json{"),
    )
    _insert_event(conn, "child-1", 3, {"part": _part("text", text="after-corrupt")})
    conn.commit()
    conn.close()

    a = OpencodeSqliteAdapter("host-1", db_path=db_path)
    events = [raw for raw, _ in a.stream("child-1", 0)]
    assert len(events) == 2
    assert events[0].payload["text"] == "valid"
    assert events[1].payload["text"] == "after-corrupt"


def test_readonly_connection_does_not_create_db(tmp_path):
    """``mode=ro`` 不创建不存在的 DB（vs 默认 connect 会建空 DB）。"""
    nonexistent = tmp_path / "should-not-create.db"
    a = OpencodeSqliteAdapter("h", db_path=nonexistent)
    # 调 discover（内部 _connect_ro）→ 不应创建文件。
    list(a.discover_children("h", 0))
    assert not nonexistent.exists(), "mode=ro 连接不应创建 DB"


def test_readonly_connection_rejects_writes(db_path):
    """readonly 连接写尝试 → sqlite3.OperationalError（防 adapter 误写 opencode DB）。"""
    a = OpencodeSqliteAdapter("h", db_path=db_path)
    conn = a._connect_ro()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO event (aggregate_id, seq, type, data) VALUES ('x', 1, 't', '{}')")
    finally:
        conn.close()
