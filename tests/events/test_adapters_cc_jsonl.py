"""tests/events/test_adapters_cc_jsonl.py —— CC sidechain jsonl adapter（SPEC-B v4 §4）。

覆盖意图：
  - 行映射：assistant+thinking / tool_use / text / user+tool_result → 正确 RawAgentEvent。
  - skip：stream_event / system / result / user 非 tool_result。
  - cursor：byte offset 增量推进（同 child 两次 stream，cursor 续读）。
  - partial-line race：末行无 \\n → 不推进 cursor，下次重读。
  - source_id 唯一：一行多 content block → 每个 block 独立 source_id。
  - discover_children scope：host_session 不匹配 → 空迭代器。
  - since_ts 过滤：mtime 旧于 since_ts → skip。
  - discover_children 显式 spawn 过滤：无 meta.json 的系统子代理 → skip。
  - fail loud：host_session 空 → raise CCAdapterError。
  - ORCA_CC_SIDECHAIN_ROOT env 覆盖（硬约束 #5）。
  - root 不存在 → discover 返空（不抛；subagent 尚未起）。
  - 单行损坏（非 JSON）→ 静默跳过。
  - tool_result.content 多形态（str / list[{type,text}] / None）归一。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from orca.events.adapters.cc_jsonl import (
    CCAdapterError,
    CCJsonlAdapter,
    _encode_cwd,
)
from orca.events.raw_agent_event import RawAgentEvent


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def sidechain_root(tmp_path: Path) -> Path:
    """``<tmp>/sidechain/`` 作 sidechain root；每 test 隔离。"""
    root = tmp_path / "sidechain"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def adapter(sidechain_root: Path) -> CCJsonlAdapter:
    """构造一个 CCJsonlAdapter，root 显式给（覆盖 env / 派生）。"""
    return CCJsonlAdapter("host-session-xyz", root=sidechain_root)


def _write_subagent_line(root: Path, task_id: str, obj: dict) -> None:
    """向 ``<root>/agent-<task_id>.jsonl`` 追加一行 JSON。"""
    path = root / f"agent-{task_id}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def _write_meta(root: Path, task_id: str, *, payload: dict | None = None) -> None:
    """写 ``agent-<task_id>.meta.json``（标记主 session Agent tool 显式 spawn 的子代理）。"""
    path = root / f"agent-{task_id}.meta.json"
    path.write_text(json.dumps(payload or {}), encoding="utf-8")


def _assistant(content: list, *, parent_id: str | None = None) -> dict:
    """构造 assistant 行（stream-json 完整消息形态）。"""
    msg = {"role": "assistant", "content": content}
    if parent_id:
        msg["parent_tool_use_id"] = parent_id
    return {"type": "assistant", "message": msg}


def _user_tool_result(tool_use_id: str, content) -> dict:
    """构造 user 行（tool_result block）。"""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}],
        },
    }


# ── _encode_cwd ───────────────────────────────────────────────────────────────


def test_encode_cwd_non_alphanumeric_to_dash():
    """cwd 的非字母数字字符逐个 → ``-``（CC projects 目录约定；与 ``_family._encode_cwd`` 同源）。"""
    assert _encode_cwd("/mnt/d/Projects/Orca") == "-mnt-d-Projects-Orca"
    assert _encode_cwd("/") == "-"
    assert _encode_cwd("relative") == "relative"
    # 下划线 / 点 / 空格 → -（CC 实证；旧实现仅换 / 时的漏网项）
    assert _encode_cwd("/home/u/my_app") == "-home-u-my-app"
    assert _encode_cwd("/srv/app.v2") == "-srv-app-v2"
    assert _encode_cwd("/tmp/orca space dir") == "-tmp-orca-space-dir"


# ── fail loud ─────────────────────────────────────────────────────────────────


def test_construct_raises_on_empty_host_session(tmp_path):
    """host_session 空 + 无 env → raise CCAdapterError（fail loud）。"""
    # 清 env 保证不被干扰。
    for k in ("ORCA_CC_SIDECHAIN_ROOT",):
        os.environ.pop(k, None)
    with pytest.raises(CCAdapterError, match="host_session"):
        CCJsonlAdapter("")


def test_env_root_override(tmp_path, monkeypatch):
    """``ORCA_CC_SIDECHAIN_ROOT`` env 覆盖派生 root（硬约束 #5 测试覆盖）。"""
    env_root = tmp_path / "env-override-sidechain"
    monkeypatch.setenv("ORCA_CC_SIDECHAIN_ROOT", str(env_root))
    a = CCJsonlAdapter("host-x")
    assert a.root == env_root


