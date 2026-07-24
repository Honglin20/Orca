"""tests/events/test_tape_read_last_complete_lines.py —— S7 helper 直接单元测试（SPEC §5 S7）。

``events.tape.read_last_complete_lines`` 是 S7 DRY 出的公共 helper（chart/sidechain 守护
3 处增量 tape 读路径共享）。本测试直接断言 helper 契约（不依赖守护进程）：

  - OSError（file missing）→ ``(None, start_offset)`` 不推进。
  - ``end_offset <= start_offset`` → ``([], start_offset)``（与 None 区分：明确「无新内容」）。
  - 整段 partial（无 ``\\n``）→ ``(None, start_offset)`` 不推进。
  - 含完整行 → 推进 offset 到最后一个 ``\\n`` 之后，返 split 后的字符串列表。
  - 多字节 UTF-8 内容 → byte offset 对齐（不漂移到 continuation byte）。

间接覆盖（既有）：``test_chart_daemon_multibyte.py`` 守 binary-mode 多字节安全；
``test_chart_daemon.py::test_*partial*`` 守 partial-line race 防护。本文件加**直接单元测试**
覆盖 helper 内部的边界分支（OSError / 空 / 纯 partial），3 处生产调用点都不会触发这些分支。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca.events.tape import read_last_complete_lines


def test_oserror_returns_none_keeps_offset(tmp_path: Path) -> None:
    """文件不存在 → OSError 分支：返 ``(None, start_offset)``，不推进 offset。"""
    missing = tmp_path / "nope.jsonl"
    lines, new_offset = read_last_complete_lines(missing, start_offset=0, end_offset=100)
    assert lines is None, "OSError → lines=None（调用方据此保留缓存）"
    assert new_offset == 0, "OSError → 不推进 offset（调用方下次重读）"


def test_end_le_start_returns_empty_list(tmp_path: Path) -> None:
    """``end_offset <= start_offset`` → 返 ``([], start_offset)``。

    与 ``None`` 显式区分：``None`` = 「有新字节但无完整行 / 读失败」；``[]`` = 「无新内容」。
    调用方据此区分缓存策略（None 沿用缓存，[] 无事可做）。
    """
    p = tmp_path / "t.jsonl"
    p.write_text('{"seq":1}\n', encoding="utf-8")

    # end == start（无新内容）
    lines, new_offset = read_last_complete_lines(p, start_offset=10, end_offset=10)
    assert lines == []
    assert new_offset == 10

    # end < start（理论不应发生；helper 不假设 stat shrink 处理）
    lines2, new_offset2 = read_last_complete_lines(p, start_offset=15, end_offset=10)
    assert lines2 == []
    assert new_offset2 == 15


def test_pure_partial_no_newline_returns_none(tmp_path: Path) -> None:
    """整段 chunk 无 ``\\n`` → ``(None, start_offset)``，不推进 offset（下次重读）。

    模拟 POSIX ``write(2)`` 中途：已写 N 字节但行尾 ``\\n`` 未落盘 → poll 落在 write 中途。
    helper 推进到 ``\\n`` 之后保证 partial 尾字节下次重读，终态事件 / max_seq 不丢。
    """
    p = tmp_path / "t.jsonl"
    p.write_text("partial-no-newline", encoding="utf-8")  # 18 字节，无 \\n
    size = p.stat().st_size

    lines, new_offset = read_last_complete_lines(p, start_offset=0, end_offset=size)
    assert lines is None, "纯 partial → lines=None（调用方不推进 offset）"
    assert new_offset == 0, "纯 partial → 不推进 offset（下次重读 partial 尾 + 后续新字节）"


def test_complete_lines_advances_offset_past_last_newline(tmp_path: Path) -> None:
    """有 ``\\n`` → 推进到 ``最后一个 \\n + 1``，返 split 后的字符串列表。

    partial 尾字节（无 ``\\n``）保留在 offset 之前，下次重读。
    """
    p = tmp_path / "t.jsonl"
    # 三行完整 + 一行 partial
    p.write_text('{"seq":1}\n{"seq":2}\n{"seq":3}\npartial\n', encoding="utf-8")
    size = p.stat().st_size

    lines, new_offset = read_last_complete_lines(p, start_offset=0, end_offset=size)
    # chunk = '{"seq":1}\\n{"seq":2}\\n{"seq":3}\\npartial\\n'.split('\\n') →
    # ['{"seq":1}', '{"seq":2}', '{"seq":3}', 'partial', '']
    assert lines == ['{"seq":1}', '{"seq":2}', '{"seq":3}', "partial", ""]
    # 推进到最后一个 \\n 之后（"{...3}\\n" 后是 "partial\\n"，末尾 \\n 偏移）
    # 30 字节 = 9+1 + 9+1 + 9+1 + 7+1 = '{"seq":1}\\n' * 3 + 'partial\\n'
    assert new_offset == size, "末尾 \\n 是最后一字节 → 推进到 size"


def test_partial_tail_bytes_not_advanced(tmp_path: Path) -> None:
    """末尾 partial 字节（无 ``\\n``）不推进 offset（下次重读）。"""
    p = tmp_path / "t.jsonl"
    # 完整行 + partial 尾（无 \\n）
    p.write_text('{"seq":1}\n{"seq":2}\npartial-tail', encoding="utf-8")
    size = p.stat().st_size

    lines, new_offset = read_last_complete_lines(p, start_offset=0, end_offset=size)
    assert lines == ['{"seq":1}', '{"seq":2}', ""]
    # 推进到第二个 \\n 之后；"partial-tail"（12 字节）保留在 offset 之前
    expected_offset = len('{"seq":1}\n{"seq":2}\n')
    assert new_offset == expected_offset


def test_multibyte_utf8_byte_offset_alignment(tmp_path: Path) -> None:
    """SPEC §5 S7 / B2-VRFY：多字节 UTF-8 内容，byte offset 不漂移。

    text-mode seek(字节)/read(字符) 混算会让 offset 落到 continuation byte →
    UnicodeDecodeError。helper 用 binary-mode + byte offsets 守此陷阱。
    """
    p = tmp_path / "mb.jsonl"
    # 含中文 + emoji 的多字节内容（每字符 3-4 字节）
    p.write_text('{"text":"中文🚀"}\n{"text":"更多内容"}\n', encoding="utf-8")
    size = p.stat().st_size

    # 第一次读：完整两行
    lines1, off1 = read_last_complete_lines(p, start_offset=0, end_offset=size)
    assert len(lines1) == 3  # 两行 + 末尾空串
    assert "中文🚀" in lines1[0]
    assert "更多内容" in lines1[1]
    assert off1 == size

    # 第二次读：追加 ASCII 行 + 再读（验证 byte offset 跨多字节不漂移）
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"text":"ascii only"}\n')
    new_size = p.stat().st_size
    lines2, off2 = read_last_complete_lines(p, start_offset=off1, end_offset=new_size)
    assert lines2 == ['{"text":"ascii only"}', ""]
    assert off2 == new_size


def test_decode_errors_replace_does_not_raise(tmp_path: Path) -> None:
    """SPEC：``decode("utf-8", errors="replace")`` → 截断 continuation byte 不抛。

    模拟「文件被多字节字符截断」边界：写入 1 个汉字首字节（非完整 UTF-8 序列）+ \\n。
    helper 应 ``errors="replace"`` 解码，不抛 UnicodeDecodeError（OSError 才抛）。
    """
    p = tmp_path / "trunc.jsonl"
    # 写入「中」字首字节（0xe4）+ \\n（不完整 UTF-8 序列）
    p.write_bytes(b"\xe4\n")
    size = p.stat().st_size

    lines, new_offset = read_last_complete_lines(p, start_offset=0, end_offset=size)
    # 解码后首字节变 replacement char（U+FFFD），不抛
    assert lines is not None
    assert new_offset == size


def test_empty_file_returns_empty(tmp_path: Path) -> None:
    """空文件 stat=0 → ``end_offset <= start_offset`` 分支 → ``([], 0)``。"""
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    size = p.stat().st_size
    assert size == 0

    lines, new_offset = read_last_complete_lines(p, start_offset=0, end_offset=size)
    assert lines == []
    assert new_offset == 0
