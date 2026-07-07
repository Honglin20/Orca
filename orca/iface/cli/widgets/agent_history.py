"""agent_history.py —— v2 右上 70% Agent History widget（spec §2.3，Conductor Activity 风格）。

回答「这个 agent 干了什么？最后说了什么？」：单 agent 视图，双行 entry +
折叠详情（Conductor Activity 风格）。last message 默认展开（用户核心需求）。

设计原则：
  - **壳无真相**：widget 只持 ``_entries: list[_HistEntry]`` + ``_expanded_seqs``，
    由 app ``set_node()`` / ``append_event()`` 注入；不订阅 bus。
  - **last message 默认展开**（用户核心需求）：``_expanded_seqs`` set 含
    当前 agent 最后一条 ``agent_message`` 的 seq，每次 ``set_node`` 重置 / 每次
    新 message 到达自动替换（spec §2.3）。
  - **Enter 切换折叠**（reviewer P0-6）：Tab 与 Textual ``focus_next`` 冲突，
    改用 Enter（Textual activate 默认键）。
  - **set_node 浅拷贝**（reviewer P1-7）：``[build_entry(e) for e in events]``
    不引用原 list；后续 app 层 ``_node_events[node].append`` 不污染 widget 内部。
  - **依赖单向**：仅 import ``orca.schema`` + textual + rich + stdlib + 本包
    ``_event_summary`` / ``_icons``；**禁止** ``orca.exec`` / ``orca.run`` / ``orca.events.bus``。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from rich.console import RenderableType
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import RichLog, Static

from orca.iface.cli.widgets._event_summary import (
    _build_detail_renderable,   # 折叠详情（调 phase-15 render_tool / render_message / render_thinking）
    _build_meta_line,           # 双行 entry 第 2 行
    _build_summary_line,        # 双行 entry 第 1 行
)
from orca.schema import Event

# spec §2.3 6 TYPE-LABEL（6 字符宽，对齐双行 entry 第 1 行）
_TYPE_LABELS: dict[str, str] = {
    "agent_thinking":            "THINK",
    "agent_tool_call":           "TOOL →",
    "agent_tool_result":         "TOOL ←",
    "agent_message":             "MSG",
    "human_decision_requested":  "GATE",
    "human_decision_resolved":   "GATE",
    "interrupt_requested":       "INT",
    "interrupt_resolved":        "INT",
}

# spec §2.3 tool_call_id cache LRU 上限（GAP-B/C 修复机制，spec §2.3）
_TOOL_CALL_CACHE_CAP = 500

# 头部行 truncated 标记阈值（spec §9 R3 FIFO 上限是 1000 events/node）
_TRUNCATED_THRESHOLD = 1000


@dataclass
class _HistEntry:
    """单条 AgentHistory entry（迁自 v1.1.1 ActivityEntry）。

    每条 entry 对应一个 Event；持有：
      - ``seq``：Event seq（排序 + 唯一标识）
      - ``event_type``：EventType 名（派生 type_label）
      - ``timestamp``：Event timestamp（双行 entry 第 1 行 HH:MM:SS）
      - ``summary``：双行 entry 第 1 行内容（type_label + summary 文本）
      - ``meta``：双行 entry 第 2 行（可空）
      - ``detail``：折叠块 Rich renderable（None = 无折叠）
    """
    seq: int
    event_type: str
    timestamp: float
    summary: str
    meta: str = ""
    detail: RenderableType | None = None


class AgentHistory(Static):
    """v2 右上 Agent 历史流（spec §2.3，Conductor Activity 风格）。

    用法（由 OrcaApp 驱动）::

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
    AgentHistory VerticalScroll { height: 1fr; }
    #agent-history-log {
        height: 1fr;
    }
    #agent-history-detail-wrap {
        height: auto;
        max-height: 50%;
        border-top: solid $accent;
        padding: 0 1;
        background: $panel;
    }
    #agent-history-detail {
        height: auto;
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
        # 内部 widget 引用（compose 后挂载；headless 测试时为 None）
        self._log: RichLog | None = None
        self._detail_view: Static | None = None
        self._header_view: Static | None = None

    # ── Textual 钩子 ──────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        # 头部行（agent 名 + entry count + 截断标记；Step 5 app 投影 iter/status/tok/cost）
        yield Static("", id="agent-history-header")
        with Vertical():
            yield RichLog(id="agent-history-log", markup=False, wrap=True, auto_scroll=True)
            # detail 包在 VerticalScroll 里：长 report（report_painter 的 Markdown）可滚动，
            # 不再被 Static + max-height 截断（B 阶段会改成 inline 流，此处最小可用）。
            with VerticalScroll(id="agent-history-detail-wrap"):
                yield Static("", id="agent-history-detail")

    def on_mount(self) -> None:
        self._log = self.query_one("#agent-history-log", RichLog)
        self._detail_view = self.query_one("#agent-history-detail", Static)
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
        """切换 agent + 全量重渲（spec §2.3 + §3）。

        Args:
            name: 新 agent 名（None = 空）。
            events: 该 agent 的全部事件列表（按 seq 排序）。

        强制 reset（避免旧 agent 状态残留，reviewer P1-8）：
          - ``_expanded_seqs`` 重置为 ``{last_agent_message_seq if any else set()}``
          - ``_selected_seq`` 重置为 ``None``
          - ``_tool_call_cache`` 重置为 ``{}``（per-node 切换时清空）
        """
        self._node_name = name
        # 切换 agent 时清空 cache，避免上一个 agent 的 tcid 残留（per-node 隔离）
        self._tool_call_cache.clear()
        # 先填 cache（让后续 _build_entry_from_event 能反查 tool_result 的 call 配对）
        for e in events:
            self._update_tool_call_cache(e)
        # reviewer P1-7：不引用原 list；_build_entry_from_event 内不存 list 引用，
        # entries 是新构造的 _HistEntry 列表，与传入 list 完全解耦
        self._entries = [self._build_entry_from_event(e) for e in events]
        # 排序（防御乱序，reducer fold 性质保证一致）
        self._entries.sort(key=lambda e: e.seq)
        # reviewer P1-8：reset _expanded_seqs 到 last message
        self._expanded_seqs = self._compute_default_expanded()
        self._selected_seq = None
        self._reflow()

    def _compute_default_expanded(self) -> set[int]:
        """返回含最后一条 agent_message seq 的 set（无 message 返回空 set）。"""
        for entry in reversed(self._entries):
            if entry.event_type == "agent_message":
                return {entry.seq}
        return set()

    # ── 增量追加（spec §3 仅 selected_node）──────────────────────────

    def append_event(self, event: Event) -> None:
        """追加单条 event + 增量渲染（spec §3）。

        调用方（OrcaApp._dispatch_to_widgets）负责过滤 ``event.node == _selected_node``。
        本方法假设 event 已属于当前 agent。

        ``agent_message`` 到达时：
          - ``_expanded_seqs`` **替换**为 ``{new_seq}``（不是 add，spec §2.3 last message 自动展开）
          - 旧的 message seq 自动从 set 移除

        ``agent_tool_call`` 到达时填 ``_tool_call_cache``；
        ``agent_tool_result`` 到达时反查 cache 派生 ``(tool, args, elapsed)``（GAP-B/C）。
        """
        # 维护 tool_call_cache（先于 _build_entry，让 result 能反查到 call）
        self._update_tool_call_cache(event)
        # 派生 entry
        entry = self._build_entry_from_event(event)
        self._entries.append(entry)
        # 保持 seq 排序（防御乱序）
        if len(self._entries) >= 2 and self._entries[-2].seq > self._entries[-1].seq:
            self._entries.sort(key=lambda e: e.seq)
        # last message 自动展开：替换 _expanded_seqs（spec §2.3 用户核心需求）
        if entry.event_type == "agent_message":
            self._expanded_seqs = {entry.seq}
        # 渲染追加（不全 reflow，性能优化）
        self._append_entry_to_log(entry)
        self._refresh_header()
        self._refresh_detail()

    def _build_entry_from_event(self, event: Event) -> _HistEntry:
        """从 canonical Event 派生 _HistEntry（spec §2.3 + §5.4 字段级定义）。

        复用 ``_event_summary`` 共享纯函数；tool_call_id cache 已在调用前更新。
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
        type_label = _TYPE_LABELS.get(etype, etype[:6].upper())
        full_summary = f"{type_label:<6}  {summary_text}"
        detail = _build_detail_renderable(etype, data, executor=self._executor)
        return _HistEntry(
            seq=event.seq,
            event_type=etype,
            timestamp=event.timestamp,
            summary=full_summary,
            meta=meta_text,
            detail=detail,
        )

    def _update_tool_call_cache(self, event: Event) -> None:
        """维护 tool_call_id cache（spec §2.3 GAP-B/C）。

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

    # ── 渲染 ──────────────────────────────────────────────────────────

    def _reflow(self) -> None:
        """全量重渲（set_node 调；headless 测试时 _log is None 安全 skip）。"""
        if self._log is None:
            return
        self._log.clear()
        for entry in self._entries:
            self._append_entry_to_log(entry)
        self._refresh_header()
        self._refresh_detail()

    def _append_entry_to_log(self, entry: _HistEntry) -> None:
        """写一条 entry 到 RichLog（双行 + 选中标记）。

        ``HH:MM:SS`` 来自 ``entry.timestamp``（spec §2.3 entry 第 1 行；修复 plan
        中错误的 ``time.localtime(entry.seq)`` —— seq 不是 timestamp）。
        """
        if self._log is None:
            return
        ts_str = time.strftime("%H:%M:%S", time.localtime(entry.timestamp))
        sel = "▶" if entry.seq == self._selected_seq else " "
        expanded = "▾" if entry.seq in self._expanded_seqs else "▸"
        line1 = f"{sel}{expanded} {ts_str}  {entry.summary}"
        self._log.write(line1)
        if entry.meta:
            line2 = f"  {'':10}    {entry.meta}"
            self._log.write(line2)

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

    def _refresh_detail(self) -> None:
        """刷新底部 detail_view：叠加所有 expanded entry 的折叠详情。"""
        if self._detail_view is None:
            return
        details = [
            e.detail for e in self._entries
            if e.seq in self._expanded_seqs and e.detail is not None
        ]
        if not details:
            self._detail_view.update("")
            return
        from rich.console import Group
        self._detail_view.update(Group(*details))

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
        """Enter：toggle 当前选中 entry 的展开状态（spec §2.3 + reviewer P0-6）。

        未选中（``_selected_seq is None``）时默认作用于**最后一条** entry——
        用户直接按 Enter 即可展开/收起当前 message，不必先 ↓ 选中（修复「Enter 没反应」
        体感 bug：旧逻辑在无选中时直接 return，用户不知要先导航）。不移动光标，避免触发
        全量 reflow（长跑性能）；detail 面板刷新即给出展开/收起反馈。

        注：默认作用于 ``entries[-1]``，**任意 event_type**（可能是 message / tool_result /
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
        self._refresh_detail()

    # ── 兼容旧 LogStream.write API（hint 行 / 占位提示等）─────────────

    def write(self, text: str) -> None:
        """兼容 v1.1.1 LogStream.write（占位提示用）。直接写 RichLog。"""
        if self._log is not None:
            self._log.write(text)


__all__ = ["AgentHistory", "_HistEntry", "_TYPE_LABELS"]