def test_explicit_root_arg_overrides_env(tmp_path, monkeypatch):
    """显式 root 参数优先于 env。"""
    monkeypatch.setenv("ORCA_CC_SIDECHAIN_ROOT", str(tmp_path / "env"))
    explicit = tmp_path / "explicit"
    a = CCJsonlAdapter("host-x", root=explicit)
    assert a.root == explicit


# ── discover_children ─────────────────────────────────────────────────────────


def test_discover_children_empty_when_root_missing(tmp_path):
    """root 不存在 → 返空（subagent 尚未起；不抛）。"""
    a = CCJsonlAdapter("h", root=tmp_path / "nonexistent")
    assert list(a.discover_children("h", 0)) == []


def test_discover_children_yields_task_ids(sidechain_root, adapter):
    """glob agent-*.jsonl + 伴 meta.json → yield task_id（去 ``agent-`` 前缀）。"""
    _write_subagent_line(sidechain_root, "task-aaa", {"type": "system"})
    _write_meta(sidechain_root, "task-aaa")
    _write_subagent_line(sidechain_root, "task-bbb", {"type": "system"})
    _write_meta(sidechain_root, "task-bbb")
    # 非 agent- 前缀文件应被忽略。
    (sidechain_root / "other.log").write_text("noise")

    children = list(adapter.discover_children("host-session-xyz", 0))
    assert sorted(children) == ["task-aaa", "task-bbb"]


def test_discover_children_scope_to_host_session(sidechain_root):
    """``host_session`` 与 adapter 构造时不同 → 返空（scope 铁律）。"""
    a = CCJsonlAdapter("correct-session", root=sidechain_root)
    _write_subagent_line(sidechain_root, "t1", {"type": "system"})
    # 故意传错 host_session。
    assert list(a.discover_children("WRONG-session", 0)) == []


def test_discover_children_since_ts_filters_old(sidechain_root, adapter):
    """since_ts 用 mtime 过滤旧文件。"""
    _write_subagent_line(sidechain_root, "old-task", {"type": "system"})
    _write_meta(sidechain_root, "old-task")
    old_path = sidechain_root / "agent-old-task.jsonl"
    # 设 mtime 为 1 小时前。
    old_time = time.time() - 3600
    os.utime(old_path, (old_time, old_time))

    future_ts = int(time.time())  # 现在 → old-task 被过滤
    children = list(adapter.discover_children("host-session-xyz", future_ts))
    assert children == []

    # since_ts = 0 → 不过滤，看到 old-task。
    assert list(adapter.discover_children("host-session-xyz", 0)) == ["old-task"]


def test_discover_children_skips_child_without_meta(sidechain_root, adapter):
    """无 meta.json 的子代理（宿主后台系统代理，如 CAC asession_memory）→ 跳过。

    意图：discover 只收主 session Agent tool 显式 spawn 的子代理（伴 meta.json），
    防止系统 memory helper 污染 workflow 节点 tape。远程已实证修复（asession_memory-*
    事件不再进 tape）。
    """
    # 任务子代理：jsonl + meta.json → yield。
    _write_subagent_line(sidechain_root, "task-real", {"type": "system"})
    _write_meta(sidechain_root, "task-real")
    # 系统子代理：仅 jsonl，无 meta.json → 跳过。
    _write_subagent_line(sidechain_root, "asession_memory-abc", {"type": "system"})

    children = list(adapter.discover_children("host-session-xyz", 0))
    assert children == ["task-real"]


# ── stream: 行映射 ────────────────────────────────────────────────────────────


