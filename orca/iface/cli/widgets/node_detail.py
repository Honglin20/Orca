"""node_detail.py —— 选中节点详情面板（phase-12 SPEC §1.3 §4.2 §6.3，替换 active_node.py）。

回答「这个节点在干嘛 / 产出了啥？」：tab 化（``流式`` / ``输出`` / ``图表(n)``），
6 种节点 kind（agent/script/set/foreach/wait/terminate）**永不空白**。

设计原则：
  - **壳无真相**：所有内容由注入的事件派生；不订阅 bus。
  - **executor-agnostic 流式**：N 个 ``agent_*`` 事件 → N 行（不预设 thinking/message
    齐备；claude 多 thinking 行、opencode 仅 message 行都正确显示，SPEC §6.3）。
  - **● 徽标确定性**：新内容到非当前 tab → 置位；``Tab.Activated`` 切到该 tab → 清除。
  - **依赖单向**：仅 import textual + stdlib + 本包 widget；**不** import ``orca.exec``
    / ``orca.run`` / ``orca.iface.mcp`` / chart-producer（SPEC §0.3）。

phase-15 render layer（render-layer-design-draft §7.3）：
  - 工具事件（agent_tool_call/result）→ ``tool_render.normalize_tool`` +
    ``render_tool`` 渲染为 Rich tool card（file_read/edit/.../unknown）。
  - 非工具事件 → 单行摘要（``_format_stream_line``）。log_stream 的工具单行摘要
    走 ``tool_render.describe_tool_event``（v1 仅 log_stream 用；node_detail 工具
    事件走 Rich card 不需要单行摘要）。
  - ``_stream_lines`` 容纳 ``str | RenderItem | _ThinkingChunk``（保留 list 语义：N 事件 → N 行/卡）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from rich.console import Group, RenderableType

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.dom import NoMatches
from textual.widgets import Static, TabbedContent, TabPane
from textual.widgets._tabs import Tabs

from orca.iface.cli.widgets.chart_panel import ChartPanel, WORKFLOW_BUCKET
from orca.iface.cli.widgets.tool_render import (
    normalize_tool,
    render_tool,
)
from orca.iface.cli.widgets.tool_render.kinds import render_thinking
from orca.schema import RenderItem


@dataclass
class _ThinkingChunk:
    """agent_thinking 累积文本（phase-15 render layer §12.8）。

    render 时由 ``_refresh_stream`` 按 ``_thinking_visible`` 切：
      - True → ``render_thinking(text)``（dim+italic 纯文本，spec §12.8）
      - False → 不渲染（仍累积，保可重建性）

    累积语义：reducer 把多个 agent_thinking 事件文本拼到同一 chunk（spec §9.2）。
    v1 简化：每个 agent_thinking 事件一个独立 chunk（视觉上每个事件一段 dim 文本）。
    """

    text: str

logger = logging.getLogger(__name__)

# 6 种节点 kind（SPEC §1.3 表）。逐字对齐 schema/workflow.AnnotatedNode。
_NODE_KINDS = {"agent", "script", "set", "foreach", "wait", "terminate"}

# Tab id 常量（确定性，单测 + Tab.Activated 派发用）。
TAB_STREAM = "stream"
TAB_OUTPUT = "output"
TAB_CHARTS = "charts"

# 默认 tab（SPEC §1.3）。
_DEFAULT_TAB = TAB_STREAM


def _format_stream_line(etype: str, data: dict) -> str:
    """流式 tab 单行格式（executor-agnostic：按 etype 派生描述，不预设种类齐备）。

    SPEC §6.3：N 个 agent_* 事件 → N 行。每个事件一行（kind 标签 + 摘要）。

    phase-15 render layer §7.3：工具事件（agent_tool_call/result）**不在本函数处理**
    （由 ``append_event_stream`` 单独走 ``tool_render.render_tool`` 出 Rich tool card）。
    本函数仅处理其余 etype（message/thinking/foreach/wait/node_started）。
    """
    kind_tag = {
        "agent_message": "msg",
        "agent_thinking": "think",
        "foreach_started": "fe▶",
        "foreach_completed": "fe✓",
        "foreach_item_started": "fe-item▶",
        "foreach_item_completed": "fe-item✓",
        "wait_started": "wait▶",
        "wait_completed": "wait✓",
        "node_started": "start",
    }.get(etype, etype)
    text = data.get("text") or data.get("reason") or ""
    if etype == "foreach_started":
        text = f"items={data.get('item_count')} concurrent={data.get('max_concurrent')}"
    elif etype == "foreach_completed":
        text = f"count={data.get('count')} succeeded={data.get('succeeded')}"
    elif etype == "foreach_item_started":
        text = f"#{data.get('index')} key={_truncate(data.get('item_key'))}"
    elif etype == "wait_started":
        text = f"{data.get('duration_seconds')}s · {data.get('reason')}"
    elif etype == "wait_completed":
        text = f"{data.get('elapsed_seconds')}s interrupted={data.get('interrupted')}"
    elif etype == "node_started":
        text = f"kind={data.get('kind')} status={data.get('status', 'running')}"
    return f"[{kind_tag}] {text}".rstrip()


def _truncate(v: Any, width: int = 80) -> str:
    s = str(v) if v is not None else ""
    if len(s) > width:
        return s[: width - 1] + "…"
    return s


def _truncate_args(args: Any, width: int = 60) -> str:
    s = str(args) if args is not None else ""
    if len(s) > width:
        return s[: width - 1] + "…"
    return s


class NodeDetail(Static):
    """选中节点详情：流式/输出/图表 tab（SPEC §1.3 / §4.2）。

    用法（由 OrcaApp 驱动）::

        nd = app.query_one(NodeDetail)
        nd.set_node("analyze", kind="agent")    # 切换选中节点
        nd.append_stream("analyze", "[msg] ...")  # 流式（仅当 node==_selected 显示）
        nd.set_output("analyze", {...})            # node_completed
        nd.upsert_chart("analyze", payload)        # custom(chart)
    """

    DEFAULT_CSS = """
    NodeDetail {
        height: 3fr;
        border: round $accent;
        padding: 0 1;
        background: $surface;
    }
    NodeDetail TabbedContent {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("c", "action_focus_charts", "图表 tab", show=False),
    ]

    def __init__(self) -> None:
        super().__init__("", id="node-detail")
        self._selected: str | None = None
        self._kind: str | None = None
        # node -> list[stream line / Rich renderable]（按节点缓存；切回时显示历史）。
        # phase-15 render layer：
        #   - agent_tool_call/result → RenderItem（render 时 render_tool 出 card）
        #   - agent_thinking → _ThinkingChunk（render 时按 _thinking_visible 切，spec §12.8）
        #   - 其余 etype → str 单行摘要（_format_stream_line）
        # spec §7.3 边界 a：维持 list 语义（N 事件 → N entries）。
        self._stream_lines: dict[str, list[str | RenderItem | _ThinkingChunk]] = {}
        # node -> output（node_completed.data.output）。
        self._outputs: dict[str, Any] = {}
        # ● 徽标：tab id -> dirty。
        self._dirty: dict[str, bool] = {TAB_STREAM: False, TAB_OUTPUT: False, TAB_CHARTS: False}
        self._active_tab: str = _DEFAULT_TAB
        self._chart_panel = ChartPanel()
        # phase-15 render layer：当前 backend（normalize_tool 查 §6.1 表用）。
        # 由 OrcaApp 在节点切换 / workflow_started 时 set_executor 设。
        self._executor: str = "claude"  # 默认 claude（v1 安全默认；OrcaApp 会覆写）
        # phase-15 render layer §12.8：thinking 全局可见性（``/thinking`` 命令切换）。
        # 默认 True（thinking 默认展开）。False 时 thinking 行不渲染（但仍累积在 _stream_lines）。
        self._thinking_visible: bool = True

    # ── Textual compose（嵌 TabbedContent + ChartPanel）────────────────────

    def compose(self) -> ComposeResult:
        with TabbedContent(initial=_DEFAULT_TAB):
            with TabPane("流式", id=TAB_STREAM):
                yield Static("", id="nd-stream")
            with TabPane("输出", id=TAB_OUTPUT):
                yield Static("", id="nd-output")
            with TabPane("图表", id=TAB_CHARTS):
                yield self._chart_panel

    @on(Tabs.TabActivated)
    def _on_tab_activated(self, event: Tabs.TabActivated) -> None:
        """SPEC §1.3 / §6.3 确定性语义：Tab.Activated 切到 → 清该 tab ●。"""
        # event.tab.id 形如 "stream" / "output" / "charts"（TabPane id 同 tab id）。
        tab_id = str(event.tab.id) if event.tab is not None else ""
        if tab_id in self._dirty:
            self._active_tab = tab_id
            self._dirty[tab_id] = False
            self._refresh_tab_labels()

    # on decorator：在类外部用 ``from textual import on`` 不便时，此处直接绑定。
    # textual 的 @on(Tabs.TabActivated) 会捕获本 TabbedContent 内 tab 切换事件。
    # （textual 自动按 widget 树派发，无需 manual connect。）

    # ── 节点切换 ──────────────────────────────────────────────────────────

    def set_node(self, name: str | None, kind: str | None = None) -> None:
        """切换选中节点（v2 AgentsList.select / auto-follow 调；v1.1.1 DagGraph 已删）。

        SPEC §1.4：``_selected`` 驱动全部 tab 内容（流式/输出/图表都按它过滤）。
        kind 决定流式/输出 tab 的数据源（SPEC §1.3 表）；``node_started`` 的 kind
        仅在 agent/script 路径可靠（foreach 无顶层 node_started kind），故 kind 由
        app 从 ``wf.nodes`` 静态派生传入（SPEC §3.1），不读 ``data.kind``。
        """
        self._selected = name
        if kind is not None:
            self._kind = kind
        self._chart_panel.set_node(name)
        self._refresh_stream()
        self._refresh_output()
        self._refresh_chart_label()

    # 兼容别名（减小 app.py diff，SPEC §4.2 兼容）。
    def set_active(self, name: str | None) -> None:
        self.set_node(name)

    @property
    def active(self) -> str | None:
        return self._selected

    @property
    def kind(self) -> str | None:
        return self._kind

    @property
    def active_tab(self) -> str:
        return self._active_tab

    @property
    def dirty(self) -> dict[str, bool]:
        """● 徽标状态（测试断言用）。"""
        return dict(self._dirty)

    # ── 流式 tab（executor-agnostic）────────────────────────────────────────

    def append_stream(self, node: str, line: str) -> None:
        """追加流式行到 ``node`` 的缓存；仅当 ``node==_selected`` 显示。

        SPEC §6.3：N 个 agent_* 事件 → N 行（append N 次 = N 行）。流式 tab 非当前 → 置 ●。
        """
        self._stream_lines.setdefault(node, []).append(line)
        if node == self._selected:
            self._refresh_stream()
            if self._active_tab != TAB_STREAM:
                self._dirty[TAB_STREAM] = True
                self._refresh_tab_labels()

    # 兼容别名：append_line(line) → append_stream(_selected, line)。
    def append_line(self, line: str) -> None:
        if self._selected is not None:
            self.append_stream(self._selected, line)

    def append_event_stream(self, node: str, etype: str, data: dict) -> None:
        """把 agent_* / foreach_* / wait_* / node_started 事件格式化为流式行（SPEC §1.3）。

        executor-agnostic：直接按 etype 派生描述，不预设种类。``data`` 缺字段 → 空串兜底。

        phase-15 render layer §7.3：
          - agent_tool_call/result → 走 ``tool_render.normalize_tool`` 出 RenderItem
            （render 时由 ``render_tool`` 派发为 Rich tool card，spec §8）。
          - agent_thinking → ``_ThinkingChunk``（render 时按 ``_thinking_visible`` 切，
            spec §12.8：dim+italic 纯文本，不渲染 markdown）
          - 其余 etype → 单行摘要 str（``_format_stream_line``）。
          - spec §7.3 边界 a：N 事件 → N entries（list 长度守恒，既有 len() 测试不变）。
        """
        if etype in ("agent_tool_call", "agent_tool_result"):
            entry: str | RenderItem | _ThinkingChunk = self._build_tool_render_item(etype, data)
        elif etype == "agent_thinking":
            entry = _ThinkingChunk(text=str(data.get("text", "")))
        else:
            entry = _format_stream_line(etype, data)
        self.append_stream(node, entry)

    def _build_tool_render_item(self, etype: str, data: dict) -> RenderItem:
        """tool_call/result → RenderItem（phase-15 render layer §3.1 / §6.2）。

        - ``agent_tool_call`` → ``status=running``，``result=None``
        - ``agent_tool_result`` → ``status=completed``，``result=data.result``

        spec §13 fail loud：args 非 dict → ``NormalizeError`` 由 normalizer 抛，本层不兜底
        （translator 层应保证 args 已 dict）。
        """
        if etype == "agent_tool_call":
            return normalize_tool(
                executor=self._executor,
                tool_name=str(data.get("tool", "")),
                args=data.get("args", {}) or {},
                result=None,
                status="running",
            )
        # agent_tool_result
        result = data.get("result")
        result_str = result if isinstance(result, str) else ("" if result is None else str(result))
        return normalize_tool(
            executor=self._executor,
            tool_name=str(data.get("tool", "")),  # tool_result.data 不带 tool 字段时兜底空
            args=data.get("args", {}) or {},
            result=result_str,
            status="completed",
        )

    def set_executor(self, executor: str) -> None:
        """设置当前 backend（``claude`` / ``opencode`` / ``codex`` / ...）。

        phase-15 render layer §6.1：normalize_tool 查 ``(executor, tool) → kind`` 表，
        故 widget 需知道 executor。OrcaApp 在 workflow_started / executor 切换时调。
        """
        self._executor = executor or "claude"

    @property
    def thinking_visible(self) -> bool:
        """phase-15 §12.8：``/thinking`` 命令查询当前可见性。"""
        return self._thinking_visible

    def toggle_thinking(self) -> bool:
        """``/thinking`` 命令切换全局可见性（spec §12.8）。

        返回切换后的状态（True=可见 / False=隐藏），由 OrcaApp notify 用户。
        """
        self._thinking_visible = not self._thinking_visible
        self._refresh_stream()
        return self._thinking_visible

    # ── 输出 tab ──────────────────────────────────────────────────────────

    def set_output(self, node: str, output: Any) -> None:
        """``node_completed`` 时调（SPEC §1.3 输出 tab）。仅当 ``node==_selected`` 显示。"""
        self._outputs[node] = output
        if node == self._selected:
            self._refresh_output()
            if self._active_tab != TAB_OUTPUT:
                self._dirty[TAB_OUTPUT] = True
                self._refresh_tab_labels()

    # ── 图表 tab（转发内部 ChartPanel）──────────────────────────────────────

    def upsert_chart(self, node_key: str, payload: dict) -> None:
        """``custom(chart)`` 分支调（转发内部 ChartPanel）。"""
        self._chart_panel.upsert(node_key, payload)
        if node_key == self._selected:
            self._refresh_chart_label()
            if self._active_tab != TAB_CHARTS:
                self._dirty[TAB_CHARTS] = True
                self._refresh_tab_labels()

    def all_charts(self):
        """转发 ChartPanel.all_charts()（ChartBrowser 公共 API，不读 _projection 私有）。"""
        return self._chart_panel.all_charts()

    # ── 渲染 ──────────────────────────────────────────────────────────

    def _refresh_stream(self) -> None:
        """刷新流式 tab 内容。SPEC §1.3：6 kind 永不空白（running 无输出时显提示）。

        phase-15 render layer §7.3：``_stream_lines`` 容纳 str（单行摘要）+ RenderItem
        （tool card）。render 时按类型分派：str → 直接拼；RenderItem → render_tool 出
        Rich renderable。最终用 Group 组装，feed 给 Static.update（textual 支持 RenderableType）。
        """
        try:
            static = self.query_one("#nd-stream", Static)
        except NoMatches:  # compose 未跑（headless 非 run_test 时）—— query_one 找不到子 widget
            return
        node = self._selected
        if node is None:
            static.update("（未选中节点）")
            return
        entries = self._stream_lines.get(node, [])
        if not entries:
            # 永不空白：running 无输出时显提示（SPEC §1.3）。
            kind = self._kind or "?"
            static.update(f"（{node} · {kind} · running，尚无输出）")
            return

        # 组装：str 摘要直拼为多行文本；RenderItem → render_tool；_ThinkingChunk →
        # 按 _thinking_visible 切（spec §12.8）。
        # Group 接受混合 RenderableType，textual Static 支持。
        parts: list[RenderableType] = []
        text_lines: list[str] = []
        for entry in entries:
            if isinstance(entry, str):
                text_lines.append(entry)
            elif isinstance(entry, _ThinkingChunk):
                if not self._thinking_visible:
                    continue  # §12.8：thinking_visible=False → 不渲染（仍累积在 _stream_lines）
                if text_lines:
                    parts.append("\n".join(text_lines))
                    text_lines = []
                parts.append(render_thinking(entry.text))
            else:  # RenderItem
                if text_lines:
                    parts.append("\n".join(text_lines))
                    text_lines = []
                parts.append(render_tool(entry))
        if text_lines:
            parts.append("\n".join(text_lines))

        if not parts:
            # 全部被过滤（thinking 不可见 + 全 thinking 事件）→ 提示文本（永不空白，§1.3）。
            static.update("（thinking 已隐藏，按 /thinking 切换可见性）")
            return
        body: RenderableType = parts[0] if len(parts) == 1 else Group(*parts)
        static.update(body)

    def _refresh_output(self) -> None:
        try:
            static = self.query_one("#nd-output", Static)
        except NoMatches:
            return
        node = self._selected
        if node is None:
            static.update("（未选中节点）")
            return
        out = self._outputs.get(node)
        if out is None:
            static.update("（尚无输出）")
            return
        if isinstance(out, dict):
            # 结构化输出：key=value 行。
            text = "\n".join(f"{k}: {_truncate(v, 100)}" for k, v in out.items())
        else:
            text = _truncate(out, 500)
        static.update(text)

    def _refresh_chart_label(self) -> None:
        """图表 tab 标题显 ``图表(n)``（SPEC §1.3）。"""
        try:
            tc = self.query_one(TabbedContent)
        except NoMatches:
            return
        node = self._selected
        count = self._chart_panel.count_for(node) if node else 0
        # TabbedContent 的 tab label 经内部 tab pane title 改。
        # textual API：tc.get_tab(id) -> Tab；改 label 用 tc.get_widget(id) 或重命 pane。
        # 简化：直接改 TabPane title（textual 支持 TabPane.title 属性 + reactive）。
        try:
            pane = tc.get_pane(TAB_CHARTS)
            pane.title = f"图表({count})" if count else "图表"
        except NoMatches:  # API 兼容兜底（pane 不存在）
            pass

    def _refresh_tab_labels(self) -> None:
        """刷新 ● 徽标（dirty tab 前缀 ●）。"""
        try:
            tc = self.query_one(TabbedContent)
        except NoMatches:
            return
        # 流式 / 输出 的 ● 前缀。
        for tab_id, base in [(TAB_STREAM, "流式"), (TAB_OUTPUT, "输出")]:
            try:
                pane = tc.get_pane(tab_id)
                dirty = self._dirty.get(tab_id, False)
                pane.title = f"● {base}" if dirty else base
            except NoMatches:
                pass
        # 图表 tab：● + (n)。
        node = self._selected
        count = self._chart_panel.count_for(node) if node else 0
        chart_dirty = self._dirty.get(TAB_CHARTS, False)
        try:
            pane = tc.get_pane(TAB_CHARTS)
            prefix = "● " if chart_dirty else ""
            pane.title = f"{prefix}图表({count})" if count else f"{prefix}图表"
        except NoMatches:
            pass

    # ── c 键：聚焦 + 切图表 tab ─────────────────────────────────────────────

    def action_focus_charts(self) -> None:
        """SPEC §5：``c`` = 聚焦 NodeDetail + 切「图表」tab。

        fail loud：TabbedContent 未 mount（compose 未跑）时记 warning（不静默吞）。
        """
        try:
            tc = self.query_one(TabbedContent)
        except NoMatches:
            logger.warning("c 键：NodeDetail TabbedContent 未 mount，无法切图表 tab")
            return
        tc.active = TAB_CHARTS
        self._active_tab = TAB_CHARTS
        self._dirty[TAB_CHARTS] = False
        self._refresh_tab_labels()


__all__ = ["NodeDetail"]
