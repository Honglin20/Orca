"""cc_jsonl.py —— Claude Code sidechain jsonl 读 adapter（SPEC-B v4 §4）。

**回答的问题**：CC 子 agent 的过程事件存在哪、怎么读？答：CC 把每个子 agent 的完整 transcript
写到 ``~/.claude/projects/<encoded-cwd>/<host_session>/subagents/agent-<task_id>.jsonl``，
每行是一条完整的 stream-json 消息（assistant / user / system / result / stream_event）。
本 adapter 把这些行映射成 ``RawAgentEvent``（payload 1:1 = EventType.data）。

**行映射（spike 实测 + claude_translator 同源 stream-json 协议，SPEC §4）**：
  - ``assistant`` 行 + content block ``thinking`` → ``thinking`` payload ``{text}``
  - ``assistant`` 行 + content block ``tool_use`` → ``tool_call`` payload
    ``{tool:name, args:input, tool_call_id:id}``
  - ``assistant`` 行 + content block ``text``     → ``text`` payload ``{text}``
  - ``user`` 行 + content block ``tool_result``   → ``tool_result`` payload
    ``{tool_call_id:tool_use_id, result:content}``（content 归一为 str，截断到上限）
  - ``user``（非 tool_result）/``attachment``     → skip
  - ``stream_event`` / ``system`` / ``result``    → skip（增量片段 / 心跳 / usage；B2 scope 外）
  - 其它未知 type                                  → skip（不抛，同 translator 容错）

**source_id** = ``f"{task_id}:{line_idx}:{block_idx}"``：
  - ``task_id`` 来自文件名（``agent-<task_id>.jsonl``）= ``child_id``。假设 ``task_id`` 不含
    ``:``（CC task_id 实证为 UUID/hex，无 ``:``）；若未来 CC 改用含 ``:`` 的 id 格式，需切换
    分隔符或换 source_id 结构。
  - ``line_idx`` 是该文件内的行号（0-based），跨 restart 稳定（jsonl append-only）。
  - ``block_idx`` 是该行 content[] 内的 block 序号（一行可能多 block，必须 disambiguate）。
  SPEC §4 字面是 ``f"{agentId}:{line_idx}"``（单 block 假设）；扩展到 block_idx 保证多 block
  唯一性，是必要修正（adapter 责任，ingestor 无感知）。

**cursor** = byte offset：``stream`` 时 ``seek(cursor)`` 跳过已读字节，readline 增量推进。
partial-line race 防护：只 yield 完整 ``\\n`` 终止的行；partial 留下次重读（同
``chart_daemon._watch_terminal`` / ``_FlockSafeTape._read_max_seq_from_disk``）。

**scope 铁律**：``discover_children`` 只 glob **本 host_session 的** ``subagents/`` 目录；
禁跨 session 扫（硬约束 #3）。``host_session`` 由调用方传（从 tape
``workflow_started.data.host_session`` 派生，与 U1 同源）。

**显式 spawn 过滤**：``discover_children`` 只 yield 伴有 ``agent-<task_id>.meta.json`` 的子代理
（主 session Agent tool 显式 spawn 才有 meta.json；宿主后台系统子代理如 CAC ``asession_memory-*``
无 meta.json → 跳过，防污染节点 tape）。详见方法 docstring。

**fail loud**：
  - 构造时 ``host_session`` 为空 → raise ``CCAdapterError``（无 session id 无法定位目录；
    daemon 主体 catch 后 CRITICAL log + exit）。
  - root 目录不存在 → ``discover_children`` 返空迭代器（不抛；subagent 尚未起）。
  - 单行解析失败 → 静默跳过（不阻塞，同 ``Tape.replay`` 容错）。

**测试覆盖**：``ORCA_CC_SIDECHAIN_ROOT`` env 覆盖 root（不依赖 ``~/.claude``；硬约束 #5 注明）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterator

from orca.events.adapters._family import (
    _encode_cwd,  # re-export：既有 ``from cc_jsonl import _encode_cwd`` 零回归
    resolve_cc_sidechain_root,
)
from orca.events.raw_agent_event import ChildRef, Cursor, RawAgentEvent

logger = logging.getLogger(__name__)

# 与 claude_translator 同源：tool_result.content 截断上限（防异常工具输出喷爆事件流）。
_TOOL_RESULT_MAX_CHARS = 4096


class CCAdapterError(RuntimeError):
    """CC adapter 配置/路径错误（fail loud；daemon 主体 catch 后 CRITICAL log + exit）。"""


def _resolve_sidechain_root(
    host_session: str, *, cwd: str | None = None, family: str | None = None,
) -> Path:
    """解析 sidechain root（SPEC §P4：委托 ``_family.resolve_cc_sidechain_root``）。

    解析顺序（resolver 内部，本函数仅丢弃 source 标记返路径）：
      1. ``ORCA_CC_SIDECHAIN_ROOT`` env（测试覆盖；绝对路径，直接用）
      2. ``family`` 显式（"cc"/"cac"，由 daemon argv ``--family`` 透传）→ source="config"
      3. 探测 ``.cac`` / ``.claude``（歧义默认 .claude）→ source="probe"
      4. 默认 ``.claude`` → source="default"

    Args:
        host_session: 宿主 CC session id（env 路径下可空；其它路径下空 → raise）。
        cwd: 当前工作目录（默认 ``os.getcwd()``）；用于派生 ``<encoded-cwd>``。
        family: 显式家族（"cc"/"cac"），覆盖探测；None → 走探测。daemon 从 config
            ``sidechain.family`` 读入透传；adapter 自身不读 config（events 层依赖铁律）。

    Returns:
        sidechain root 目录 ``Path``（**不保证存在**；调用方 ``is_dir`` 判定）。

    Raises:
        CCAdapterError: ``host_session`` 为空（非 env 路径下）；或 ``family`` 非法。
    """
    try:
        root, _source = resolve_cc_sidechain_root(
            host_session, cwd=cwd, family=family,
        )
    except ValueError as e:
        # 包装成 CCAdapterError（保留 fail loud 语义 + 既有 ``pytest.raises(CCAdapterError)`` 测试）。
        raise CCAdapterError(str(e)) from e
    return root


class CCJsonlAdapter:
    """CC sidechain jsonl 读 adapter（实现 ``ReadAdapter`` 协议）。

    无状态（除 cursor 游标在调用方 ``_SidechainDriver`` 内存 dict 持久化）。
    每次 ``stream`` 重新 open 文件 → seek 到 cursor → readline 增量读。

    线程安全：单 driver task 串行调（同 ``_SidechainDriver.run``）→ 无并发。
    """

    def __init__(
        self,
        host_session: str,
        *,
        cwd: str | None = None,
        root: Path | None = None,
        family: str | None = None,
    ) -> None:
        """Args:
            host_session: 宿主 CC session id（必非空，否则 ``stream``/``discover`` fail loud）。
            cwd: 当前工作目录（默认 ``os.getcwd()``）。``root`` 给定时忽略。
            root: 显式 sidechain root（测试用；覆盖 env / family / 派生）。
            family: 显式家族 ``"cc"`` / ``"cac"``（SPEC §P4）。``root`` 给定时忽略；
                否则透传给 ``resolve_cc_sidechain_root`` 覆盖探测（source="config"）。
                daemon 从 config ``sidechain.family`` 读入经 argv ``--family`` 透传；
                adapter 自身不读 config（events 层依赖铁律）。
        """
        self._host_session = host_session
        self._cwd = cwd
        self._family = family
        if root is not None:
            self._root = Path(root)
        else:
            # fail loud at construct：host_session 空 + 无 env → raise（无意义继续）。
            self._root = _resolve_sidechain_root(host_session, cwd=cwd, family=family)

    @property
    def root(self) -> Path:
        """观测：sidechain root 目录（测试用）。"""
        return self._root

    def discover_children(
        self, host_session: str, since_ts: int
    ) -> Iterator[ChildRef]:
        """glob ``<root>/agent-*.jsonl``，yield ``task_id``（= child_id）。

        scope：``host_session != self._host_session`` → 返空（本 adapter 绑定单一 host_session，
        不会跨 session；硬约束 #3）。``since_ts`` 用文件 mtime 过滤（可选优化）。

        目录不存在 → 返空迭代器（不抛；subagent 尚未起）。

        **显式 spawn 过滤**：只 yield 伴有 ``agent-<task_id>.meta.json`` 的子代理。主 session
        经 Agent tool 显式 spawn 的子代理，宿主会建 ``.meta.json``（agentType / description 等）；
        宿主后台自动 spawn 的系统子代理（如 CAC ``asession_memory-*`` memory helper）不经主
        session Agent tool，**无** ``.meta.json`` → 跳过，避免污染 workflow 节点 tape。因果证据：
        受污染 session 的主 session Agent tool_use 数 == 任务子代理数（系统代理不走此路）。CC
        （``.claude``）所有子代理都有 ``.meta.json`` → 本过滤为 no-op，零回归。

        **完整性**：daemon 对新 child 的 ``stream`` 从 cursor=0 读全文 → 即便 ``.meta.json`` 晚于
        jsonl 首行写入、discover 延迟若干 poll cycle 才 yield，首次 stream 仍读全文，**不丢事件**
        （仅延迟 ingest）。

        **失效升级路径**：若未来宿主给系统子代理也写 ``.meta.json``，改读其
        ``parent_session_id`` 判 ``== host_session``（parentage 语义比文件存在性更内禀）。
        """
        if host_session != self._host_session:
            # scope 铁律：只扫本 host_session。
            return
        if not self._root.is_dir():
            logger.debug(
                "CC sidechain root %s 不存在（subagent 可能尚未起）", self._root,
            )
            return
        for path in sorted(self._root.glob("agent-*.jsonl")):
            if since_ts > 0:
                try:
                    if path.stat().st_mtime < since_ts:
                        continue
                except OSError:
                    continue
            # agent-<task_id>.jsonl → task_id
            task_id = path.stem.removeprefix("agent-")
            if not task_id:
                continue
            # 显式 spawn 判据：只认伴有 meta.json 的子代理（见 docstring）。
            meta_path = self._root / f"agent-{task_id}.meta.json"
            if not meta_path.is_file():
                logger.debug(
                    "CC sidechain %s: 无 meta.json（非主 session 显式 spawn 的系统子代理），跳过",
                    task_id,
                )
                continue
            yield task_id

    def stream(
        self, child: ChildRef, cursor: Cursor
    ) -> Iterator[tuple[RawAgentEvent, Cursor]]:
        """从 byte ``cursor`` 起读 ``child`` jsonl 增量行 → 映射 → yield (event, new_cursor)。

        partial-line race 防护：末行若不以 ``\\n`` 结尾，不推进 cursor（下次重读同字节）。

        单行可能产 0/1/N 个事件（按 content blocks 数量）；每事件 source_id 唯一（含 block_idx）。
        """
        path = self._root / f"agent-{child}.jsonl"
        if not path.is_file():
            return  # 文件被删 / child 名错；静默返（不阻塞 driver）

        try:
            with open(path, "r", encoding="utf-8") as f:
                f.seek(cursor)
                line_idx = _count_lines(path, cursor)  # cursor=byte_offset → line_idx
                while True:
                    line = f.readline()
                    if not line:
                        return  # EOF
                    if not line.endswith("\n"):
                        # partial line；下次重读（不 tell 推进）。
                        logger.debug(
                            "CC sidechain %s: 行 %d partial（无 \\n），下次重读",
                            path.name, line_idx,
                        )
                        return
                    new_cursor = f.tell()
                    for raw in self._map_line(line, child, line_idx):
                        yield raw, new_cursor
                    line_idx += 1
                    cursor = new_cursor
        except OSError:
            logger.warning(
                "CC sidechain %s: 读失败（OSError）", path, exc_info=True,
            )
            return

    def _map_line(
        self, line: str, child_id: str, line_idx: int
    ) -> Iterator[RawAgentEvent]:
        """单行 stream-json → RawAgentEvent 列表。

        按 ``claude_translator`` 同款 top-level type 分派（assistant/user）；
        其他 type（stream_event/system/result）skip。
        """
        stripped = line.strip()
        if not stripped:
            return
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            # 完整行（含 \n）却解析失败 → 损坏行，跳过（不阻塞，同 Tape.replay 容错）。
            logger.debug(
                "CC sidechain agent-%s.jsonl 行 %d 非合法 JSON，跳过",
                child_id, line_idx,
            )
            return
        if not isinstance(obj, dict):
            return

        top_type = obj.get("type")
        if top_type == "assistant":
            yield from self._map_assistant(obj, child_id, line_idx)
        elif top_type == "user":
            yield from self._map_user(obj, child_id, line_idx)
        # stream_event / system / result / unknown → skip

    def _map_assistant(
        self, obj: dict, child_id: str, line_idx: int
    ) -> Iterator[RawAgentEvent]:
        """assistant 行：遍历 content[] blocks，按 block type 映射。

        - thinking → thinking{child_id, source_id, {text}}
        - tool_use → tool_call{child_id, source_id, {tool, args, tool_call_id}}
        - text     → text{child_id, source_id, {text}}
        """
        message = obj.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block_idx, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "thinking":
                text = block.get("thinking", "")
                if isinstance(text, str) and text:
                    yield RawAgentEvent(
                        child_id=child_id,
                        source_id=f"{child_id}:{line_idx}:{block_idx}",
                        kind="thinking",
                        payload={"text": text},
                    )
            elif block_type == "tool_use":
                yield RawAgentEvent(
                    child_id=child_id,
                    source_id=f"{child_id}:{line_idx}:{block_idx}",
                    kind="tool_call",
                    payload={
                        "tool": str(block.get("name", "")),
                        "args": block.get("input") or {},
                        "tool_call_id": str(block.get("id", "")),
                    },
                )
            elif block_type == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text:
                    yield RawAgentEvent(
                        child_id=child_id,
                        source_id=f"{child_id}:{line_idx}:{block_idx}",
                        kind="text",
                        payload={"text": text},
                    )

    def _map_user(
        self, obj: dict, child_id: str, line_idx: int
    ) -> Iterator[RawAgentEvent]:
        """user 行：仅 tool_result block 映射；非 tool_result（attachment 等）skip。

        result 字段归一为 str（同 ``claude_translator._normalize_tool_result_content``），
        截断到 ``_TOOL_RESULT_MAX_CHARS``（防喷爆）。
        """
        message = obj.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block_idx, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            tool_call_id = str(block.get("tool_use_id", ""))
            result_text = _normalize_tool_result_content(block.get("content"))
            if len(result_text) > _TOOL_RESULT_MAX_CHARS:
                result_text = result_text[:_TOOL_RESULT_MAX_CHARS] + "…[truncated]"
            yield RawAgentEvent(
                child_id=child_id,
                source_id=f"{child_id}:{line_idx}:{block_idx}",
                kind="tool_result",
                payload={
                    "tool_call_id": tool_call_id,
                    "result": result_text,
                },
            )


def _count_lines(path: Path, byte_cursor: int) -> int:
    """数 ``path`` 前 ``byte_cursor`` 字节内的换行数 = 下一行 0-based line_idx。

    cursor 是 byte offset；stream 从该 byte 起读，对应行号需独立算（不能依赖文件全局计数）。
    O(cursor) 一次性扫；典型 cursor 在 driver 持续推进下增长（仅 cold start 时 cursor=0 是免费）。

    **实现简化**：每次 stream 调用都重数 cursor 前的换行。对小文件（subagent transcript
    通常 < 几 MB）足够；超大文件可缓存（YAGNI，未来 P3 spike）。
    """
    if byte_cursor <= 0:
        return 0
    try:
        with open(path, "rb") as f:
            chunk = f.read(byte_cursor)
        return chunk.count(b"\n")
    except OSError:
        return 0


def _normalize_tool_result_content(raw: object) -> str:
    """tool_result.content 可能是 str / list[{type,text}] / None，归一成单字符串。

    同 ``claude_translator._normalize_tool_result_content``（DRY：理论上应抽到共享 util，
    但跨 layer 依赖（translator 在 profiles/，adapter 在 events/）→ 局部保留 5 行容错）。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(raw)