def test_stream_maps_assistant_thinking(sidechain_root, adapter):
    """assistant content block thinking → RawAgentEvent(thinking, {text})."""
    _write_subagent_line(sidechain_root, "t1", _assistant([
        {"type": "thinking", "thinking": "let me consider"},
    ]))
    events = [raw for raw, _ in adapter.stream("t1", 0)]
    assert len(events) == 1
    e = events[0]
    assert e.kind == "thinking"
    assert e.payload == {"text": "let me consider"}
    assert e.child_id == "t1"
    assert e.source_id == "t1:0:0"  # line_idx=0, block_idx=0


def test_stream_maps_assistant_tool_use(sidechain_root, adapter):
    """assistant tool_use → tool_call payload {tool, args, tool_call_id}."""
    _write_subagent_line(sidechain_root, "t1", _assistant([
        {"type": "tool_use", "name": "Read", "id": "tc-1", "input": {"path": "/x"}},
    ]))
    events = [raw for raw, _ in adapter.stream("t1", 0)]
    assert len(events) == 1
    e = events[0]
    assert e.kind == "tool_call"
    assert e.payload == {"tool": "Read", "args": {"path": "/x"}, "tool_call_id": "tc-1"}
    assert e.source_id == "t1:0:0"


def test_stream_maps_assistant_text(sidechain_root, adapter):
    """assistant text block → text payload {text}。"""
    _write_subagent_line(sidechain_root, "t1", _assistant([
        {"type": "text", "text": "hello world"},
    ]))
    events = [raw for raw, _ in adapter.stream("t1", 0)]
    assert len(events) == 1
    assert events[0].kind == "text"
    assert events[0].payload == {"text": "hello world"}


def test_stream_maps_user_tool_result_str(sidechain_root, adapter):
    """user tool_result.content str → tool_result payload {tool_call_id, result}."""
    _write_subagent_line(sidechain_root, "t1", _user_tool_result("tc-1", "PHASE_B_FIXTURE"))
    events = [raw for raw, _ in adapter.stream("t1", 0)]
    assert len(events) == 1
    e = events[0]
    assert e.kind == "tool_result"
    assert e.payload == {"tool_call_id": "tc-1", "result": "PHASE_B_FIXTURE"}


def test_stream_maps_user_tool_result_list(sidechain_root, adapter):
    """tool_result.content list[{type,text}] → 归一为拼接字符串（同 claude_translator）。"""
    content = [
        {"type": "text", "text": "part1 "},
        {"type": "text", "text": "part2"},
    ]
    _write_subagent_line(sidechain_root, "t1", _user_tool_result("tc-1", content))
    events = [raw for raw, _ in adapter.stream("t1", 0)]
    assert len(events) == 1
    assert events[0].payload["result"] == "part1 part2"


def test_stream_skips_non_relevant_lines(sidechain_root, adapter):
    """stream_event / system / result / user 非 tool_result → skip（不产事件）。"""
    _write_subagent_line(sidechain_root, "t1", {"type": "system", "subtype": "init"})
    _write_subagent_line(sidechain_root, "t1", {
        "type": "stream_event", "event": {"type": "content_block_delta",
                                          "delta": {"type": "text_delta", "text": "增量"}}
    })
    _write_subagent_line(sidechain_root, "t1", {"type": "result", "result": "done"})
    _write_subagent_line(sidechain_root, "t1", {
        "type": "user", "message": {"role": "user", "content": [
            {"type": "attachment", "name": "foo"},  # 非 tool_result
        ]},
    })
    events = [raw for raw, _ in adapter.stream("t1", 0)]
    assert events == [], "stream_event/system/result/attachment 全 skip"


def test_stream_handles_multiple_blocks_in_one_line(sidechain_root, adapter):
    """一行多 content block → 多 RawAgentEvent，source_id 含 block_idx 保唯一。"""
    _write_subagent_line(sidechain_root, "t1", _assistant([
        {"type": "thinking", "thinking": "thought"},
        {"type": "text", "text": "answer"},
        {"type": "tool_use", "name": "Read", "id": "tc-1", "input": {"path": "/x"}},
    ]))
    events = [raw for raw, _ in adapter.stream("t1", 0)]
    assert len(events) == 3
    kinds = [e.kind for e in events]
    assert kinds == ["thinking", "text", "tool_call"]
    # 全部 source_id 唯一（含 block_idx 0/1/2）。
    source_ids = [e.source_id for e in events]
    assert len(set(source_ids)) == 3
    assert source_ids == ["t1:0:0", "t1:0:1", "t1:0:2"]


