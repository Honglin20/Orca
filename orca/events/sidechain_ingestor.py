"""sidechain_ingestor.py —— RawAgentEvent → agent_* tape event（SPEC-B v4 §3.2/§6/§7）。

**回答的问题**：``RawAgentEvent`` 如何变成 tape 的 agent_* 事件？答：1:1 透传（R2，零 rename）
+ source_id 查重（R3）+ U1 读 tape 派生 node（§6）。

**铁律（SPEC §0 / §6 / §7）**：
  - **零 rename**（R2）：ingestor 不改 payload（adapter 已保证 payload 1:1 = EventType.data）。
    唯一例外：``step_boundary`` 的 ``{phase}`` → ``{step_reason}``（schema 对齐 agent_step_started）。
    ``source_id`` 进 ``data.source_id``（data 是 free dict，零 schema 改）。
  - **唯一写路径**（§7）：只经 ``bus.emit`` → ``_FlockSafeTape``；与 ``cli.next`` 同锁互斥。
  - **幂等（R3）**：内存 ``source_id`` set O(1) 查；命中 skip。crash restart 从 tape 一次性
    重建 set（O(N) one-shot，**非**每 emit 重扫）—— 同 ``_FlockSafeTape._read_max_seq_from_disk``
    重启重置模式。
  - **U1 node 派生**（§6）：emit 前 **增量**扫 tape 取最后 ``node_started`` 的 node。
    单 run 内（per-run tape 无 multi-run race）≤0.5s trailing 窗口（poll cycle）。

**接口同一性（SPEC §0）**：本模块零 backend 分支（grep 守门 0 hit）。kind 集合映射在本模块，
adapter 只产 RawAgentEvent；backend 永远不进 ingestor。

依赖单向：events 层内部模块（依赖 ``orca.schema`` + ``raw_agent_event`` + ``bus`` 类型）。
不依赖 iface/adapter；不被 schema 反向依赖。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from orca.events.raw_agent_event import RawAgentEvent, RawKind
from orca.events.tape import read_last_complete_lines
from orca.schema import EventType

if TYPE_CHECKING:
    from orca.events.bus import EventBus

logger = logging.getLogger(__name__)


# agent_* EventType 集合（replay reducer 对这些 no-op，前端按 session_id 分组消费）。
_AGENT_EVENT_TYPES = frozenset({
    "agent_message", "agent_thinking", "agent_tool_call",
    "agent_tool_result", "agent_step_started",
})


# kind → EventType 1:1 映射表（R2）。新增 kind 仅需扩此表（OCP，不改 ingest 主体）。
_KIND_TO_EVENT_TYPE: dict[RawKind, EventType] = {
    "thinking": "agent_thinking",
    "text": "agent_message",
    "tool_call": "agent_tool_call",
    "tool_result": "agent_tool_result",
    "step_boundary": "agent_step_started",
}


def _adapt_payload(kind: RawKind, payload: dict) -> dict:
    """payload 透传（R2）；``step_boundary`` 是唯一 schema 对齐变换。

    - ``thinking`` / ``text`` / ``tool_call`` / ``tool_result`` → 1:1 透传（浅拷贝防下游误改）。
    - ``step_boundary`` → ``{step_reason: payload.phase}``（schema ``agent_step_started.data`` 是
      ``{step_reason?}``，对齐 ``opencode_translator._translate_step_start``）。phase 缺失/空 → ``{}``
      （首个 step_start 无 reason，同 opencode_translator 语义）。
    """
    if kind == "step_boundary":
        phase = payload.get("phase")
        if isinstance(phase, str) and phase:
            return {"step_reason": phase}
        return {}
    return dict(payload)


class SidechainIngestor:
    """1:1 透传 ``RawAgentEvent`` → ``bus.emit``（agent_*），带 source_id 查重 + U1 node 派生。

    Lifecycle：
      - 构造：``SidechainIngestor(bus, tape_path)``；空 set / None node。
      - 启动一次：``rebuild_from_tape()`` 扫 tape 重建 source_id set + initial node（O(N)）。
      - 每事件：``await ingest(raw)`` → O(1) dedup + O(delta) derive node + bus.emit。

    **并发模型**：单 driver task 串行调 ``ingest``（同 ``_SidechainDriver.run``）→ 无 in-process
    race。跨进程 tape 写互斥由 ``_FlockSafeTape`` + ``cli._try_acquire_flock`` 守（同 chart 守护）。

    **U1 单 run 限定**：本 ingestor 绑定一个 tape_path（per-run）；不会跨 run race（SPEC §6 H3）。
    """

    def __init__(self, bus: "EventBus", tape_path: Path) -> None:
        self._bus = bus
        self._tape_path = Path(tape_path)
        # R3.2：内存 source_id set。emit 前 O(1) 查；命中 skip。
        self._seen_source_ids: set[str] = set()
        # §6 U1：当前 node（最后一条 node_started.node）。
        self._current_node: str | None = None
        # _derive_current_node 的增量扫描游标（同 _FlockSafeTape._scan_offset 模式）。
        self._node_scan_offset: int = 0

    def rebuild_from_tape(self) -> None:
        """crash restart 一次性重建 source_id set + initial current_node（R3.3）。

        扫 tape 全文：
          - agent_* 事件 → 取 ``data.source_id`` 入 set（仅 str 非空）。
          - ``node_started`` 事件 → 取最后一条 ``node`` 作 ``_current_node``。
          - 推进 ``_node_scan_offset`` 到 EOF（后续 ``_derive_current_node`` 只读 delta）。

        失败语义：tape 不存在 / 读失败 → 静默返（与首次启动等价：空 set + None node）；
        损坏行（非合法 JSON / 缺字段）跳过（不阻塞，同 ``Tape.replay`` 容错）。
        """
        try:
            text = self._tape_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return  # 首启（tape 还没被 bootstrap 创建）；空 set 等价
        except OSError:
            logger.warning(
                "sidechain ingestor rebuild: 读 %s 失败（OSError），以空 set 启动",
                self._tape_path, exc_info=True,
            )
            return

        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t in _AGENT_EVENT_TYPES:
                sid = obj.get("data", {}).get("source_id")
                if isinstance(sid, str) and sid:
                    self._seen_source_ids.add(sid)
            elif t == "node_started":
                node = obj.get("node")
                if isinstance(node, str) and node:
                    self._current_node = node

        try:
            self._node_scan_offset = self._tape_path.stat().st_size
        except OSError:
            # stat 失败不影响重建结果（offset 仅用于后续 derive 的增量优化）。
            pass

    async def ingest(self, raw: RawAgentEvent) -> bool:
        """1:1 透传 ``raw`` → ``bus.emit``（agent_*）。

        Returns:
            True = 真 emit（新 source_id）；False = dedup skip（已 ingest 过）。

        Steps:
          1. O(1) source_id 查重；命中 → 返 False。
          2. O(delta) ``_derive_current_node`` 增量扫 tape。
          3. payload 透传 + ``data.source_id`` 注入（R3.1）。
          4. ``bus.emit`` 单一写路径（EventBus → _FlockSafeTape）。
        """
        if raw.source_id in self._seen_source_ids:
            return False
        self._seen_source_ids.add(raw.source_id)

        node = self._derive_current_node()
        event_type = _KIND_TO_EVENT_TYPE.get(raw.kind)
        if event_type is None:
            # 不应发生（adapter 只产 RawKind 内的 kind）；fail loud 不静默吞。
            logger.error(
                "sidechain ingestor: 未知 RawKind %r (source_id=%s)，跳过",
                raw.kind, raw.source_id,
            )
            return False

        data = _adapt_payload(raw.kind, raw.payload)
        data["source_id"] = raw.source_id  # R3.1：source_id 进 data

        await self._bus.emit(
            event_type,
            data,
            node=node,
            session_id=raw.child_id,
        )
        return True

    def _derive_current_node(self) -> str | None:
        """O(delta) 增量扫 tape，取最后一条 ``node_started`` 的 node（§6 U1）。

        维护 ``_node_scan_offset``：每次仅读「上次 offset → EOF」新字节，找新 ``node_started``。
        首次（``rebuild_from_tape`` 后）offset = EOF；后续每 emit 仅扫 delta（同
        ``_FlockSafeTape._read_max_seq_from_disk`` 模式）。

        partial-line race 防护 + binary-mode 多字节安全由共享 helper
        ``events.tape.read_last_complete_lines`` 守（SPEC §5 S7 DRY 抽出）。

        时序正确性：``bus.emit`` 之前调本方法 → 读到的是「cli ``next`` 最近一次 append 的状态」，
        跨进程 flock 互斥保证 cli append 与 daemon emit 不交错（SPEC §6 U1 闭环）。
        """
        try:
            cur_size = self._tape_path.stat().st_size
        except (FileNotFoundError, OSError):
            return self._current_node

        if cur_size < self._node_scan_offset:
            # tape 被截断（理论不应发生；cli.next 只 append）→ 仅重置 offset 让下次全扫重派生。
            # 保留 ``_current_node``：避免防御性分支意外产生 node=None 的孤儿 agent_* 事件
            # （下次 derive 会基于全 tape 重扫覆盖它；保留比清空更保守）。
            self._node_scan_offset = 0

        if cur_size == self._node_scan_offset:
            return self._current_node

        lines, new_offset = read_last_complete_lines(
            self._tape_path, self._node_scan_offset, cur_size,
        )
        if lines is None:
            # 读失败 / 整段 partial → 沿用缓存 node（下次重读）。
            logger.debug(
                "sidechain ingestor derive node: 读 %s 失败/partial（沿用缓存 node=%r）",
                self._tape_path, self._current_node, exc_info=True,
            )
            return self._current_node

        self._node_scan_offset = new_offset
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "node_started":
                node = obj.get("node")
                if isinstance(node, str) and node:
                    self._current_node = node
        return self._current_node

    # ── 观测辅助（测试用）─────────────────────────────────────────────────────

    @property
    def seen_source_ids(self) -> frozenset[str]:
        """测试观测：返回当前 source_id set 的 snapshot（frozen 防外部误改）。"""
        return frozenset(self._seen_source_ids)

    @property
    def current_node(self) -> str | None:
        """测试观测：返回当前派生的 node。"""
        return self._current_node
