"""test_unit_server.py —— MCP server 骨架单元测试（SPEC phase-10 §D2.4）。

D2 阶段覆盖：
  - **单例 assert**：构造第二个 RunManager 应被 ``_assert_runmanager_singleton`` 检测。
  - **FlushingStdoutWriter flush**：每写一次 flush 一次。
  - **不引用 elicitation / progress API**：grep assert。
  - **OrcaMcpServer 构造**：构造后 _mcp 实例就位（D3 加 list_tools 四件套测）。
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orca.iface.mcp import FlushingStdoutWriter
from orca.iface.mcp.server import OrcaMcpServer, _assert_runmanager_singleton
from orca.iface.web.run_manager import RunManager

from tests.iface.web.conftest import run_async


# ── 1. 单例 assert（SPEC §0.1 第一条 / §1.4）──────────────────────────────────


def test_singleton_violation_detected_when_two_managers():
    """构造第二个 RunManager 应被 ``_assert_runmanager_singleton`` 检测（raise）。

    意图：防止后续 refactor 误造多实例（多壳必须同进程共享同一 RunManager）。
    """
    import gc

    from orca.iface.web.run_manager import RunManager as _RM

    gc.collect()
    pre_count = sum(1 for o in gc.get_objects() if isinstance(o, _RM))
    assert pre_count == 0, f"测试前置：残留 {pre_count} 个 RunManager 未回收"

    m1 = _RM()
    m2 = _RM()  # noqa: F841 — 故意制造第二个实例，触发 assert
    with pytest.raises(RuntimeError, match="singleton violated"):
        _assert_runmanager_singleton(m1)


def test_singleton_ok_with_one_manager():
    """单实例时 assert 通过（无 raise）。"""
    import gc

    from orca.iface.web.run_manager import RunManager as _RM

    gc.collect()
    pre_count = sum(1 for o in gc.get_objects() if isinstance(o, _RM))
    assert pre_count == 0, f"测试前置：残留 {pre_count} 个 RunManager 未回收"

    m = _RM()
    _assert_runmanager_singleton(m)  # 不 raise 即通过


# ── 2. FlushingStdoutWriter flush（SPEC §0.1 第四条）─────────────────────────


def test_flushing_writer_flushes_after_each_write():
    """FlushingStdoutWriter 每写一次 flush 一次（SPEC §4.1）。"""
    mock_stream = MagicMock()
    writer = FlushingStdoutWriter(mock_stream)

    run_async(_write_three(writer))

    assert mock_stream.write.call_count == 3
    assert mock_stream.flush.call_count == 3


async def _write_three(writer: FlushingStdoutWriter) -> None:
    await writer.write(b'{"jsonrpc":"2.0","method":"a"}\n')
    await writer.write(b'{"jsonrpc":"2.0","method":"b"}\n')
    await writer.write(b'{"jsonrpc":"2.0","method":"c"}\n')


def test_flushing_writer_explicit_flush_method():
    """``writer.flush()`` 显式调 stream.flush。"""
    mock_stream = MagicMock()
    writer = FlushingStdoutWriter(mock_stream)
    writer.flush()
    mock_stream.flush.assert_called_once()


def test_flushing_writer_with_real_bytesio():
    """真实 BytesIO 验证写入正确。"""
    buf = io.BytesIO()
    writer = FlushingStdoutWriter(buf)
    run_async(writer.write(b"hello"))
    assert buf.getvalue() == b"hello"


# ── 3. 不引用 elicitation / progress API（SPEC §0.1 第六条）──────────────────


def test_no_elicitation_or_progress_references():
    """``orca/iface/mcp/`` 下不引用 elicitation / progress_notification API（§0.1 第六条）。"""
    mcp_dir = Path(__file__).resolve().parents[3] / "orca" / "iface" / "mcp"
    pattern = re.compile(r"elicit|progress_notification|send_progress", re.IGNORECASE)
    hits: list[str] = []
    for py_file in mcp_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'"):
                continue
            if pattern.search(line) and "elicitation" not in stripped.lower():
                if not _in_docstring(text, line_no):
                    hits.append(f"{py_file.name}:{line_no}: {line}")
    assert not hits, f"违反 SPEC §0.1 第六条，发现客户端能力引用：{hits}"


def _in_docstring(text: str, target_line: int) -> bool:
    """粗略判断 target_line 是否在三引号 docstring 内（用三引号计数奇偶）。"""
    count = 0
    for line_no, line in enumerate(text.splitlines(), 1):
        if line_no >= target_line:
            break
        count += line.count('"""')
    return count % 2 == 1


# ── 4. OrcaMcpServer 构造（D3 加 list_tools 测）──────────────────────────────


def test_server_constructs_with_manager():
    """OrcaMcpServer 构造后 _mcp 实例就位（D2 骨架验证；D3 加 tool 注册测）。"""
    m = RunManager()
    server = OrcaMcpServer(m)
    assert server._mcp is not None
    assert server._mcp.name == "orca"
