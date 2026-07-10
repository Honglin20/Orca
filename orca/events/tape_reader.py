"""tape_reader.py —— read-only 流式解析外部 tape 文件（SPEC web-attach §0 D2 / §2.2）。

回答「为什么不直接 ``Tape(resume=True)``？」「attacher 绝不能抢外部写者的 flock——
``Tape(resume=True)`` 是 append 模式（重开同 path），会污染 seq + 抢 advisory lock」。
故 attacher 走本模块：``open(mode="r")`` 纯只读，逐行 parse → yield ``Event``。

设计规则（SPEC 铁律 6 / §0 D2）：
  - **纯只读**：永不 ``open(mode="a")`` / 不 ``Tape(resume=True)`` / 不 ``flush`` / 不 ``unlink``。
  - **容忍末尾残行**：tape 写者可能刚 flush 半行；末尾非 JSON 行 → 视为残行停止 yield
    （不抛、不截断——只读没有改写文件的权限）。中间残行记 warning 跳过继续。
  - **since_seq 过滤**：``event.seq > since_seq`` 才 yield（与 ``Tape.replay`` 语义一致，
    让 WS resume / GET /events?since 复用同一路径）。
  - **耗尽语义**：生成器持文件句柄；调用方需完整迭代或用 ``contextlib.aclosing``。

依赖单向：只依赖 ``orca.schema``（Event），不 import ``Tape``（避免任何写路径复用）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

from orca.schema import Event

logger = logging.getLogger(__name__)


def replay(path: Path | str, *, since_seq: int = 0) -> Iterator[Event]:
    """纯只读流式解析 tape 文件，yield seq > since_seq 的 Event（SPEC §0 D2 / §2.2）。

    - 末尾 partial 行（写者刚 flush 一半）→ 停止 yield（不抛、不截断）。
    - 中间行非 JSON / 非 Event → 记 warning 跳过继续（fail-soft 读，与 Tape.replay 对齐）。
    - 文件不存在 → 直接 return（空迭代；调用方决定 404 / live-pending）。

    AC（SPEC §8.13）：本模块及其调用方 grep 不得出现 ``Tape(resume=True)``。
    """
    p = Path(path)
    if not p.exists():
        return
    with open(p, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                # 末尾残行（写者未 flush 完整行）→ 停止 yield；中间残行罕见（append 是整行写），
                # 但理论上可能（崩溃中间态）。残行视为「读到这里为止」，不再继续——避免把
                # partial JSON 当 Event 喂给 follow task / GET /events。
                logger.warning(
                    "tape_reader %s 第 %d 行非合法 JSON，停止 yield（read-only 不截断）",
                    p,
                    lineno,
                )
                return
            try:
                event = Event(**obj)
            except Exception as e:  # pydantic 校验失败
                logger.warning(
                    "tape_reader %s 第 %d 行无法解析为 Event：%s，跳过",
                    p,
                    lineno,
                    e,
                )
                continue
            if event.seq <= since_seq:
                continue
            yield event


def count_and_bounds(path: Path | str) -> tuple[int, int, int]:
    """纯只读扫一遍 tape，返回 ``(event_count, oldest_seq, newest_seq)``。

    - event_count：成功 parse 的整行数（不含 partial 末尾）。
    - oldest_seq / newest_seq：seq 的 min / max（无事件 → 0 / 0）。

    用于 ``GET /api/runs/<id>/meta``（perf 路径：单次扫文件 O(N)，不构造 Tape）。
    """
    p = Path(path)
    if not p.exists():
        return (0, 0, 0)
    count = 0
    oldest = 0
    newest = 0
    with open(p, "r", encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
                seq = int(obj["seq"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                # 残行：停止扫（与 replay 语义一致）
                break
            count += 1
            if oldest == 0 or seq < oldest:
                oldest = seq
            if seq > newest:
                newest = seq
    return (count, oldest, newest)


def tail_events(path: Path | str, n: int) -> list[Event]:
    """反向扫文件取最后 n 个 Event（SPEC web-attach §8.4b perf AC）。

    - 从文件尾按字节块（64KB）反扫，找 ``n+1`` 个 ``\\n`` 即停（O(file_size_of_last_n_lines)，
      与 tape 总大小无关——50MB tape 上仍只读尾部 ~50KB）。
    - 然后从切点 parse 到 EOF，过滤 partial 末行，按 seq 升序返回。
    - ``n <= 0`` → 空列表；文件不存在 → 空列表。

    用于 ``GET /events?tail=M``（避免 50MB tape 全量 list 物化，BLOCKER 1 修复）。
    """
    if n <= 0:
        return []
    p = Path(path)
    if not p.exists():
        return []
    file_size = p.stat().st_size
    if file_size == 0:
        return []

    # 反向读 64KB 块直到找到 n+1 个 newline（含 EOF 处的可能 newline）
    chunk_size = 64 * 1024
    newline_count = 0
    read_offset = file_size
    tail_bytes = b""
    while read_offset > 0 and newline_count < n + 1:
        read_len = min(chunk_size, read_offset)
        read_offset -= read_len
        with open(p, "rb") as bf:
            bf.seek(read_offset)
            block = bf.read(read_len)
        tail_bytes = block + tail_bytes
        newline_count = tail_bytes.count(b"\n")
        if read_offset == 0:
            break

    # tail_bytes 现含足够内容；decode + split，跳过 partial 末行（若有），取最后 n
    try:
        text = tail_bytes.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        text = tail_bytes.decode("utf-8", errors="ignore")
    # 顶部块可能切在一行中间——丢首段（直到第一个 \n）
    if read_offset > 0:
        # 仅当不是从文件头开始，才丢首段（避免丢合法首行）
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl + 1 :]
    lines = text.split("\n")
    # 末尾若为空串（文件以 \n 结尾）则去掉
    if lines and lines[-1] == "":
        lines.pop()
    # 末尾 partial 行（无 \n 终结且解析失败）→ 丢
    if lines:
        try:
            json.loads(lines[-1])
        except (json.JSONDecodeError, ValueError):
            lines.pop()
    # 取最后 n 行 parse
    take = lines[-n:] if len(lines) > n else lines[:]
    events: list[Event] = []
    for raw in take:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
            events.append(Event(**obj))
        except (json.JSONDecodeError, Exception):  # noqa: BLE001
            continue
    # seq 升序
    events.sort(key=lambda e: e.seq)
    return events


def since_limited(path: Path | str, since_seq: int, limit: int) -> list[Event]:
    """正向扫，``event.seq > since_seq`` 的前 ``limit`` 个（提前 break，避免全量物化）。

    用于 ``GET /events?since=N&limit=M``（BLOCKER 1 修复——避免 list(generator)[:limit]）。
    """
    if limit <= 0:
        return []
    out: list[Event] = []
    for event in replay(path, since_seq=since_seq):
        out.append(event)
        if len(out) >= limit:
            break
    return out
