"""raw_agent_event.py —— 子 agent 过程事件统一 IR（SPEC-B v4 §3.2/§3.3，R1）。

**回答的问题**：CC sidechain jsonl 与 opencode sqlite 两条读路径，如何把「子 agent 过程」
归一成同一个被 ingestor 消费的抽象？答：``RawAgentEvent``（payload 1:1 对齐
``EventType.data``，零 rename）。

**契约（R1，不可妥协）**：``payload`` 逐字 = ``EventType.data`` 字段（见 ``schema/event.py``）。
两 adapter 只负责「读原始源 + 按映射吐 RawAgentEvent」；rename / 字段变换在 adapter 内。
ingestor **零 rename** 透传 payload 到 agent_* data（唯一例外：``step_boundary`` 的
``{phase}`` → ``{step_reason}``，schema 对齐）。

依赖单向：本模块是 events 层最底层的 IR，只依赖 stdlib（dataclasses/typing）。不依赖
bus/tape/schema 事件类型集合 —— ingestor 在映射时才校验 type 在 EventType 内。

**接口同一性（SPEC §0）**：ingestor / bus / 前端永远消费 ``RawAgentEvent``，不感知 backend。
backend 差异只在 ``adapters/*`` 实现 + ``sidechain_daemon.py`` 启动参数。本模块 grep 守门
范围内零 backend 分支（SPEC §9 AC5）。

**kind 并集（U2=a）**：``RawKind`` 是两 backend 的**可能产出并集**：
  - CC 产 ``{thinking, tool_call, tool_result, text}``（sidechain 无 step；spike 坐实）
  - opencode 额外产 ``step_boundary``（step_start part）

前端对缺失 kind graceful 降级（``entries.ts:151-158`` 已就位）。「接口同一性」= 相同代码
路径消费相同 IR，**非**强制两后端产出相同 kind 集。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Literal, Protocol


# kind 并集（U2=a）。新增 kind 仅需扩此 Literal + ingestor 映射 + adapter（OCP）。
RawKind = Literal["thinking", "tool_call", "tool_result", "text", "step_boundary"]


@dataclass(frozen=True, slots=True)
class RawAgentEvent:
    """子 agent 过程事件的统一 IR（SPEC-B v4 §3.2）。

    不变式：
      - ``payload`` 逐字 = 对应 ``EventType.data`` 字段（R1）。adapter 负责保证；ingestor 透传。
      - ``source_id`` 全局唯一（per backend 内）：CC ``f"{task_id}:{line_idx}:{block_idx}"`` /
        opencode ``f"opc:{seq}"``。进 ``agent_*.data.source_id``（data 是 free dict，零 schema 改；
        reducer 对 agent_* no-op，``replay.py:132-135``；``pairToolEvents`` 不读 source_id）。

    Fields:
        child_id: 子 agent 标识。CC task_id / opencode child session id。进 ``agent_*.session_id``
            （前端按 session_id 分组，``selectConversation`` 自动归组）。
        source_id: 幂等 key（跨 restart 稳定）。daemon crash 后从 tape 重建 source_id set，
            再次 ingest 同一事件时 O(1) 命中 skip（R3）。
        kind: 产出类型（并集，见 ``RawKind``）。
        payload: 逐字 = ``EventType.data``。schema 详见模块 docstring「payload schema」段。
    """

    child_id: str
    source_id: str
    kind: RawKind
    payload: dict


# 子 agent 句柄（type-erased；CC task_id / opencode session id 均为 str）。
ChildRef = str

# adapter 内部 cursor（type-erased；CC byte-offset / opencode event.seq 均为 int）。
# daemon 在 cursors dict 持久化 cursor；crash 丢失 → 重启从 0 重读，source_id 去重兜底。
Cursor = int


class ReadAdapter(Protocol):
    """读 adapter 契约（SPEC-B v4 §3.3）。

    两 adapter（``adapters/cc_jsonl.py`` / ``adapters/opencode_sqlite.py``）唯一共同产出 =
    ``RawAgentEvent``；``discover_children`` / ``stream`` 接口同款。Backend 差异封装在实现里
    （CC sidechain 目录 resolve / opencode sqlite 查询），调用方零 backend 感知。

    **幂等保证（R3）**：``stream`` **可能重复产出同一** ``RawAgentEvent``（crash 后从 cursor=0
    重读）；``SidechainIngestor`` 用 ``source_id`` 去重，adapter 不需查重。

    **scope 铁律**（硬约束 #3）：``discover_children`` 只扫本 run 的 ``host_session``：
      - CC：只 glob ``<host_session>/subagents/agent-*.jsonl``，禁跨 session。
      - opencode：只查 ``parent_id=<host>`` 或父 event 流的 task tool，禁跨 session。

    **fail loud（硬约束 #5）**：
      - 路径 resolve / sqlite open 失败（配置错）→ raise，daemon 主体 CRITICAL log + exit。
      - partial-line（CC）/ seq 空段（opencode）→ 只 yield 完整行；partial 留下次重读。
      - 单行解析失败 → 静默跳过（损坏行不阻塞；同 ``Tape.replay`` 容错语义）。
    """

    def discover_children(
        self, host_session: str, since_ts: int
    ) -> Iterator[ChildRef]:
        """发现本 ``host_session`` 的子 agent。

        Args:
            host_session: 本 run 的宿主 session id（从 tape
                ``workflow_started.data.host_session`` 派生；U1 同源）。
            since_ts: 时间窗下界（epoch 秒）；adapter 可选用 mtime 过滤旧 child。

        Yields:
            ``child_id``（CC task_id / opencode child session id）。顺序未规定；调用方对同一
            child 多次 yield 无害（cursor 持续推进，source_id 去重兜底）。
        """
        ...

    def stream(
        self, child: ChildRef, cursor: Cursor
    ) -> Iterator[tuple[RawAgentEvent, Cursor]]:
        """从 ``cursor`` 起读 ``child`` 的增量事件，yield ``(event, new_cursor)``。

        Cursor 语义由 adapter 自定义（CC byte-offset / opencode event.seq）。调用方在 cursors
        dict 内存持久化；crash 丢失 → 从 0 重读，source_id 去重兜底。

        partial-line race 防护（CC）：只 yield 完整 ``\\n`` 终止的行；末尾 partial 行下次重读
        （同 ``chart_daemon._watch_terminal`` / ``_FlockSafeTape._read_max_seq_from_disk``）。
        """
        ...
