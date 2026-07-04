"""header.py —— 顶部全局指标 widget（SPEC §4.4 + tui-redesign v1.1 §7.2 footer 区）。

回答「整个 run 的元信息」：``Orca Run #<id> · <workflow> · <model?> · <done>/<total> nodes · ⏸ <n> awaiting gate``，
**外加 footer 区**（spec v1.1 §6.2 / §7.2）：
  - per-node token/cost 横向滚动（spec §6.2）
  - 当前 filter 模式标签（spec §5.1 ``[全部事件]`` / ``[仅 analyzer]``）

设计原则：
  - **壳无真相**：stats 由 app 从事件流计算后 ``update_stats`` 注入；widget 自己不算。
  - **idempotent**：同样 stats 多次 update 渲染一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from textual.widgets import Static


@dataclass
class NodeUsageStats:
    """单节点的 token/cost 汇总（spec §6.2）。"""

    name: str
    tokens: int = 0  # input + output
    cost_usd: float = 0.0


@dataclass
class HeaderStats:
    """Header 显示所需的全局指标（SPEC §4.4 + spec v1.1 §7.2）。

    所有字段由 app 从事件流派生（done/total 数 node_completed vs node 总数；
    awaiting 数未 resolved 的 gate）。``model`` 来自 workflow 配置或运行期 agent 事件。

    spec v1.1 §6.2 / §7.2 新增：
      - ``per_node_usage``：每节点 token/cost（agent_usage 收敛到 Header footer）
      - ``filter_node``：当前 Activity Stream filter 节点（None=all / str=仅该节点）
    """

    run_id: str = "?"
    workflow_name: str = "?"
    model: str | None = None
    done: int = 0
    total: int = 0
    awaiting_gate: int = 0
    # spec v1.1 §6.2：per-node usage（横向滚动 + 优先 running）
    per_node_usage: list[NodeUsageStats] = field(default_factory=list)
    # spec v1.1 §5.1：filter 模式（None=all / str=仅该节点）
    filter_node: str | None = None
    # spec v1.1 §7.2：当前 running 节点名（footer 优先显示）
    running_node: str | None = None

    def render_text(self) -> str:
        """渲染 header 顶行（spec §4.4 全局指标）。"""
        model_part = f" · {self.model}" if self.model else ""
        gate_part = f" · ⏸ {self.awaiting_gate} awaiting gate" if self.awaiting_gate else ""
        return (
            f"Orca Run #{self.run_id} · {self.workflow_name}{model_part} · "
            f"{self.done}/{self.total} nodes{gate_part}"
        )

    def render_footer_text(self) -> str:
        """渲染 footer 区（spec v1.1 §6.2 / §7.2 footer 行）。

        格式：``[全部事件] | analyzer 1.2k tok · $0.0004 | configurator 1.8k tok · $0.0006 | ...``

        横向滚动靠 Textual 自动 wrap（窄屏会换行；生产场景建议 width>=100）。
        优先把 running 节点放第一位（spec §11 决议）。
        """
        # filter 模式标签（spec §5.1）
        if self.filter_node is None:
            filter_tag = "[全部事件]"
        else:
            filter_tag = f"[仅 {self.filter_node}]"
        # per-node usage 排序：running 节点优先，其余按声明序（per_node_usage 已是声明序）
        ordered = list(self.per_node_usage)
        if self.running_node and any(u.name == self.running_node for u in ordered):
            running = next(u for u in ordered if u.name == self.running_node)
            ordered.remove(running)
            ordered.insert(0, running)
        # 格式化（千位带 k 后缀；cost 4 位小数）
        parts = [filter_tag]
        for u in ordered:
            tok_str = f"{u.tokens / 1000:.1f}k" if u.tokens >= 1000 else str(u.tokens)
            parts.append(f"{u.name} {tok_str} tok · ${u.cost_usd:.4f}")
        return "  ".join(parts)


class Header(Static):
    """顶部全局指标条（SPEC §4.4 + spec v1.1 §7.2 footer 区）。

    用法（由 OrcaApp 驱动）::

        header = app.query_one(Header)
        header.update_stats(HeaderStats(
            run_id="r1", workflow_name="nas", total=7, done=3,
            per_node_usage=[NodeUsageStats("analyzer", 1200, 0.0004), ...],
            filter_node="analyzer",  # 或 None
        ))
    """

    DEFAULT_CSS = """
    Header {
        dock: top;
        height: 2;  # spec v1.1 §7.2：顶行 + footer 区（per-node usage + filter 标签）
        background: $primary;
        color: $text;
        padding: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__("", id="orca-header")
        self._stats: HeaderStats | None = None

    def update_stats(self, stats: HeaderStats) -> None:
        """用新 stats 重渲染 header（顶行 + footer 区）。"""
        self._stats = stats
        body = stats.render_text() + "\n" + stats.render_footer_text()
        self.update(body)

    @property
    def stats(self) -> HeaderStats | None:
        """最近一次 update 的 stats（测试断言用；无则为 None）。"""
        return self._stats


__all__ = ["Header", "HeaderStats", "NodeUsageStats"]

