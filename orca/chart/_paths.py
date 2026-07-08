"""_paths.py —— chart 传输路径派生（phase-13 §7.7 短路径化，2026-07-08）。

回答「chart ingestor 的 Unix socket 放哪？」：``/tmp/orca-<sha1(run_id)[:10]>.sock``。

**为什么不在 ``runs/<run_id>.sock``**：runs 目录可能是深绝对路径（服务器长 prefix，
如 ``/usr1/.../Orca-phase13-in-session-v8/runs/``），叠加 run_id 后超 ``sun_path``
（macOS 104 / Linux 108 字节）→ ``bind`` 抛 ``OSError``。socket 是**传输通道**（不持久化
状态，run 结束删除），与 tape/jsonl 的存放位置无关，故移到系统短路径 temp 目录，彻底
消除长度敏感性。tape / jsonl / prompts 仍在 runs 目录不变。

**两端同源**：服务端（``run_manager`` → ``chart_ingestor`` bind）与客户端 env
（``script.py`` / ``claude/executor.py`` → ``ORCA_CHART_SOCK``）都调本函数，由 run_id
派生同一短路径（hash 确定性），保证 bind/connect 寻址一致。

**依赖单向**：本模块仅依赖 stdlib（hashlib/tempfile/pathlib），是 chart 包底层，可被
``events/chart_ingestor`` / ``exec/*`` / ``iface/web/run_manager`` 安全 import。
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

# sha1(run_id) 取前 10 hex（40 bit）——同机并发 run 碰撞概率可忽略；完整 run_id 不入路径
# （run_id 含 wf 名 + 时间戳，可能很长）。10 hex + ``orca-`` prefix + ``.sock`` ≈ 21 字节，
# 远低于 SOCK_PATH_MAX。
_SOCK_HASH_LEN = 10


def chart_sock_path(run_id: str) -> Path:
    """chart ingestor Unix socket 路径：``<tmp>/orca-<sha1(run_id)[:10]>.sock``。

    确定性：同一 run_id 两端算出同一路径（hash 单向稳定）。
    短：base = ``tempfile.gettempdir()``（/tmp 或 $TMPDIR），总长短于 ``SOCK_PATH_MAX``。

    Args:
        run_id: Orca run 标识（``<wf_name>-<ts>-<hex>``）。

    Returns:
        绝对 socket 路径（parent 一定存在——temp 目录）。调用方负责 bind 前 unlink stale、
        run 结束 unlink。
    """
    short = hashlib.sha1(run_id.encode("utf-8")).hexdigest()[:_SOCK_HASH_LEN]
    return Path(tempfile.gettempdir()) / f"orca-{short}.sock"
