"""agents_list.py —— v2 左 30% Agents List widget（spec §2.2）。

回答「workflow 里有哪些 agent？现在跑到哪个？」：拓扑序纵向列表，每行
``{sel_mark} {name} {icon} {elapsed}`` + 第二行 ``  {tok}``。j/k 切换选中。

设计原则：
  - **壳无真相**：widget 只持 ``node_names`` + ``_projections: dict[str, NodeProj]``，
    由 app ``update_node()`` 注入；不订阅 bus、不读 tape、不解析 Event。
  - **拓扑序静态**：build() 时一次性接收 node_names，顺序固定（拓扑序由 app 派生）。
  - **依赖单向**：仅 import textual + stdlib + 本包 ``_icons``；
    **不** import ``orca.exec`` / ``orca.run`` / ``orca.events.bus``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from textual.binding import Binding
from textual.widgets import Static

from orca.iface.cli.widgets._icons import NODE_STATUS_ICONS
from orca.schema.state import Status


@dataclass
class NodeProj:
    """单 agent 的渲染投影（spec §2.2 字段）。仅 widget 内部状态，由 update_node 派生。"""
    name: str
    # Status 是 canonical Literal（schema 层权威）；widget 不自造 status 字符串（P4）。
    # ``blocked`` / ``running`` 等值由 app.dispatch 经 ``projections.node_status`` 派生后注入。
    status: Status = "pending"
    elapsed: float | None = None   # 秒
    tokens: int | None = None
    iter_n: int = 1
    error_msg: str | None = None


class AgentsList(Static):
    """v2 左侧 agent 列表（spec §2.2）。

    用法（由 OrcaApp 驱动）::

        lst = app.query_one(AgentsList)
        lst.build(["analyzer", "configurator", "runner", ...])
        lst.update_node("analyzer", status="running", iter_n=1)
        lst.update_node("analyzer", status="done", elapsed=14.0, tokens=1234)
        lst.select("configurator")    # 用户 j/k 选中
    """

    DEFAULT_CSS = """
    AgentsList {
        width: 30%; min-width: 20; max-width: 40;
        border: round $primary;
        padding: 0 1;
        background: $surface;
    }
    """

    BINDINGS = [
        # spec v2 §2.2：j/k 切 agent。原 widget 级 BINDINGS 在 ``Static`` 默认
        # ``can_focus=False`` 时不触发；改为 OrcaApp 级 BINDINGS 上提（spec §2.2）。
        # 单测通道保留：``test_widgets.py`` 直接调 ``action_select_next/prev``。
    ]

    def __init__(self) -> None:
        super().__init__("", id="agents-list")
        # 拓扑序 node 名（build 时确定，不变）
        self._node_names: list[str] = []
        # 渲染投影 dict（按 name 索引；update_node 修改）
        self._projections: dict[str, NodeProj] = {}
        # 当前选中（None=未选中）
        self._selected: str | None = None

    # ── 初始化 ──────────────────────────────────────────────────────────

    def build(self, node_names: Iterable[str]) -> None:
        """从拓扑序 node 名构造（一次性，幂等）。"""
        names = list(node_names)
        self._node_names = names
        self._projections = {n: NodeProj(name=n) for n in names}
        # 默认选中第一个（无选中时 j/k 从头开始）
        self._selected = names[0] if names else None
        self._rerender()

    # ── 投影更新（由 app dispatch 调）──────────────────────────────────

    def update_node(
        self,
        name: str,
        *,
        status: Status | None = None,
        elapsed: float | None = None,
        tokens: int | None = None,
        error_msg: str | None = None,
        iter_n: int | None = None,
    ) -> None:
        """更新单 agent 的投影字段（spec §2.2）。None 字段不修改。幂等。"""
        proj = self._projections.get(name)
        if proj is None:
            return  # 未知节点防御（不抛；调用方传未注册 name 时不污染既有投影）
        if status is not None and status in NODE_STATUS_ICONS:
            proj.status = status
        if elapsed is not None:
            proj.elapsed = elapsed
        if tokens is not None:
            proj.tokens = tokens
        if error_msg is not None:
            proj.error_msg = error_msg
        if iter_n is not None:
            proj.iter_n = iter_n
        self._rerender()

    def projection_of(self, name: str) -> NodeProj | None:
        """读单 agent 当前投影（测试用，DRY 通道）。"""
        return self._projections.get(name)

    # ── 选中（j/k / app 调）─────────────────────────────────────────────

    def select(self, name: str | None) -> None:
        """选中某 agent + 通知 app（spec §2.2）。

        SPEC：用户 j/k 或点选 → ``_auto_follow=False``；``_selected_node=picked``。
        本 widget 只设本地 ``_selected`` + 通知 app（app 负责 ``_auto_follow`` + 驱动 AgentHistory）。
        """
        if name is None or name not in self._projections:
            return
        self._selected = name
        self._rerender()
        # 通知 app：用 duck-typing 拿 app 句柄（getattr 兜底），
        # 避免 widget 反向 import app 模块（依赖单向：widgets 不依赖 app）。
        app = self.app
        handler = getattr(app, "_on_node_selected", None)
        if handler is not None:
            handler(name)

    def set_selected_silent(self, name: str | None) -> None:
        """同步 ``_selected`` + 重渲染**不触发** ``_on_node_selected`` 回调。

        phase-16 修复（auto-follow sync bug）：app ``_dispatch_to_widgets`` 在
        ``_auto_follow=True`` 时设 ``_selected_node`` + 驱动 AgentHistory，但若不同步
        AgentsList 的可见 ``▸`` 光标，会出现「app 选 report_painter / 列表还显 analyzer」
        的不一致——用户按 j/k 从 STALE 的 analyzer 位置出发，跳到错误 agent。

        与 ``select()`` 的区别：本方法**不**回调 app（避免 ``_on_node_selected`` 把
        ``_auto_follow`` 改回 False，造成 auto-follow 自我取消）。仅 widget 内部一致。
        """
        if name is None or name not in self._projections:
            return
        self._selected = name
        self._rerender()

    @property
    def selected(self) -> str | None:
        return self._selected

    # ── Textual actions（j/k 绑定）─────────────────────────────────────

    def action_select_next(self) -> None:
        if not self._node_names:
            return
        cur = self._selected
        if cur is None or cur not in self._node_names:
            self.select(self._node_names[0])
            return
        idx = self._node_names.index(cur)
        self.select(self._node_names[(idx + 1) % len(self._node_names)])

    def action_select_prev(self) -> None:
        if not self._node_names:
            return
        cur = self._selected
        if cur is None or cur not in self._node_names:
            self.select(self._node_names[-1])
            return
        idx = self._node_names.index(cur)
        self.select(self._node_names[(idx - 1) % len(self._node_names)])

    # ── 渲染 ──────────────────────────────────────────────────────────

    def _rerender(self) -> None:
        """重渲染：每 agent 一行 + 选中标记 + 投影字段。

        单行格式（spec §2.2）：``{sel} {name:<14} {icon} {elapsed:>4}`` + 可选 ``· {tok}`` + 可选 ``· iter {N}``。
        失败时第二行：``    ! {err[:30]}``（spec §6.3 错误显示）。
        """
        if not self._node_names:
            self.update("(no agents)")
            return
        lines: list[str] = []
        for name in self._node_names:
            proj = self._projections[name]
            sel = "▸" if name == self._selected else " "
            icon = NODE_STATUS_ICONS.get(proj.status, "○")
            elapsed_str = _format_elapsed(proj.elapsed) if proj.elapsed is not None else ""

            # 基础行：sel + name + icon + elapsed
            line = f"{sel} {name:<14} {icon} {elapsed_str:>4}"
            # 可选段：tokens + iter_n（按顺序追加，避免重复拼接）
            extras: list[str] = []
            if proj.tokens is not None:
                extras.append(_format_tokens(proj.tokens))
            if proj.iter_n is not None and proj.iter_n > 1:
                extras.append(f"iter {proj.iter_n}")
            if extras:
                line = f"{line}  · {'  · '.join(extras)}"

            # 失败时第二行显错误摘要（spec §2.2 + §6.3 错误显示）。
            # P4 / ADR §8.1：不字面量比较 status——``error_msg`` 仅 ``failed`` 节点由
            # app.dispatch 注入（``update_node(error_msg=...)``），truthiness 与
            # ``status == "failed"`` 等价且更鲁棒（未来加新失败态不需改 widget）。
            if proj.error_msg:
                err = proj.error_msg[:30]
                line = f"{line}\n    ! {err}"

            lines.append(line)
        # 底部加 keybinding hint（小，不抢焦点）
        lines.append("")
        lines.append("[j/k 切换 · a 跟随]")
        self.update("\n".join(lines))


def _format_elapsed(elapsed: float) -> str:
    """秒格式化：``< 60s`` 显 ``{n}s``；``>= 60s`` 显 ``{m}m{s}s``（spec §2.2）。"""
    if elapsed < 0:
        return "0s"
    if elapsed < 60:
        return f"{elapsed:.0f}s"
    minutes = int(elapsed // 60)
    secs = elapsed - minutes * 60
    return f"{minutes}m{secs:.0f}s"


def _format_tokens(tokens: int) -> str:
    """token 数格式化：``< 1000`` 显原数；``>= 1000`` 显 ``{k}k``（spec §2.2）。"""
    if tokens < 0:
        return "0"
    if tokens < 1000:
        return str(tokens)
    return f"{tokens / 1000:.1f}k"
