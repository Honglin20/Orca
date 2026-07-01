"""transport.py —— stdio writer flush 兜底（SPEC phase-10 §4.1 / §0.1 第四条）。

回答「opencode 在 stdio 批量发不 flush 的 bug (#21516) 怎么躲？」：每条 JSON-RPC 消息
写完显式 ``flush()``。即便 SDK 未来某版本忘了 flush，Orca 不受影响。

**flush 决策（2026-07-01 调研 mcp SDK 1.27.2）**：

    mcp.server.stdio.stdio_server 内部 ``stdout_writer`` 协程已 ``await stdout.flush()``
    每写一行（源码已确认）。SDK 默认 ``stdout = anyio.wrap_file(sys.stdout)``，
    包装的是底层 ``sys.stdout.buffer``。

    所以本类不是「兜底空 wrapper」——它是**纵深防御 belt-and-suspenders**：

    1. SDK 自己的 flush 已 active（主防线）。
    2. 本类在 ``write()`` 里**再次** flush（次防线），用于「走我们 wrapper 的写入路径」
       时不依赖 SDK 是否记得 flush。
    3. 调用方可用 ``stdio_server(stdout=our_wrapped_stream)`` 注入我们包装过的流，
       或直接拿来包任意 file-like（单测 mock）。

    不替换 SDK 的 ``stdio_server``——只提供可注入的 wrapper + 给测试用 mock 验证 flush
    次数。SPEC §4.4 客户端差异隔离在 transport.py，不污染 server 工具逻辑。

依赖单向：本模块**零 Orca 依赖**（纯 stdlib typing），任何模块可 import。
"""

from __future__ import annotations

from typing import Protocol


class _WritableStream(Protocol):
    """file-like 写流协议（``sys.stdout.buffer`` / ``io.BytesIO`` / mock 都满足）。"""

    def write(self, data: bytes) -> int: ...
    def flush(self) -> None: ...


class FlushingStdoutWriter:
    """每写一次就 flush 的 stdout 包装（SPEC §4.1）。

    用法 1（注入 stdio_server）::

        wrapped = FlushingStdoutWriter(sys.stdout.buffer)
        async with stdio_server(stdout=anyio.wrap_file(wrapped), ...):
            ...

    用法 2（单测 mock 验证 flush 次数）::

        buf = io.BytesIO()
        writer = FlushingStdoutWriter(buf)
        await writer.write(b"x")
        assert buf.write.call_count == 1  # 若 buf 是 MagicMock

    设计：``write`` 是 async（匹配 anyio 流的 async write 协议，让 ``anyio.wrap_file``
    可直接包）。``flush`` 同步（``sys.stdout.buffer.flush`` 本就同步）。
    """

    def __init__(self, stream: _WritableStream) -> None:
        self._stream = stream

    async def write(self, data: bytes) -> None:
        """写字节 + 立即 flush（每条 JSON-RPC 消息一行，flush 保证 opencode 收到）。"""
        self._stream.write(data)
        self._stream.flush()

    def flush(self) -> None:
        """显式 flush（兼容任何调用方主动 flush 的代码路径）。"""
        self._stream.flush()
