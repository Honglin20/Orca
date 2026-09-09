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
import os
import sys
import tempfile
import time
from pathlib import Path

# sha1(run_id) 取前 10 hex（40 bit）——同机并发 run 碰撞概率可忽略；完整 run_id 不入路径
# （run_id 含 wf 名 + 时间戳，可能很长）。10 hex + ``orca-`` prefix + ``.sock`` ≈ 21 字节，
# 远低于 SOCK_PATH_MAX。
_SOCK_HASH_LEN = 10

IS_WINDOWS = sys.platform == "win32"


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


def chart_port_file_path(sock_path: Path | str) -> Path:
    """Windows TCP 模式的端口 sidecar 文件：``<sock_path>.port``（同 tmp 目录同 stem）。

    Windows 无 Unix socket（Python 不暴露 ``AF_UNIX``）→ ingestor 改听 TCP
    ``127.0.0.1:<临时端口>``；端口 bind 期才确定，写入本文件供两端（env 注入 / liveness
    探测）读取。路径由 sock 路径派生（``with_suffix(".port")``），run_id 派生确定性不变。
    """
    return Path(sock_path).with_suffix(".port")


def write_chart_port_file(port_file: Path, port: int) -> None:
    """原子写端口文件（tmp + ``os.replace``，防 reader 读到半截）。

    - **tmp 名带 pid**（spec 2026-09-08 D1 修订）：双 daemon 稳态（liveness 保守 false →
      respawn 是架构接受的常态）下，两个写者对固定 tmp 名交错写会互踩（一方 replace 掉
      另一方刚写的 tmp → FileNotFoundError）；per-pid tmp 互不干扰。
    - **``os.replace`` 重试 1 次**：Windows 上 reader（``chart_endpoint`` 的 ``read_text``）
      恰好持句柄期间 replace 抛 ``PermissionError`` → 短暂等待后重试一次，仍失败则
      raise（fail loud，由 ingestor crash 熔断兜底，不无限循环）。
    """
    tmp = port_file.with_suffix(f".port.{os.getpid()}.tmp")
    tmp.write_text(str(port), encoding="utf-8")
    try:
        os.replace(tmp, port_file)
    except PermissionError:
        time.sleep(0.05)
        try:
            os.replace(tmp, port_file)
        except OSError:
            tmp.unlink(missing_ok=True)  # best-effort 清残留 tmp（不掩盖原异常）
            raise


def chart_endpoint(sock_path: Path | str) -> str:
    """ingestor 连接端点字符串（executor env 注入 + liveness 探测共用单一真相源）。

    - POSIX：socket 文件绝对路径（现状不变，``ORCA_CHART_SOCK`` 契约不变）。
    - Windows：``tcp://127.0.0.1:<port>``；端口文件缺失/损坏 → ``""``（调用方不注 env，
      script 端按既有「缺 ORCA_CHART_SOCK」fail loud 语义报错，无新增静默路径）。
    """
    sp = Path(sock_path)
    if not IS_WINDOWS:
        return str(sp.resolve())
    try:
        port = int(chart_port_file_path(sp).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return ""
    return f"tcp://127.0.0.1:{port}"


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