def test_stream_skips_empty_text_and_thinking(sidechain_root, adapter):
    """空 text / 空 thinking → skip（不产噪音事件）。"""
    _write_subagent_line(sidechain_root, "t1", _assistant([
        {"type": "text", "text": ""},
        {"type": "thinking", "thinking": ""},
        {"type": "text", "text": "real"},
    ]))
    events = [raw for raw, _ in adapter.stream("t1", 0)]
    assert len(events) == 1
    assert events[0].payload == {"text": "real"}


# ── stream: cursor + partial-line ─────────────────────────────────────────────


def test_stream_cursor_resumes_from_byte_offset(sidechain_root, adapter):
    """第二次 stream 从上次返回的 cursor 续读。"""
    _write_subagent_line(sidechain_root, "t1", _assistant([{"type": "text", "text": "first"}]))
    events1 = list(adapter.stream("t1", 0))
    assert len(events1) == 1
    cursor_after_first = events1[-1][1]
    assert cursor_after_first > 0

    # 追加第二行。
    _write_subagent_line(sidechain_root, "t1", _assistant([{"type": "text", "text": "second"}]))
    events2 = list(adapter.stream("t1", cursor_after_first))
    assert len(events2) == 1
    assert events2[0][0].payload == {"text": "second"}


def test_stream_partial_line_does_not_advance_cursor(sidechain_root, adapter, tmp_path):
    """末行无 \\n → 不 yield、不推进 cursor；下次重读。"""
    # 先写一行完整 + 一行 partial。
    _write_subagent_line(sidechain_root, "t1", _assistant([{"type": "text", "text": "complete"}]))
    path = sidechain_root / "agent-t1.jsonl"
    # partial 写成「合法 JSON 的前缀」，后续 append 能拼成完整 JSON。
    with open(path, "ab") as f:
        f.write(b'{"type": "assistant", "message": ')  # partial，无 \n

    events = list(adapter.stream("t1", 0))
    # 只应拿到 1 个完整行的事件；partial 不进。
    assert len(events) == 1
    assert events[0][0].payload == {"text": "complete"}
    cursor = events[-1][1]

    # 补完 partial → 下次 stream 从 cursor 续读，能看到补完的那行（拼成合法 JSON）。
    with open(path, "ab") as f:
        f.write(b'{"role":"assistant","content":[{"type":"text","text":"finished"}]}}\n')
    events2 = list(adapter.stream("t1", cursor))
    assert len(events2) == 1
    assert events2[0][0].payload == {"text": "finished"}


def test_stream_missing_file_returns_empty(sidechain_root, adapter):
    """child 文件不存在 → 返空（不抛）。"""
    assert list(adapter.stream("nonexistent-child", 0)) == []


def test_stream_skips_corrupt_lines(sidechain_root, adapter):
    """完整 \\n 行但非合法 JSON → 静默跳过（不阻塞）。"""
    path = sidechain_root / "agent-t1.jsonl"
    # 写一行损坏 + 一行有效。
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"valid_json": false\n')  # 损坏行（无逗号）
        f.write(json.dumps(_assistant([{"type": "text", "text": "ok"}])) + "\n")
    events = list(adapter.stream("t1", 0))
    assert len(events) == 1
    assert events[0][0].payload == {"text": "ok"}


# ── 多 child 隔离 ─────────────────────────────────────────────────────────────


def test_stream_isolates_children_by_filename(sidechain_root, adapter):
    """不同 child 的 jsonl 互不干扰（按文件名寻址）。"""
    _write_subagent_line(sidechain_root, "t1", _assistant([{"type": "text", "text": "from-t1"}]))
    _write_subagent_line(sidechain_root, "t2", _assistant([{"type": "text", "text": "from-t2"}]))

    e1 = [raw for raw, _ in adapter.stream("t1", 0)]
    e2 = [raw for raw, _ in adapter.stream("t2", 0)]
    assert len(e1) == 1 and e1[0].child_id == "t1"
    assert len(e2) == 1 and e2[0].child_id == "t2"
