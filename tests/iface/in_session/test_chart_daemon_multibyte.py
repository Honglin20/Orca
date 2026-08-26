"""tests/iface/in_session/test_chart_daemon_multibyte.py —— B2-VRFY Bug #3 回归（共享 chart_daemon）。

``chart_daemon._FlockSafeTape._read_max_seq_from_disk`` 与 ``_watch_terminal`` 原用 text-mode
``seek(字节)/read(字符)`` 混算，多字节 UTF-8 tape 上 offset 漂移到 continuation byte →
``UnicodeDecodeError``（ValueError 子类，非 OSError，未被兜住）传播。

chart 自身 payload 当前是 ASCII（故历史未触发），但 B2 引入中文/emoji 的 agent_* 事件后，
这条**共享**路径必崩（test-agent 真机实测：4435 CC 事件含中文 → 崩在 8 事件后卡死）。
binary-mode 修复（byte seek + ``rfind(b"\\n")`` + ``decode(errors="replace")``）后必过。
"""

from __future__ import annotations

import json
from pathlib import Path

from orca.iface.in_session.chart_daemon import _FlockSafeTape


def _append_utf8(tape_path: Path, obj: dict) -> None:
    """append 一行（ensure_ascii=False → 中文/emoji 为真实多字节 UTF-8 字节）。"""
    with open(tape_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def test_read_max_seq_from_disk_multibyte_tape(tmp_path: Path) -> None:
    """多字节 tape 多轮增量扫：不崩 + max_seq 正确推进（旧 text-mode 第 2 轮起必崩）。"""
    tape_path = tmp_path / "mb.jsonl"
    flock_path = tmp_path / "mb.flock"
    tape = _FlockSafeTape(tape_path, run_id="r", flock_path=flock_path)
    assert tape._read_max_seq_from_disk() == 0  # 空 tape

    # 三轮：每轮 append 大段多字节内容 + 带 seq 的行，增量扫 max_seq。
    # 多字节使 byte/char 发散 → 旧代码第 2+ 轮 seek 落 continuation byte 必崩。
    for i in range(1, 4):
        _append_utf8(tape_path, {"seq": i * 10, "type": "agent_message",
                                 "data": {"text": "中文多字节内容审计🚀" * 15}})
        _append_utf8(tape_path, {"seq": i * 10 + 1, "type": "node_started", "node": f"节点{i}"})
        assert tape._read_max_seq_from_disk() == i * 10 + 1, f"轮 {i} max_seq 应推进到 {i * 10 + 1}"


def test_read_max_seq_from_disk_multibyte_then_ascii(tmp_path: Path) -> None:
    """多字节内容后再追加 ASCII 行：offset 仍字节对齐，max_seq 继续正确（防累积漂移）。"""
    tape_path = tmp_path / "mb2.jsonl"
    flock_path = tmp_path / "mb2.flock"
    tape = _FlockSafeTape(tape_path, run_id="r", flock_path=flock_path)

    _append_utf8(tape_path, {"seq": 5, "type": "agent_message",
                             "data": {"text": "中文🚀内容" * 10}})
    assert tape._read_max_seq_from_disk() == 5

    # 追加更高 seq 的 ASCII 行（ensure_ascii=True 默认）。
    with open(tape_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"seq": 9, "type": "node_started", "node": "x"}) + "\n")
    assert tape._read_max_seq_from_disk() == 9
