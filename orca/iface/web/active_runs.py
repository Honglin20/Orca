"""active_runs.py —— in-session YOLO 兜底路由（SPEC 2026-08-07 §3.2）。

回答「session 未注册进 registry 时，broker 怎么找到该 session 所属的活跃 run？」：
扫 runs 目录的 ``orca-<run_id>.json`` marker → 读对应 ``<run_id>.jsonl`` tape →
双键匹配（首行 ``data.host_session`` / 任一事件顶层 ``session_id``）→ 命中返回 run_id。

设计要点（SPEC §2.1–§2.4）：
  - **活跃 = marker 存在 且 tape 存在 且 tape 末行非终态事件**
    （``workflow_completed`` / ``workflow_failed`` / ``workflow_cancelled``，
    防 kill -9 / 断电残留的 stale marker 把已死 run 当活跃，yolo-allow 扩面）。
  - **双键匹配**：host 键走首行 ``data.host_session``（bootstrap 即写入，无竞态），
    node 键走全部事件顶层 ``session_id``（覆盖子代理 id 路径，实证宿主≠子代理）。
  - **多 run 命中**：取 marker mtime 最新者 + warning（fail loud，不静默 ids[0]）；
    mtime 平局按 run_id 字典序取最小（确定性）。
  - **per-run 缓存**：键 = ``(tape path, mtime_ns, size, marker 存在性)`` →
    ``{host_session, node_session_ids}``；键变化即失效，容量有界。
  - **fail-soft**：坏行 / data 非 dict / 首行截断 / 半写 → 跳过 + warn，不崩；
    ``host_session=null`` 仍走 node 扫描。
  - **异常语义**：resolver 内部 catch（含 ``RegistryCorruptError`` / ``OSError``）→
    warning → 视为未命中（``native-fallback ask``），禁止传播到 ``create_app``。
  - **调用期枚举**：工厂 ``build_active_run_resolver()`` 零 IO；每次调用时枚举
    ``resolve_runs_dir()`` + ``list_registered()`` 的 runs 目录（多项目全覆盖）。

依赖单向（SPEC §3.2 / AC4）：仅依赖 ``orca.runtime``（public re-export）+
``orca.iface.in_session.marker`` + stdlib。零 run/tape*/exec/events.bus/gates.handler
依赖（结构化 import 守门）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable, Iterable

from orca.iface.in_session.marker import read_marker
from orca.runtime import (
    RUNS_DIRNAME,
    RegistryCorruptError,
    list_registered,
    resolve_runs_dir,
)

logger = logging.getLogger(__name__)

# 终态事件集合（SPEC §2.3 第二守卫）：末行为其一 → 视为不活跃。
_TERMINAL_TYPES: frozenset[str] = frozenset(
    {"workflow_completed", "workflow_failed", "workflow_cancelled"}
)

# 向后 seek 读末行的块大小；索引缓存容量上限（防无界内存）。
_TAIL_CHUNK = 64 * 1024
_CACHE_MAX_ENTRIES = 512

# per-run tape 索引缓存：键 = (tape path, mtime_ns, size, marker 存在性)。
_tape_cache: dict[
    tuple[str, int, int, bool], tuple[str | None, frozenset[str]]
] = {}


def _read_last_line(path: Path) -> str | None:
    """读取文件最后一行（向后 seek，仅读尾部块，O(末尾) 而非全量读）。

    空文件 / 读失败 → None。文件尾换行忽略（``"a\\nb\\n"`` → ``"b"``）。
    """
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return None
            pos = size
            buf = b""
            while True:
                take = min(_TAIL_CHUNK, pos)
                pos -= take
                f.seek(pos)
                buf = f.read(take) + buf
                text = buf.decode("utf-8", errors="replace")
                lines = text.splitlines()
                if len(lines) >= 2:
                    # 已读到完整最后一行（含其行首换行）。
                    return lines[-1]
                if pos == 0:
                    return lines[-1] if lines else None
    except OSError as e:
        logger.warning("active-run tape 末行读取失败 %s: %s", path, e)
        return None
    return None


def _last_event_type(raw: str | None) -> str | None:
    """末行 JSON 的 ``type`` 字段；半写/坏行/非 dict → None（视为非终态 → 活跃）。"""
    if not raw:
        return None
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    return event_type if isinstance(event_type, str) else None


def _index_tape(tape_path: Path) -> tuple[str | None, frozenset[str]]:
    """扫描 tape：首行 ``data.host_session`` + 全部事件顶层 ``session_id``。

    fail-soft：坏行 / data 非 dict / 首行截断 → 跳过 + warn，不崩；
    ``host_session`` 仅当非空 str 才记录（null/缺键 → 不参与 host 键，仍走 node 扫描）。
    """
    host_session: str | None = None
    node_sessions: set[str] = set()
    try:
        with tape_path.open("r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError as e:
                    logger.warning(
                        "active-run tape 坏行跳过 %s:%d（%s）", tape_path, lineno, e,
                    )
                    continue
                if not isinstance(event, dict):
                    logger.warning(
                        "active-run tape 行非 object 跳过 %s:%d", tape_path, lineno,
                    )
                    continue
                sid = event.get("session_id")
                if isinstance(sid, str) and sid:
                    node_sessions.add(sid)
                if lineno == 1:
                    data = event.get("data")
                    if isinstance(data, dict):
                        hs = data.get("host_session")
                        if isinstance(hs, str) and hs:
                            host_session = hs
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("active-run tape 读取失败 %s: %s", tape_path, e)
        return None, frozenset()
    return host_session, frozenset(node_sessions)


def _tape_index(
    tape_path: Path, marker_exists: bool,
) -> tuple[str | None, frozenset[str]]:
    """带缓存的 tape 索引：键 = (path, mtime_ns, size, marker 存在性) → 索引。

    marker 存在性由调用方显式传入并计入键（SPEC §2.3：marker 增删强制计入缓存键），
    防终态 run 被缓存误路由；容量超限整体清空（有界内存，approval 低频）。
    当前调用点仅在 marker 存在且非终态时建索引，故传入 ``True``——若未来复用于
    无 marker 路径，必须传真实存在性，否则键不诚实、可能 stale 命中。
    """
    try:
        st = tape_path.stat()
    except OSError as e:
        logger.warning("active-run tape stat 失败 %s: %s", tape_path, e)
        return None, frozenset()
    key = (str(tape_path), st.st_mtime_ns, st.st_size, marker_exists)
    cached = _tape_cache.get(key)
    if cached is not None:
        return cached
    index = _index_tape(tape_path)
    if len(_tape_cache) >= _CACHE_MAX_ENTRIES:
        _tape_cache.clear()
    _tape_cache[key] = index
    return index


def resolve_session_to_active_run(
    session_id: str,
    runs_dirs: Iterable[Path],
) -> str | None:
    """核心纯函数：扫 marker → 终态第二守卫 → 读 tape → 双键匹配 → 最新者。

    返回命中的 ``run_id`` 或 ``None``（未命中）。坏数据 fail-soft 内部消化，不抛。
    """
    candidates: list[tuple[str, int]] = []  # (run_id, marker mtime_ns)
    seen_dirs: set[str] = set()
    for runs_dir in runs_dirs:
        try:
            resolved = Path(runs_dir).resolve()
        except (OSError, RuntimeError) as e:
            logger.warning("active-run runs dir resolve 失败跳过 %s（%s）", runs_dir, e)
            continue
        if str(resolved) in seen_dirs or not resolved.is_dir():
            continue
        seen_dirs.add(str(resolved))
        for marker_path in sorted(resolved.glob("orca-*.json")):
            try:
                marker_mtime = marker_path.stat().st_mtime_ns
            except OSError as e:
                logger.warning(
                    "active-run marker stat 失败跳过 %s（%s）", marker_path, e,
                )
                continue
            try:
                marker = read_marker(marker_path)
            except (OSError, UnicodeDecodeError) as e:
                # read_marker 内部已容错 JSON/OSError；此处兜底非法编码等逃逸异常，
                # 保证单个坏 marker 只跳过自身、不中断整轮扫描（per-marker fail-soft）。
                logger.warning(
                    "active-run marker 读取异常跳过 %s（%s）", marker_path, e,
                )
                continue
            if marker is None:
                # 半写 / 损坏 / 缺 run_id：拿不到 run_id 即无法定位 tape，跳过 + warn。
                logger.warning("active-run marker 不可读跳过 %s", marker_path)
                continue
            run_id = marker.run_id
            tape_path = resolved / f"{run_id}.jsonl"
            if not tape_path.is_file():
                logger.warning(
                    "active-run orphan marker（tape 缺失）跳过 %s", marker_path,
                )
                continue
            if _last_event_type(_read_last_line(tape_path)) in _TERMINAL_TYPES:
                continue  # 终态第二守卫：stale marker + 终态 tape → 不活跃。
            host_session, node_sessions = _tape_index(tape_path, marker_exists=True)
            if host_session == session_id or session_id in node_sessions:
                candidates.append((run_id, marker_mtime))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[1], item[0]))  # mtime 最新；平局 run_id 最小。
    if len(candidates) > 1:
        logger.warning(
            "active-run 多 run 命中 session=%s，取最新 %s（候选=%s）",
            session_id, candidates[0][0], [run for run, _ in candidates],
        )
    return candidates[0][0]


def _enumerate_runs_dirs() -> list[Path]:
    """调用期枚举 runs 目录：``resolve_runs_dir()`` + 注册表全项目 ``<root>/runs``。

    注册表损坏 → warn + 忽略该来源（不阻断 cwd/env 路径的枚举）。
    """
    dirs: list[Path] = []
    try:
        dirs.append(resolve_runs_dir())
    except ValueError as e:
        logger.warning("active-run resolve_runs_dir 失败，跳过该来源：%s", e)
    try:
        registered = list_registered()
    except RegistryCorruptError:
        logger.warning(
            "active-run 注册表损坏，跳过 registered 枚举", exc_info=True,
        )
        registered = {}
    for meta in registered.values():
        root = meta.get("path")
        if isinstance(root, str) and root:
            dirs.append(Path(root) / RUNS_DIRNAME)
    return dirs


def build_active_run_resolver(
    runs_dirs: Iterable[Path] | None = None,
) -> Callable[[str], str | None]:
    """工厂：返回 ``Callable[[session_id], run_id | None]``。

    - ``runs_dirs=None``：**调用期**枚举 ``resolve_runs_dir()`` + ``list_registered()``
      的 runs 目录（工厂期零 IO，不传播注册表损坏到 ``create_app``）。
    - 显式传入 ``runs_dirs``：跳过枚举（测试注入 / 定制扫描面）。
    - 返回的 resolver 同步调用（approval 低频 + 缓存兜底，SPEC §3.1 首版不做线程化）；
      任何异常内部 catch → warning → 视为未命中（ask），不炸 ``create_app``。
    """

    def _resolve(session_id: str) -> str | None:
        dirs = list(runs_dirs) if runs_dirs is not None else _enumerate_runs_dirs()
        try:
            return resolve_session_to_active_run(session_id, dirs)
        except Exception:  # noqa: BLE001 —— 兜底边界：异常 → 未命中（ask），fail loud 靠 warning
            logger.warning(
                "active-run resolver 异常，按未命中处理 session=%s", session_id,
                exc_info=True,
            )
            return None

    return _resolve
