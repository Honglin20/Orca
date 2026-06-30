"""header.py —— 顶部全局指标 widget（SPEC §4.4）。

回答「整个 run 的元信息」：``Orca Run #<id> · <workflow> · <model?> · <done>/<total> nodes · ⏸ <n> awaiting gate``。

设计原则：
  - **壳无真相**：stats 由 app 从事件流计算后 ``update_stats`` 注入；widget 自己不算。
  - **idempotent**：同样 stats 多次 update 渲染一致。
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.widgets import Static


@dataclass
class HeaderStats:
    """Header 显示所需的全局指标（SPEC §4.4）。

    所有字段由 app 从事件流派生（done/total 数 node_completed vs node 总数；
    awaiting 数未 resolved 的 gate）。``model`` 来自 workflow 配置或运行期 agent 事件。
    """

    run_id: str = "?"
    workflow_name: str = "?"
    model: str | None = None
    done: int = 0
    total: int = 0
    awaiting_gate: int = 0

    def render_text(self) -> str:
        """渲染为 header 文本行。"""
        model_part = f" · {self.model}" if self.model else ""
        gate_part = f" · ⏸ {self.awaiting_gate} awaiting gate" if self.awaiting_gate else ""
        return (
            f"Orca Run #{self.run_id} · {self.workflow_name}{model_part} · "
            f"{self.done}/{self.total} nodes{gate_part}"
        )


class Header(Static):
    """顶部全局指标条（SPEC §4.4）。

    用法（由 OrcaApp 驱动）::

        header = app.query_one(Header)
        header.update_stats(HeaderStats(run_id="r1", workflow_name="nas", total=7, done=3))
    """

    DEFAULT_CSS = """
    Header {
        dock: top;
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__("", id="orca-header")
        self._stats: HeaderStats | None = None

    def update_stats(self, stats: HeaderStats) -> None:
        """用新 stats 重渲染 header。"""
        self._stats = stats
        self.update(stats.render_text())

    @property
    def stats(self) -> HeaderStats | None:
        """最近一次 update 的 stats（测试断言用；无则为 None）。"""
        return self._stats
