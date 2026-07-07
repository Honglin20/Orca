"""agent_history.py —— phase-16 单流 inline Agent History（CC 风格折叠 + 工具配对）。

回答「这个 agent 干了什么？最后说了什么？」：单 agent 视图，**一条 RichLog 流**
+ entry 内联展开（Claude-Code 风格）。tool_call + tool_result **配对成一条** entry
（默认折叠一行，Enter 展开 tool card）；agent message 视觉分级（bold + 主题色），
展开内联 Markdown。

设计原则（phase-16 SPEC 铁律）：
  - **壳无真相**（铁律 #1）：widget 只持 ``_entries: list[_HistEntry]`` +
    ``_expanded_seqs``，由 app ``set_node()`` / ``append_event()`` 注入；不订阅 bus、
    不读 tape（重放/replay 必产相同渲染）。配对/派生是 event list 的纯函数（reducer
    fold），顺序无关。
  - **render layer 零改动**（铁律 #2）：``render_tool`` / ``render_message`` /
    ``render_thinking`` 原样复用，产出的 Rich renderable 直接 ``RichLog.write(...)``。
  - **依赖单向**（铁律 #3）：仅 import ``orca.schema`` + textual + rich + stdlib +
    本包 ``_event_summary`` / ``tool_render``；禁止 ``orca.exec`` / ``orca.run`` /
    ``orca.events.bus``。
  - **接口统一性**（铁律 #4）：AgentHistory 公开 API（``set_node`` / ``append_event`` /
    ``set_executor`` / ``action_*``）签名零变化——OrcaApp 调用点不动。
  - **不留兼容路径**（铁律 #7）：删 ``#agent-history-detail*`` DOM + ``_detail_view``
    + ``_refresh_detail``；新旧渲染模型不并存。``app.query_one("#agent-history-detail")``
    必抛 ``NoMatches``。

phase-16 主要变化（相对 v2 两区设计）：
  1. **单流 inline**：取消独立 detail 区，所有内容写进**一条** ``#agent-history-log``；
     展开的 entry 紧跟其摘要行下方内联 detail（缩进 + ``⎿`` 引导）。
  2. **工具配对**（解决 B1）：``agent_tool_call`` 建 running ToolEntry 入 ``_entries``；
     ``agent_tool_result`` 到达时**就地升级**对应 entry（``entries[i] = merged``），
     保持原 call 的 seq + 列表位置（``merged.seq = call.seq``），避免 ``_selected_seq``
     dangling。不 remove+append。
  3. **视觉分级**（解决 B2）：message 摘要行 bold + 主题色；thinking dim italic；
     tool 中性色 + status icon ``✓/…/✗``。
  4. **Enter = reflow**：因为 detail 现在内联在同一条 RichLog，toggle 必须整流重渲
     （clear + rewrite），不能只 refresh 一个独立 detail 块。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from rich.console import RenderableType
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import RichLog, Static

from orca.iface.cli.widgets._event_summary import (
    _build_detail_renderable,   # 折叠详情（调 phase-15 render_tool / render_message / render_thinking）
    _build_meta_line,           # meta 信息（tool 配对后 lines/elapsed；message lines markdown）
    _build_summary_line,        # summary 文本（去 TYPE-LABEL 前缀）
)
from orca.schema import Event

# spec §2.3 6 TYPE-LABEL（6 字符宽，对齐 summary 行）
_TYPE_LABELS: dict[str, str] = {
    "agent_thinking":            "THINK",
    "agent_tool_call":           "TOOL",
    "agent_tool_result":         "TOOL",
    "agent_message":             "MSG",
    "human_decision_requested":  "GATE",
    "human_decision_resolved":   "GATE",
    "interrupt_requested":       "INT",
    "interrupt_resolved":        "INT",
}

# spec §2.3 tool_call_id cache LRU 上限（GAP-B/C 修复机制，spec §2.3）
_TOOL_CALL_CACHE_CAP = 500

# _pending_results LRU 上限（phase-16 §0.2 铁律 #5：result 早于 call 的 orphan 缓冲，
# 两路径共用；translator 丢 call 时防 dict 无界增长）。
_PENDING_RESULTS_CAP = 500

# 头部行 truncated 标记阈值（spec §9 R3 FIFO 上限是 1000 events/node）
_TRUNCATED_THRESHOLD = 1000

# _HistEntry.kind —— 派生自 event_type（"tool" = 配对后的 call+result）
EntryKind = Literal["tool", "message", "thinking", "other"]

# 工具状态 icon（summary 行尾）：completed / running / failed
_TOOL_STATUS_ICON = {"completed": "✓", "running": "…", "failed": "✗"}


def _derive_kind(event_type: str) -> EntryKind:
    """从 event_type 派生 EntryKind（纯函数，顺序无关）。

    - ``agent_message`` → ``"message"``
    - ``agent_thinking`` → ``"thinking"``
    - ``agent_tool_call`` / ``agent_tool_result`` → ``"tool"``（配对后归一类）
    - 其余 → ``"other"``
    """
    if event_type == "agent_message":
        return "message"
    if event_type == "agent_thinking":
        return "thinking"
    if event_type in ("agent_tool_call", "agent_tool_result"):
        return "tool"
    return "other"


@dataclass
class _HistEntry:
    """单条 AgentHistory entry（phase-16 单流模型）。

    每条 entry 对应**一个或一对** Event（tool 配对后 call+result 共享一条 entry）；
    持有：
      - ``seq``：排序 + 唯一标识（tool 配对后 = call.seq，保持列表位置不变）
      - ``kind``：``"tool"`` / ``"message"`` / ``"thinking"`` / ``"other"``
      - ``event_type``：原 Event type（tool 配对后 = ``"agent_tool_call"``，标识 call 位）
      - ``timestamp``：Event timestamp（summary 行 HH:MM:SS）
      - ``summary``：summary 文本（**不含** TYPE-LABEL 前缀，reflow 时拼）
      - ``meta``：第 2 行元信息（可空）
      - ``detail``：内联 detail Rich renderable（None = 无折叠）
      - ``tool_status``：tool 状态（``"completed"`` / ``"running"`` / ``"failed"``）；
        非 tool entry 为 ``None``。
      - ``tool_name``：tool 名（tool entry 用；其余 None）—— summary 行 ``✓ <tool>`` 用
      - ``merged``：tool entry 是否已配对 result（True = call+result 已合；False = 仅 call，
        running 态）—— 测试期 fail loud 断言用（SPEC §5.2 ``merged==False`` 数 == 0）
      - ``tool_call_id``：tool entry 的 tcid（call 位填，配对/反查用）；非 tool entry 为 None
      - ``call_args``：tool call 的 args dict（merge 时重建 detail 用，避免 cache evicted）
    """
    seq: int
    kind: EntryKind
    event_type: str
    timestamp: float
    summary: str
    meta: str = ""
    detail: RenderableType | None = None
    tool_status: str | None = None
    tool_name: str | None = None
    merged: bool = False
    tool_call_id: str | None = None
    call_args: dict = field(default_factory=dict)


class AgentHistory(Static):
    """phase-16 单流 inline Agent History（spec §2.3 + phase-16 SPEC §2）。

    用法（由 OrcaApp 驱动，签名零变化）::

        hist = app.query_one(AgentHistory)
        hist.set_executor("claude")                          # normalize_tool 查表用
        hist.set_node("analyzer", events=[...])              # 切换 agent 全量重渲
        hist.append_event(event)                              # 增量追加（仅 selected_node）
    """

    DEFAULT_CSS = """
    AgentHistory {
        width: 1fr; height: 7fr;
        border: round $success;
        padding: 0 1;
        background: $surface;
    }
    #agent-history-log {
        height: 1fr;
    }
    """

    BINDINGS = [
        # spec v2 §2.3 + §2.2：j/k/Enter/L 全部在 OrcaApp 级 BINDINGS 上提，原因：
        # 1. AgentHistory 是 ``Static``（``can_focus=False`` 默认），widget BINDINGS 在
        #    无 focus 时不触发；
        # 2. 内嵌的 ``RichLog(agent-history-log)`` 拿默认焦点后吞 j/k/L 字符，且其 BINDINGS
        #    优先级高于 App 级，会拦截 widget 自己绑的 j/k。
        # 解决：widget BINDINGS 完全不绑 j/k/L/enter，全部由 App 级 BINDINGS 命中后转发到
        # 既有 action_* 方法（单测通道保留：``test_action_toggle_expand`` 等仍直接调 widget
        # action_*，接口零修改）。
        # 这里 BINDINGS 留空是为了不与 App 级冲突（widget BINDINGS 命中后 App 级失效）。
    ]

    def __init__(self) -> None:
        super().__init__("", id="agent-history")
        # 当前 agent 名（None = 未设）
        self._node_name: str | None = None
        # entries 列表（按 seq 排序，reducer fold）
        self._entries: list[_HistEntry] = []
        # 当前 executor（normalize_tool 查表用）
        self._executor: str = "claude"
        # spec §2.3 last message 默认展开规则：set 含 last message seq
        # 每次 set_node 重置；新 agent_message 到达时替换（不是 add）
        self._expanded_seqs: set[int] = set()
        # 当前选中 entry seq（None = 未选中；j/k 切换）
        self._selected_seq: int | None = None
        # tool_call_id cache（spec §2.3 GAP-B/C 修复，迁自 v1.1.1）
        # key = tool_call_id, value = (tool_name, args_dict, call_timestamp)
        self._tool_call_cache: dict[str, tuple[str, dict, float]] = {}
        # tool_call_id → entry index 反查表（phase-16 §2.2 就地升级 O(1) 定位用）：
        # call append 时填；result 就地升级后位置不变故无需更新；sort 后整体重建。
        # 独立于 _tool_call_cache 的 LRU evict（index 反映 _entries 真实位置）。
        self._tcid_to_entry_idx: dict[str, int] = {}
        # 乱序 result 缓冲（phase-16 §5.6 reducer fold 顺序无关性）：
        # result 早于 call 到达时暂存 tcid → result_event；call 到达时配对 + 移除。
        # 仅 set_node 全量 fold 用（append_event 增量路径走降级，见 _fold_event 参数）。
        self._pending_results: dict[str, Event] = {}
        # 内部 widget 引用（compose 后挂载；headless 测试时为 None）
        self._log: RichLog | None = None
        self._header_view: Static | None = None

    # ── Textual 钩子 ──────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        # 头部行（agent 名 + entry count + 截断标记；Step 5 app 投影 iter/status/tok/cost）
        yield Static("", id="agent-history-header")
        with Vertical():
            # phase-16 §2.1：**唯一**一条 RichLog 流（删 #agent-history-detail* 独立区）
            yield RichLog(id="agent-history-log", markup=False, wrap=True, auto_scroll=True)

    def on_mount(self) -> None:
        self._log = self.query_one("#agent-history-log", RichLog)
        self._header_view = self.query_one("#agent-history-header", Static)

    # ── 配置 ──────────────────────────────────────────────────────────

    def set_executor(self, executor: str) -> None:
        """设置当前 backend（claude / opencode / codex）—— normalize_tool 查表用。"""
        self._executor = executor or "claude"

    @property
    def entries(self) -> list[_HistEntry]:
        """entries 只读视图（测试用；返回浅拷贝防外部修改）。"""
        return list(self._entries)

    @property
    def node_name(self) -> str | None:
        return self._node_name

    @property
    def expanded_seqs(self) -> set[int]:
        """当前展开的 seq set（测试用；返回浅拷贝防外部修改）。"""
        return set(self._expanded_seqs)

    # ── 切换 agent（spec §2.3 set_node + reviewer P1-7 浅拷贝 + P1-8 reset）──

    def set_node(self, name: str | None, events: list[Event]) -> None:
        """切换 agent + 全量重渲（spec §2.3 + §3 + phase-16 §2.2 配对 fold）。

        Args:
            name: 新 agent 名（None = 空）。
            events: 该 agent 的全部事件列表（按 seq 排序）。

        强制 reset（避免旧 agent 状态残留，reviewer P1-8）：
          - ``_expanded_seqs`` 重置为 ``{last_agent_message_seq if any else set()}``
          - ``_selected_seq`` 重置为 ``None``
          - ``_tool_call_cache`` / ``_tcid_to_entry_idx`` 重置为空（per-node 隔离）

        phase-16 §5.6 reducer fold：配对/派生是 event list 的纯函数，正序/乱序回放
        产相同 ``(seq, kind, summary)`` 集合。
        """
        self._node_name = name
        # 切换 agent 时清空 cache + index + pending，避免上一个 agent 的 tcid 残留
        self._tool_call_cache.clear()
        self._tcid_to_entry_idx.clear()
        self._pending_results.clear()
        # reducer fold：逐 event 处理（call 建条目；result 就地升级 by tcid 匹配）。
        # 与 append_event 共用同一 _apply_event（铁律 #5：replay == live，构造性保证）。
        self._entries = []
        for e in events:
            self._apply_event(e)
        # 排序（防御乱序，reducer fold 性质保证一致）+ 重建 index map（位置可能变）
        self._entries.sort(key=lambda e: e.seq)
        self._rebuild_tcid_index()
        # reviewer P1-8：reset _expanded_seqs 到 last message
        self._expanded_seqs = self._compute_default_expanded()
        self._selected_seq = None
        self._reflow()

    def _apply_event(self, event: Event) -> None:
        """**单一 fold 核心**（phase-16 SPEC §0.2 铁律 #5：replay == live，构造性保证）。

        ``set_node``（批量/replay）与 ``append_event``（增量/live ``orca run``）**共用本方法**
        ——禁止两套配对/缓冲逻辑（旧实现的 ``buffer_orphans=True/False`` 分支已删，那是
        「多套接口」违反铁律 #4，会导致同一事件流在 replay 与 live 下产出不同 entry）。

        - ``agent_tool_call``：建 running ToolEntry append；填 ``_tool_call_cache`` +
          ``_tcid_to_entry_idx``。若 ``_pending_results`` 有匹配 tcid（result 早到），
          立即就地升级（保证乱序回放顺序无关）。
        - ``agent_tool_result``：``_tcid_to_entry_idx`` O(1) 反查 call 位 index，
          **就地升级**（保持原 seq 和列表位置，``merged.seq = call.seq``）；无匹配 + 有 tcid
          → 暂存 ``_pending_results``（**两路径同一语义**：等 call 到达再配对，不降级独立
          entry——降级会制造 replay/live 分叉）；无 tcid（异常 result）→ 独立 entry（无法配对）。
        - 其余：直接 append。

        顺序无关性（phase-16 §5.6 reducer fold 铁律 #1）：配对靠 ``tool_call_id`` 匹配 +
        ``_pending_results`` 缓冲（两路径同一缓冲），正序/逆序/乱序回放产相同结果。

        性能（reviewer P1-1）：call/result 配对 O(1) 经 ``_tcid_to_entry_idx``。
        """
        etype = event.type
        if etype == "agent_tool_call":
            self._update_tool_call_cache(event)
            entry = self._build_entry_from_event(event)
            self._entries.append(entry)
            tcid = (event.data or {}).get("tool_call_id")
            if tcid is not None:
                self._tcid_to_entry_idx[tcid] = len(self._entries) - 1
                # 乱序补救：若 result 早于 call 到达（pending 缓冲），立即就地升级
                if tcid in self._pending_results:
                    pending_result = self._pending_results.pop(tcid)
                    idx = self._tcid_to_entry_idx[tcid]
                    self._entries[idx] = self._merge_result_into_entry(
                        self._entries[idx], pending_result,
                    )
        elif etype == "agent_tool_result":
            tcid = (event.data or {}).get("tool_call_id")
            call_idx = self._tcid_to_entry_idx.get(tcid) if tcid else None
            if call_idx is not None:
                # 就地升级：merged.seq = call.seq（保持位置不变，避免 _selected_seq dangling）
                self._entries[call_idx] = self._merge_result_into_entry(
                    self._entries[call_idx], event,
                )
                # 配对完成：从 index map 移除（防后续同 tcid result 重复命中）
                if tcid is not None:
                    self._tcid_to_entry_idx.pop(tcid, None)
            elif tcid:
                # result 早于 call（或 call 丢失）→ 暂存 pending 等配对（两路径同一语义，
                # 不降级独立 entry——降级会制造 replay/live 分叉，违反铁律 #5）。LRU 防泄漏。
                self._pending_results[tcid] = event
                if len(self._pending_results) > _PENDING_RESULTS_CAP:
                    oldest = next(iter(self._pending_results))
                    self._pending_results.pop(oldest, None)
            else:
                # 无 tcid 的异常 result：无法配对，独立 entry（genuine anomaly，非路径分支）
                entry = self._build_entry_from_event(event)
                self._entries.append(entry)
        else:
            self._update_tool_call_cache(event)
            entry = self._build_entry_from_event(event)
            self._entries.append(entry)

    def _rebuild_tcid_index(self) -> None:
        """sort 后重建 ``_tcid_to_entry_idx``（位置可能变）。

        仅索引 running（未配对）tool entry；merged entry 已配对，无需进 index。
        """
        self._tcid_to_entry_idx.clear()
        for i, entry in enumerate(self._entries):
            if entry.kind == "tool" and not entry.merged and entry.tool_call_id:
                self._tcid_to_entry_idx[entry.tool_call_id] = i

    def _compute_default_expanded(self) -> set[int]:
        """返回含最后一条 agent_message seq 的 set（无 message 返回空 set）。"""
        for entry in reversed(self._entries):
            if entry.kind == "message":
                return {entry.seq}
        return set()

    # ── 增量追加（spec §3 仅 selected_node）──────────────────────────

    def append_event(self, event: Event) -> None:
        """追加单条 event + 渲染（spec §3 + phase-16 §2.2 就地升级）。

        调用方（OrcaApp._dispatch_to_widgets）负责过滤 ``event.node == _selected_node``。
        本方法假设 event 已属于当前 agent。

        ``agent_message`` 到达时：
          - ``_expanded_seqs`` **替换**为 ``{new_seq}``（不是 add，spec §2.3 last message 自动展开）

        ``agent_tool_call`` 到达时填 ``_tool_call_cache`` + ``_tcid_to_entry_idx``；
        ``agent_tool_result`` 到达时反查 index map **就地升级**对应 ToolEntry（保持 seq/位置）。
        若 result 无 call 配对（translator 丢事件）：**降级独立 entry**（fail loud，
        SPEC §0.2 #5）——增量路径不缓冲 pending（生产期事件流时序保证 call 先于 result）。

        phase-16 §2.4：因 detail 现在内联在同一条 RichLog，追加 / toggle 都触发全量 reflow。
        """
        # 单一 fold 核心 _apply_event（铁律 #5：与 set_node 共用，replay == live 构造性保证）。
        self._apply_event(event)
        # 保持 seq 排序（防御乱序）+ 维护 index map（就地升级位置不变，但若 sort 触发需重建）。
        # 假设：生产期 seq 单调递增，乱序仅限末位（translator 重试边界）；set_node 全量
        # 路径已有统一 sort + rebuild 兜底，故此处局部守卫足够。
        if len(self._entries) >= 2 and self._entries[-2].seq > self._entries[-1].seq:
            self._entries.sort(key=lambda e: e.seq)
            self._rebuild_tcid_index()
        else:
            # 单事件 append：若新 entry 是 running tool call，登记 index map
            if event.type == "agent_tool_call":
                tcid = (event.data or {}).get("tool_call_id")
                if tcid is not None:
                    self._tcid_to_entry_idx[tcid] = len(self._entries) - 1
        # last message 自动展开：替换 _expanded_seqs（spec §2.3 用户核心需求）
        if event.type == "agent_message":
            self._expanded_seqs = {event.seq}
        # phase-16 §2.4：全量 reflow（detail 内联在同一条 RichLog）
        self._reflow()

    def _build_entry_from_event(self, event: Event) -> _HistEntry:
        """从 canonical Event 派生 _HistEntry（spec §2.3 + §5.4 + phase-16 §2.2）。

        复用 ``_event_summary`` 共享纯函数；tool_call_id cache 已在调用前更新。
        phase-16：summary 不含 TYPE-LABEL 前缀（reflow 时按 kind 拼分级样式 + label）。
        """
        etype = event.type
        data = dict(event.data or {})
        # GAP-B/C：tool_result 反查 cache 派生 tool/args/elapsed
        if etype == "agent_tool_result":
            tcid = data.get("tool_call_id")
            cached = self._tool_call_cache.get(tcid) if tcid else None
            if cached is not None:
                cached_tool, cached_args, call_ts = cached
                elapsed = max(0.0, event.timestamp - call_ts)
                data.setdefault("tool", cached_tool)
                data.setdefault("args", cached_args)
                data.setdefault("elapsed", elapsed)
        # 复用 _event_summary 共享函数
        summary_text = _build_summary_line(etype, data)
        meta_text = _build_meta_line(etype, data)
        detail = _build_detail_renderable(etype, data, executor=self._executor)
        kind = _derive_kind(etype)
        # tool entry 状态：call=running；result=completed（失败检测见 _merge_result_into_entry）
        tool_status = "running" if etype == "agent_tool_call" else (
            "completed" if etype == "agent_tool_result" else None
        )
        tool_name = str(data.get("tool", "")) if kind == "tool" else None
        return _HistEntry(
            seq=event.seq,
            kind=kind,
            event_type=etype,
            timestamp=event.timestamp,
            summary=summary_text,
            meta=meta_text,
            detail=detail,
            tool_status=tool_status,
            tool_name=tool_name,
            merged=False,
            tool_call_id=tcid if (tcid := data.get("tool_call_id")) and kind == "tool" else None,
            call_args=data.get("args", {}) if kind == "tool" else {},
        )

    def _merge_result_into_entry(self, call_entry: _HistEntry, result_event: Event) -> _HistEntry:
        """就地升级：把 ``agent_tool_result`` 合进 call ToolEntry（phase-16 §2.2）。

        保持原 call 的 seq + 列表位置（``merged.seq = call.seq``）—— 不 remove+append，
        避免 ``_selected_seq`` dangling 指向已删 entry。

        数据来源：``call_entry`` 已存 ``tool_call_id`` / ``call_args`` / ``tool_name`` /
        ``timestamp``（fold 时落）；``_tool_call_cache`` 作 elapsed 派生用（call_ts 反查）。
        顺序无关：即使 result 早于 call（不应发生但防御），caller 已保证 call_entry 存在。

        合并后 entry：
          - ``seq`` = call.seq（不变）
          - ``kind`` = ``"tool"``（不变）
          - ``event_type`` = ``"agent_tool_call"``（标识 call 位）
          - ``merged`` = True
          - ``tool_status`` = ``"completed"``（``result.error`` 或非零 exit → ``"failed"``）
          - ``detail`` = 重建（用合并后的 data 调 render_tool；call+result 一起）
          - ``summary`` / ``meta`` 重建（含 elapsed / result lines）

        Args:
            call_entry: 原 call ToolEntry（running 态，``merged=False``）。
            result_event: 配对的 ``agent_tool_result`` Event。

        Returns:
            新的 merged ToolEntry（caller 负责写回 ``entries[i]``）。
        """
        result_data = dict(result_event.data or {})
        # elapsed：优先用 cache 反查 call_ts（GAP-B/C 主路径）
        call_ts = call_entry.timestamp
        tcid = call_entry.tool_call_id
        if tcid and tcid in self._tool_call_cache:
            call_ts = self._tool_call_cache[tcid][2]
        elapsed = max(0.0, result_event.timestamp - call_ts)
        merged_data = {
            "tool": call_entry.tool_name or "",
            "args": call_entry.call_args,
            "result": result_data.get("result", ""),
            "elapsed": elapsed,
        }
        # 保留 exit_code（若 translator 补了）
        if "exit_code" in result_data:
            merged_data["exit_code"] = result_data["exit_code"]
        # 失败检测：result.error 或 exit_code != 0 → failed
        is_failed = bool(result_data.get("error")) or (
            isinstance(merged_data.get("exit_code"), int)
            and merged_data["exit_code"] != 0
        )
        tool_status = "failed" if is_failed else "completed"

        # 重建 summary / meta / detail（用合并后的 data）
        summary_text = _build_summary_line("agent_tool_result", merged_data)
        meta_text = _build_meta_line("agent_tool_result", merged_data)
        detail = _build_detail_renderable(
            "agent_tool_result", merged_data, executor=self._executor,
        )
        return _HistEntry(
            seq=call_entry.seq,                # 保持原 seq（位置不变）
            kind="tool",
            event_type="agent_tool_call",      # 标识 call 位
            timestamp=call_entry.timestamp,    # call 时间戳（summary 行 HH:MM:SS）
            summary=summary_text,
            meta=meta_text,
            detail=detail,
            tool_status=tool_status,
            tool_name=call_entry.tool_name,
            merged=True,
            tool_call_id=call_entry.tool_call_id,
            call_args=call_entry.call_args,
        )

    def _update_tool_call_cache(self, event: Event) -> None:
        """维护 tool_call_cache（spec §2.3 GAP-B/C）。

        ``agent_tool_call`` 到达时填 cache：tcid → (tool, args, call_ts)；
        LRU 上限保护：超 cap 时丢最旧（FIFO 顺序，dict insertion order）。
        """
        if event.type != "agent_tool_call":
            return
        tcid = (event.data or {}).get("tool_call_id")
        if not tcid:
            return
        self._tool_call_cache[tcid] = (
            str((event.data or {}).get("tool", "")),
            (event.data or {}).get("args", {}) or {},
            event.timestamp,
        )
        # LRU 上限保护：超 cap 丢最旧（dict 保持 insertion order）
        if len(self._tool_call_cache) > _TOOL_CALL_CACHE_CAP:
            oldest = next(iter(self._tool_call_cache))
            self._tool_call_cache.pop(oldest, None)

    # ── 渲染（phase-16 §2.1 单流 inline + §2.3 视觉分级）──────────────

    def _reflow(self) -> None:
        """全量重渲：clear RichLog → 逐 entry 写 summary + 内联 detail（headless safe）。

        phase-16 §2.1：所有内容写进**一条** ``#agent-history-log``。
          - summary 行：``{sel}{expand} HH:MM:SS  {TYPE-LABEL}  {styled_summary}``
            - ``sel`` = ``▶`` / `` ``（光标条）
            - ``expand`` = ``▾``（展开）/ ``▸``（折叠）
            - TYPE-LABEL 视觉分级：message bold+主题色；thinking dim italic；tool 中性
          - 展开时紧跟 summary 行下方写 ``  ⎿`` + 缩进 detail renderable。
        """
        if self._log is None:
            return
        self._log.clear()
        for entry in self._entries:
            self._write_summary_line(entry)
            if entry.seq in self._expanded_seqs and entry.detail is not None:
                self._write_inline_detail(entry.detail)
        self._refresh_header()

    def _write_summary_line(self, entry: _HistEntry) -> None:
        """写一条 entry 的 summary 行到 RichLog（含分级样式 + 光标/展开标记）。

        phase-16 §2.3 视觉分级：
          - message：``MSG`` + bold + 主题色（``$success``）；summary 文本同色
          - thinking：``THINK`` + dim italic
          - tool：``TOOL`` + 中性色，前置 status icon ``✓/…/✗``
          - other：中性色
        """
        if self._log is None:
            return
        ts_str = time.strftime("%H:%M:%S", time.localtime(entry.timestamp))
        sel = "▶" if entry.seq == self._selected_seq else " "
        expanded = "▾" if entry.seq in self._expanded_seqs else "▸"
        type_label = _TYPE_LABELS.get(entry.event_type, entry.event_type[:5].upper())
        # 构建 Rich Text（含分级样式）；用 Text 拼接保样式分段
        prefix = f"{sel}{expanded} {ts_str}  "
        # tool entry 前置 status icon
        if entry.kind == "tool":
            icon = _TOOL_STATUS_ICON.get(entry.tool_status or "running", "…")
            summary_body = f"{icon} {entry.summary}"
        else:
            summary_body = entry.summary
        full_text = f"{prefix}{type_label:<5}  {summary_body}"
        # 分级样式（message bold 主题色 / thinking dim italic / tool+other 中性）
        style = self._style_for_kind(entry.kind)
        self._log.write(Text(full_text, style=style))
        # meta 行（tool 配对后的 lines/elapsed；message lines markdown）
        if entry.meta:
            meta_text = f"  {'':10}    {entry.meta}"
            self._log.write(Text(meta_text, style=style))

    def _style_for_kind(self, kind: EntryKind) -> str:
        """phase-16 §2.3 视觉分级样式映射（Rich style 字符串）。

        **必须用 Rich 原生颜色名**（如 ``green`` / ``cyan``），**不能**用 Textual 设计
        token（``$success`` 等）—— Rich 的 ``Style.parse`` 不识别 ``$token``，会把整个
        style 串当无效静默丢弃（实测 ``bold $success`` 渲染出零 ANSI 码 = 纯文本，导致
        message 与 tool 视觉无差异）。Textual token 只在 Textual CSS / ``Widget.styles``
        里有效。message 用 ``bold green``（对齐 AgentHistory ``border: round $success`` 的
        绿色基调 + 用户「MSG 突出」诉求）。
        """
        if kind == "message":
            return "bold green"
        if kind == "thinking":
            return "dim italic"
        return ""

    def _write_inline_detail(self, detail: RenderableType) -> None:
        """写展开 entry 的内联 detail（``  ⎿`` 引导 + 缩进 renderable）。

        phase-16 §2.1：detail renderable（render_tool / render_message / render_thinking
        产出）直接 ``RichLog.write(...)`` —— RichLog 接受任意 RenderableType。
        先写一行 ``  ⎿`` 引导符，再写 detail 本体（renderable 自带缩进/边框）。
        """
        if self._log is None:
            return
        # 引导行（缩进 + ⎿）；detail 本体作为独立 renderable 写入
        self._log.write(Text("  ⎿", style="dim"))
        self._log.write(detail)

    def _refresh_header(self) -> None:
        """刷新头部行：``{name} · {N} events · (⚠ truncated)``.

        Step 5 app.py 落地后，iter / status / elapsed / tok / cost 由 app 投影
        调 ``set_header_stats(...)`` 补全；Step 3 仅显 name + count + truncated。
        """
        if self._header_view is None:
            return
        if self._node_name is None:
            self._header_view.update("")
            return
        truncated = "  ⚠ truncated" if len(self._entries) >= _TRUNCATED_THRESHOLD else ""
        text = f"── {self._node_name} · {len(self._entries)} events{truncated} ──"
        self._header_view.update(text)

    # ── j/k 导航 + Enter 展开 ─────────────────────────────────────────

    def action_cursor_down(self) -> None:
        """j 键：选中下一条（不 wrap；末条不动）。"""
        if not self._entries:
            return
        if self._selected_seq is None:
            self._selected_seq = self._entries[0].seq
            self._reflow()
            return
        for i, e in enumerate(self._entries):
            if e.seq == self._selected_seq and i + 1 < len(self._entries):
                self._selected_seq = self._entries[i + 1].seq
                self._reflow()
                return

    def action_cursor_up(self) -> None:
        """k 键：选中上一条（不 wrap；首条不动）。"""
        if not self._entries:
            return
        if self._selected_seq is None:
            self._selected_seq = self._entries[-1].seq
            self._reflow()
            return
        for i, e in enumerate(self._entries):
            if e.seq == self._selected_seq and i > 0:
                self._selected_seq = self._entries[i - 1].seq
                self._reflow()
                return

    def action_toggle_expand(self) -> None:
        """Enter：toggle 当前选中 entry 的展开状态（spec §2.3 + reviewer P0-6 + phase-16 §2.4）。

        未选中（``_selected_seq is None``）时默认作用于**最后一条** entry——
        用户直接按 Enter 即可展开/收起当前 message，不必先 ↓ 选中（修复「Enter 没反应」
        体感 bug：旧逻辑在无选中时直接 return，用户不知要先导航）。

        phase-16 §2.4：因 detail 内联在同一条 RichLog，toggle 触发**全量 _reflow**
        （clear + rewrite），不能只 refresh 一个独立 detail 块。

        注：默认作用于 ``entries[-1]``，**任意 event_type**（可能是 message / tool /
        thinking）。这与 ``_compute_default_expanded`` 的「last **message** 自动展开」规则
        **有意区分**：自动展开只挑 message；Enter 无选中时作用于物理末条（用户当前关注点）。
        """
        seq = self._selected_seq
        if seq is None:
            if not self._entries:
                return
            seq = self._entries[-1].seq
        if seq in self._expanded_seqs:
            self._expanded_seqs.discard(seq)
        else:
            self._expanded_seqs.add(seq)
        # phase-16 §2.4：detail 内联 → 全量 reflow
        self._reflow()

    # ── 兼容旧 LogStream.write API（hint 行 / 占位提示等）─────────────

    def write(self, text: str) -> None:
        """兼容 v1.1.1 LogStream.write（占位提示用）。直接写 RichLog。"""
        if self._log is not None:
            self._log.write(text)


__all__ = ["AgentHistory", "_HistEntry", "_TYPE_LABELS", "EntryKind"]
