"""_paths.py —— run 派生路径集中（chart socket + workflow 产物目录）。

回答两个「由 run_id 派生的路径放哪？」：

1. **chart ingestor socket**（phase-13 §7.7 短路径化，2026-07-08）：
   ``/tmp/orca-<sha1(run_id)[:10]>.sock``。**为什么不在 ``runs/<run_id>.sock``**：runs 目录
   可能是深绝对路径（服务器长 prefix），叠加 run_id 后超 ``sun_path``（macOS 104 / Linux 108
   字节）→ ``bind`` 抛 ``OSError``。socket 是**传输通道**（不持久化状态，run 结束删除），与
   tape/jsonl 的存放位置无关，故移到系统短路径 temp 目录。两端（``run_manager`` bind +
   ``script.py`` / ``claude/executor.py`` env 注入）同源，run_id 派生确定性短路径。

2. **workflow 产物权威目录**（P8 / plan 2026-07-21 §Phase 4-A）：
   ``<runs_dir>/<run_id>/artifacts/``。引擎注入单一目录给 workflow 用，消除 workflow 自建
   ``llm_artifacts/<model>/...`` 与引擎管的 ``runs/<run_id>/`` 两套 run_id 不合流。bootstrap
   ``mkdir -p`` 创建；workflow 脚本据 ``$ORCA_ARTIFACTS_DIR`` 写产物。

**单一真相源**：两个路径都在**本模块**定义，被 ``events/chart_ingestor`` / ``exec/*`` /
``iface/web/run_manager`` / ``iface/in_session/cli.py``（bootstrap + gc）共同 import。

**依赖单向**：本模块仅依赖 stdlib（hashlib/tempfile/pathlib），是 chart 包底层（实际是
「run 派生路径」的通用底层，模块名沿用 chart 历史命名），可被 exec/iface/events/run 各层
安全 import（无反向依赖）。
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


def artifacts_dir_for_run(runs_dir: Path | str, run_id: str) -> Path:
    """run 产物权威目录：``<runs_dir>/<run_id>/artifacts/``（P8 / plan 2026-07-21 §Phase 4-A）。

    确定性：同一 (runs_dir, run_id) 多端算出同一路径（in-session env 文件 / 后端 spawn overlay
    / gc 清理路径同源）。绝对/相对皆可：返回 ``Path(runs_dir) / run_id / "artifacts"``，路径
    形态由调用方决定（``_write_orca_env`` / executor spawn / gc 都 ``resolve()`` 成绝对后用）。

    Args:
        runs_dir: tape 文件所在目录（``runs/``，与 ``bg_runner.default_tape_path`` 同源）。
        run_id: Orca run 标识（``<wf_name>-<ts>-<hex>``）。

    Returns:
        ``<runs_dir>/<run_id>/artifacts/`` Path。**不** ``mkdir`` —— 调用方（bootstrap）按需
        ``mkdir(parents=True, exist_ok=True)`` 创建（本函数是纯路径派生，无副作用）。

    约定：``runs_dir`` 一般是相对 ``runs/``（CWD 下），但生产路径调用方 ``resolve()`` 后注 env，
    让 workflow 脚本拿到的 ``$ORCA_ARTIFACTS_DIR`` 是绝对路径（避免 subagent 切目录后相对路径
    漂移—— 与 ``ORCA_CHART_SOCK`` / ``ORCA_AGENT_RESOURCES`` 同 resolve 契约）。
    """
    return Path(runs_dir) / run_id / "artifacts"

