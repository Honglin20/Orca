"""opencode_sqlite.py —— opencode sqlite ``event`` 表读 adapter（SPEC-B v4 §5）。

**回答的问题**：opencode 子 agent 的过程事件怎么读？答：opencode 把所有 session 事件
（含 sub-agent）写到 sqlite DB 的 ``event`` 表；按 ``aggregate_id``（= session id）+ ``seq``
游标增量查 → 映射成 ``RawAgentEvent``。

**为什么用 ``event`` 表而非 ``message.part`` 表（SPEC §5 关键决策，纠 v3）**：
``message.part.updated.1`` 在 INSERT + UPDATE 各 fire 一次（9111 实测 ≈2.1/part）。``part``
行的 ``id`` / ``time_created`` 在状态翻转（task ``running`` → ``completed``）时**不变** →
``part`` 游标漏 tool 完成态。``event`` 表捕获翻转 → adapter 吐和 CC 一样的
``tool_call`` → ``tool_result`` 序列（**强化接口统一**）。

**映射（SPEC §5，与 opencode_translator 对齐）**：
  - ``reasoning`` part       → ``thinking`` payload ``{text}``
  - ``text`` part            → ``text`` payload ``{text}``
  - ``tool`` part + INSERT (state.status=``running``)   → ``tool_call``
    payload ``{tool, args=state.input, tool_call_id=part.callID}``
  - ``tool`` part + UPDATE (state.status=``completed``) → ``tool_result``
    payload ``{tool_call_id=part.callID, result=state.output}``
  - ``step-start`` part      → ``step_boundary`` payload ``{phase:"start"}``
  - ``step-finish`` part     → skip（agent_usage 在 B2 scope 外，U4 deferred）
  - 其它未知 part.type       → skip（不抛，与 opencode_translator ``unknown_event`` 不同：
    B2 仅 scope 过程事件，未知 part 不进 tape）

**source_id** = ``f"opc:{seq}"``：用 ``event.seq`` 而非 ``part.id``。SPEC §5 字面提 ``part.id``
（spike 假设 immutable），但 part 单行双状态（running→completed）时 part.id 不变 → source_id
必撞。seq 是 event 表连续整数无空洞、INSERT/UPDATE 各占一行 → 天然唯一。这是对 SPEC §5 的
**防御性修正**（adapter 责任，ingestor 无感知；P2 spike E2 验证 part.id immutability 后可回调）。

**cursor** = ``event.seq``：``stream`` 查 ``WHERE aggregate_id=? AND seq > cursor ORDER BY seq``。

**scope 铁律**：``discover_children`` 只查 **parent_id=<host_session>** 的 session 或父 event
流的 task tool part；禁跨 session 扫（硬约束 #3）。

**读 live WAL**：``sqlite3.connect("file:...?mode=ro", uri=True)`` 只读连接，WAL 并发读，不
阻塞 opencode 写（SPEC §5）。每次 stream 新开连接（短查询）→ 无长事务持锁。

**fail loud**：
  - DB 文件不存在 → ``discover_children`` / ``stream`` 返空（opencode 未启动 / 路径错）。
  - 查询失败 → log warning + 返空（不阻塞 driver；下次重试）。
  - 单 row data JSON 解析失败 → skip（不阻塞）。

**P2 spike（实施时）**：part.id immutable 生命周期证（E2）+ WAL commit 间隔 vs ≤2s（N4）。
本契约实现 + 单测覆盖即可；**不跑 opencode 真机 spike**（任务约束）。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Iterator

from orca.events.raw_agent_event import ChildRef, Cursor, RawAgentEvent

logger = logging.getLogger(__name__)

# 与 opencode_translator 同源：tool output 截断上限。
_TOOL_RESULT_MAX_CHARS = 4096


class OpencodeAdapterError(RuntimeError):
    """opencode adapter 配置错误（fail loud；daemon catch 后 CRITICAL log + exit）。"""


def _resolve_db_path(db_path: Path | None = None) -> Path:
    """opencode session sqlite DB 路径。

    解析顺序：
      1. ``db_path`` 显式参数（测试用）。
      2. ``ORCA_OPENCODE_DB`` env（测试覆盖）。
      3. 默认：``~/.local/share/opencode/session.db``（opencode v1.14+ 实测）。
    """
    if db_path is not None:
        return Path(db_path)
    env_db = os.environ.get("ORCA_OPENCODE_DB")
    if env_db:
        return Path(env_db)
    # B2-VRFY local patch: real opencode v1.18 writes to opencode.db (not session.db
    # as SPEC §5 assumed). Try opencode.db first, fall back to session.db for compat.
    default_opencode = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    if default_opencode.is_file():
        return default_opencode
    return Path.home() / ".local" / "share" / "opencode" / "session.db"


class OpencodeSqliteAdapter:
    """opencode sqlite event 表读 adapter（实现 ``ReadAdapter`` 协议）。

    无状态（除 cursor 游标在调用方 ``_SidechainDriver`` 持久化）。
    每次 ``stream`` 新开 readonly 连接 → 查增量行 → 关闭。
    """

    # opencode event 表中我们关心的 envelope type（``message.part.updated.1`` fire 于
    # INSERT + UPDATE；其它 type 如 ``session.updated.1`` / ``message.updated.1`` 与 B2 scope
    # 无关，WHERE 过滤掉以减负载）。
    _RELEVANT_EVENT_TYPE = "message.part.updated.1"

    # task tool 名（opencode 内置）：父 session 派子 agent 的 tool。
    _TASK_TOOL_NAME = "task"

    def __init__(
        self,
        host_session: str,
        *,
        db_path: Path | None = None,
    ) -> None:
        """Args:
            host_session: 宿主 opencode session id（必非空）。
            db_path: opencode session sqlite 路径（测试用；覆盖 env / 默认）。
        """
        if not host_session:
            raise OpencodeAdapterError(
                "opencode adapter: host_session 为空"
                "（需要 ORCA_HOST_SESSION_ID 或显式 --host-session）"
            )
        self._host_session = host_session
        self._db_path = _resolve_db_path(db_path)

    @property
    def db_path(self) -> Path:
        """观测：DB 路径（测试用）。"""
        return self._db_path

    def _connect_ro(self) -> sqlite3.Connection:
        """readonly + WAL-safe 连接（``mode=ro`` + ``immutable=1`` 不用：WAL 仍在写）。"""
        # mode=ro：只读，不允许 write 操作；WAL concurrent reader 不阻塞 writer。
        uri = f"file:{self._db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        # 避免自动 begin transaction（readonly 下不必要，且会持 shared lock）。
        conn.isolation_level = None
        return conn

    def discover_children(
        self, host_session: str, since_ts: int
    ) -> Iterator[ChildRef]:
        """扫父 event 流的 ``task`` tool part，提 ``state.metadata.sessionId`` → child session。

        scope：``host_session != self._host_session`` → 返空（硬约束 #3）。
        实证（SPEC §5）：10 样本 == ``session.parent_id``。
        备选路径（``SELECT id FROM session WHERE parent_id=?``）作 fallback（session 表查不到时）。
        """
        if host_session != self._host_session:
            return
        if not self._db_path.is_file():
            return  # opencode 未启动 / 路径错

        seen: set[str] = set()
        try:
            conn = self._connect_ro()
        except sqlite3.Error:
            logger.warning(
                "opencode adapter: 打开 DB %s 失败", self._db_path, exc_info=True,
            )
            return
        try:
            # 主路径：扫父 event 流找 task tool part 的 metadata.sessionId。
            rows = conn.execute(
                "SELECT data FROM event "
                "WHERE aggregate_id = ? AND type = ? ORDER BY seq",
                (host_session, self._RELEVANT_EVENT_TYPE),
            ).fetchall()
            for (data_json,) in rows:
                try:
                    data = json.loads(data_json)
                except (json.JSONDecodeError, TypeError):
                    continue
                part = data.get("part") if isinstance(data, dict) else None
                if not isinstance(part, dict):
                    continue
                if part.get("tool") != self._TASK_TOOL_NAME:
                    continue
                state = part.get("state") or {}
                if not isinstance(state, dict):
                    continue
                # 仅 completed 态的 task part 才有完整 metadata.sessionId（实证）。
                if state.get("status") != "completed":
                    continue
                meta = state.get("metadata") or {}
                child_sid = meta.get("sessionId") if isinstance(meta, dict) else None
                if isinstance(child_sid, str) and child_sid and child_sid not in seen:
                    seen.add(child_sid)
                    yield child_sid

            # Fallback：session.parent_id 查询（主路径漏时兜底；session_parent_idx 加速）。
            if not seen:
                fallback_rows = conn.execute(
                    "SELECT id FROM session WHERE parent_id = ?",
                    (host_session,),
                ).fetchall()
                for (sid,) in fallback_rows:
                    if isinstance(sid, str) and sid and sid not in seen:
                        seen.add(sid)
                        yield sid
        except sqlite3.Error:
            logger.warning(
                "opencode adapter discover: 查询 %s 失败", self._db_path, exc_info=True,
            )
        finally:
            conn.close()

    def stream(
        self, child: ChildRef, cursor: Cursor
    ) -> Iterator[tuple[RawAgentEvent, Cursor]]:
        """从 ``event.seq > cursor`` 起读 child 流，映射 → yield (event, new_cursor=seq)。"""
        if not self._db_path.is_file():
            return
        try:
            conn = self._connect_ro()
        except sqlite3.Error:
            logger.warning(
                "opencode adapter stream: 打开 DB %s 失败", self._db_path, exc_info=True,
            )
            return
        try:
            rows = conn.execute(
                "SELECT seq, data FROM event "
                "WHERE aggregate_id = ? AND type = ? AND seq > ? "
                "ORDER BY seq",
                (child, self._RELEVANT_EVENT_TYPE, cursor),
            ).fetchall()
        except sqlite3.Error:
            logger.warning(
                "opencode adapter stream: 查询 child=%s cursor=%d 失败",
                child, cursor, exc_info=True,
            )
            return
        finally:
            conn.close()

        for seq, data_json in rows:
            try:
                data = json.loads(data_json)
            except (json.JSONDecodeError, TypeError):
                continue
            part = data.get("part") if isinstance(data, dict) else None
            if not isinstance(part, dict):
                continue
            for raw in self._map_part(part, child, seq):
                yield raw, seq

    def _map_part(
        self, part: dict, child_id: str, seq: int
    ) -> Iterator[RawAgentEvent]:
        """单 part → RawAgentEvent（按 part.type 分派）。

        seq 是 event 表 PER-AGGREGATE（per-session）行号；GLOBAL source_id 必须含 child
        以免跨 child seq 撞车（B2-VRFY local patch：实证 event PK=(aggregate_id,seq)，
        v1 实现误以为 seq 全局唯一）。
        """
        part_type = part.get("type")
        if part_type == "reasoning":
            text = part.get("text", "")
            if isinstance(text, str) and text:
                yield RawAgentEvent(
                    child_id=child_id,
                    source_id=f"opc:{child_id}:{seq}",
                    kind="thinking",
                    payload={"text": text},
                )
        elif part_type == "text":
            text = part.get("text", "")
            if isinstance(text, str) and text:
                yield RawAgentEvent(
                    child_id=child_id,
                    source_id=f"opc:{child_id}:{seq}",
                    kind="text",
                    payload={"text": text},
                )
        elif part_type == "tool":
            yield from self._map_tool_part(part, child_id, seq)
        elif part_type == "step-start":
            yield RawAgentEvent(
                child_id=child_id,
                source_id=f"opc:{child_id}:{seq}",
                kind="step_boundary",
                payload={"phase": "start"},
            )
        # step-finish / unknown → skip（B2 scope 仅过程；step_finish 含 usage = U4 deferred）

    def _map_tool_part(
        self, part: dict, child_id: str, seq: int
    ) -> Iterator[RawAgentEvent]:
        """tool part 单行双状态（running→completed）→ 两次 event 行（INSERT + UPDATE）。

        - INSERT (state.status=``running``) → ``tool_call`` payload
          ``{tool, args=state.input, tool_call_id=part.callID}``
        - UPDATE (state.status=``completed``) → ``tool_result`` payload
          ``{tool_call_id=part.callID, result=state.output}``

        args = ``state.input``（形状随 tool 变：bash={command}/read={filePath}/task={...}/…，
        per-tool 分派由前端处理）；output 恒 string（结构化在 metadata）。
        """
        state = part.get("state") or {}
        if not isinstance(state, dict):
            return
        status = state.get("status")
        tool_name = str(part.get("tool", ""))
        call_id = str(part.get("callID", ""))
        if status == "running":
            yield RawAgentEvent(
                child_id=child_id,
                source_id=f"opc:{child_id}:{seq}",
                kind="tool_call",
                payload={
                    "tool": tool_name,
                    "args": state.get("input") or {},
                    "tool_call_id": call_id,
                },
            )
        elif status == "completed":
            output = state.get("output")
            result_text = _normalize_tool_output(output)
            if len(result_text) > _TOOL_RESULT_MAX_CHARS:
                result_text = result_text[:_TOOL_RESULT_MAX_CHARS] + "…[truncated]"
            yield RawAgentEvent(
                child_id=child_id,
                source_id=f"opc:{child_id}:{seq}",
                kind="tool_result",
                payload={
                    "tool_call_id": call_id,
                    "result": result_text,
                },
            )
        # 其它 status (pending/failed/...) → B2 scope 外，skip


def _normalize_tool_output(raw: object) -> str:
    """工具 output 归一成字符串（同 ``opencode_translator._normalize_tool_output``）。

    防御性处理 str / None / 其他类型。跨 layer 不共享（同 CC adapter 工具 result 归一）。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(raw)
