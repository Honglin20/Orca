"""_dag_render.py —— DAG 3 行盒子渲染 helper（tui-redesign-draft v1.1 §4.4 §4.5 §4.6）。

回答「单节点盒子怎么画？fan-in 怎么标？after=None 怎么摆？」：本模块只负责渲染，
**不动** ``LayeredDagLayout`` / ``CompactOutlineLayout`` 的分层算法（spec §13 决议）。

设计原则：
  - **纯函数 / 数据类**：``box_render(proj)`` 返 3 行文本（``list[str]``，长度恰 3）；可独立单测。
  - **字段级对齐 spec §4.4**：name 居中 / status+iter 第 2 行 / elapsed+tok 或 error 第 3 行。
  - **fan-in 标注 §4.5**：``fan_in_annotation(total, arrived)`` 返 ``"(N inputs · M/N arrived)"``
    文字（N≥2 才显示；arrived==total 时只显 ``"(N inputs)"``）。
  - **after=None section §4.6**：``render_after_none_section(...)`` 渲染旁支盒子列表。
  - **fallback ≥ 5 §4.3**：``should_fallback_to_outline(layer_widths, cols_budget)`` 决定切 CompactOutline。
  - **依赖单向**：只 import stdlib；无 textual/rich/orca.* 依赖。
"""

from __future__ import annotations

from dataclasses import dataclass

# spec §4.3 fallback 阈值：同层并行 ≥ 5 切 outline（既有 CompactOutlineLayout 复用）。
FALLBACK_PARALLEL_THRESHOLD = 5

# spec §4.4 单盒子内部宽度（边框 ── 之间）。最小 12 字符（8 name + 2 padding + 2 border）。
_MIN_BOX_INNER_WIDTH = 12
_MAX_NAME_CHARS = 14  # 名字超此长度截断 + …

# spec §4.4 错误摘要长度（第 3 行替代 elapsed+tok）。
_ERROR_PREVIEW_LEN = 30


@dataclass
class NodeProjection:
    """单节点的渲染投影（spec §4.4 字段级定义）。

    由 app 维护的 reducer 派生 fold（重放必重建，spec §4.4.1）：
      - ``status``：pending/running/done/failed/blocked
      - ``iter_n``：当前节点 iter 号（1-based；同 session_id 重试不增量）
      - ``elapsed``：完成时静态；运行时 live timer（由 app 注入 wall clock，**不进 tape**）
      - ``tokens``：同 session_id 最后一条 agent_usage 的 in+out
      - ``error_msg``：node_failed/error 的 message 前 30 字符（替代第 3 行）
      - ``fan_in_total``：拓扑入边数（静态，spec §4.5 O2=a）
      - ``fan_in_arrived``：已完成的前置节点数（动态，spec §4.5 M）
    """

    name: str
    status: str = "pending"
    iter_n: int = 1
    elapsed: float | None = None
    tokens: int | None = None
    error_msg: str | None = None
    fan_in_total: int = 0
    fan_in_arrived: int = 0


def truncate_name(name: str, max_chars: int = _MAX_NAME_CHARS) -> str:
    """节点名超长截断 + ``…``（spec §4.4 acceptance）。"""
    if len(name) <= max_chars:
        return name
    return name[: max_chars - 1] + "…"


def _status_icon(status: str) -> str:
    """状态 → 图标（复用 ``_icons``，DRY）。"""
    from orca.iface.cli.widgets._icons import NODE_STATUS_ICONS

    return NODE_STATUS_ICONS.get(status, NODE_STATUS_ICONS["pending"])


def format_tokens(tokens: int | None) -> str:
    """token 数格式化（千位带 k 后缀；None → ``--``）。"""
    if tokens is None:
        return "--"
    if tokens >= 1000:
        return f"{tokens / 1000:.1f}k"
    return str(tokens)


def format_elapsed(elapsed: float | None) -> str:
    """耗时格式化（< 60s 显秒；>= 60s 显 m+s；None → ``--``）。"""
    if elapsed is None:
        return "--"
    if elapsed < 60:
        return f"{elapsed:.0f}s"
    minutes = int(elapsed // 60)
    secs = elapsed - minutes * 60
    return f"{minutes}m{secs:.0f}s"


def box_render(proj: NodeProjection, *, width: int = _MIN_BOX_INNER_WIDTH) -> list[str]:
    """渲染单节点 3 行盒子（spec §4.4 字段级定义）。

    返长度恰 3 的字符串列表（顶层边框 + 行 1 + 行 2 + 行 3 + 底层边框 = 5 行；
    本函数返**内容** 3 行，边框由调用者拼）。

    实际返 5 行（含上下边框），方便调用者直接 join。

    失败节点第 3 行显 ``! <error_msg[:30]>`` 替代 elapsed+tok（spec §4.4 acceptance）。

    Args:
      proj: 节点投影。
      width: 盒子内部宽度（默认 12；调用者可调宽以对齐同层）。
    """
    name = truncate_name(proj.name).center(width)
    icon = _status_icon(proj.status)
    line2 = f"{icon} {proj.status} · iter {proj.iter_n}".center(width)
    if proj.status == "failed" and proj.error_msg:
        # 失败：第 3 行显错误摘要（spec §4.4 acceptance / §6.3）
        preview = proj.error_msg[:_ERROR_PREVIEW_LEN]
        line3 = f"! {preview}".center(width)
    else:
        elapsed = format_elapsed(proj.elapsed)
        tokens = format_tokens(proj.tokens)
        line3 = f"{elapsed} · {tokens} tok".center(width)
    top = "┌" + "─" * width + "┐"
    bot = "└" + "─" * width + "┘"
    return [top, f"│{name}│", f"│{line2}│", f"│{line3}│", bot]


def fan_in_annotation(total: int, arrived: int) -> str | None:
    """fan-in 副标文字（spec §4.5 O2=a）。

    - N < 2（线性）→ None（不显示）
    - N >= 2 且 arrived < total → ``"(N inputs · M/N arrived)"``
    - N >= 2 且 arrived == total → ``"(N inputs)"``（全部到齐，副标消失，spec §4.5 acceptance）
    """
    if total < 2:
        return None
    if arrived >= total:
        return f"({total} inputs)"
    return f"({total} inputs · {arrived}/{total} arrived)"


def should_fallback_to_outline(
    layer_node_counts: list[int], cols_budget: int,
) -> bool:
    """spec §4.3：同层并行 ≥ 5 切 outline fallback。

    ``layer_node_counts``：每层节点数列表（如 ``[1, 1, 4, 1]``）。
    返 True 表示该切 ``CompactOutlineLayout``（既有 fallback）。

    阈值来源：3 行盒子最小宽 12 字符 + 间隔 2 → 4 并行 = 51 字符（fits 60）；
    5 并行 = 64 字符（超 60 临界）；6 并行 = 77 字符（超 80）。
    """
    if any(count >= FALLBACK_PARALLEL_THRESHOLD for count in layer_node_counts):
        return True
    # 极窄屏（< 30 列）也 fallback（盒子挤不下）
    if cols_budget < _MIN_BOX_INNER_WIDTH + 2:
        return True
    return False


def render_main_branch_nodes(
    projections: dict[str, NodeProjection],
    main_layers: list[list[str]],
) -> list[str]:
    """渲染主流分层节点（3 行盒子纵向堆 + 层间 ``│`` 箭头）。

    Args:
      projections: 节点名 → NodeProjection。
      main_layers: 主流分层（已剔除 after=None 旁支），每层一组节点名。

    返：可 ``join("\\n")`` 的字符串列表（每行 1 个 str）。
    """
    lines: list[str] = []
    for i, layer_nodes in enumerate(main_layers):
        # 同层横向并排（width 对齐）
        max_name = max((len(projections[n].name) for n in layer_nodes), default=8)
        width = max(_MIN_BOX_INNER_WIDTH, min(max_name + 2, 22))
        boxes_per_node = [box_render(projections[n], width=width) for n in layer_nodes]
        # 横向 join（5 行拼接，每行用 2 空格间隔）
        for row_idx in range(5):
            parts = [box[row_idx] for box in boxes_per_node]
            lines.append("  ".join(parts))
        # fan-in 副标（如有）
        for n in layer_nodes:
            proj = projections[n]
            ann = fan_in_annotation(proj.fan_in_total, proj.fan_in_arrived)
            if ann:
                pad = " " * (width // 2)
                lines.append(f"{pad}{n} {ann}")
        # 层间连边（非末层）
        if i < len(main_layers) - 1:
            lines.append(" " * (width // 2) + "│")
            lines.append(" " * (width // 2) + "▼")
    return lines


def render_after_none_section(
    projections: dict[str, NodeProjection],
    after_none_nodes: list[str],
    merge_target: str | None,
) -> list[str]:
    """渲染 after=None 旁支 section（spec §4.6 O3=b）。

    Args:
      projections: 全部节点投影（含旁支节点）。
      after_none_nodes: after=None 的节点名列表（按拓扑序）。
      merge_target: 主流末端汇聚节点名（如 reporter），用于文字标注。

    返：渲染行（含 section 标题 ``─── 旁支（after=None） ───``）。
    """
    if not after_none_nodes:
        return []
    lines: list[str] = ["", "─── 旁支（after=None） ───", ""]
    for n in after_none_nodes:
        proj = projections[n]
        max_name = max(len(proj.name), 8)
        width = max(_MIN_BOX_INNER_WIDTH, min(max_name + 2, 22))
        for line in box_render(proj, width=width):
            lines.append(line)
        if merge_target:
            lines.append(f"    └────▶ {merge_target} (末端汇聚)")
        lines.append("")
    return lines


def split_main_and_after_none(
    topo_nodes: list[str],
    edges: list[tuple[str, str]],
) -> tuple[list[str], list[str], str | None]:
    """把拓扑节点分主流 / 旁支（after=None，spec §4.6）。

    after=None 定义（spec §4.6）：节点没有上游入边（``indeg == 0``）但**不是** entry 节点。
    这是「孤立起点」——独立分支，没有显式 ``after``。

    Args:
      topo_nodes: 拓扑全部节点名。
      edges: 边列表 (src, dst)。

    返：
      - ``main_nodes``：主流节点（按拓扑序，含 entry）。
      - ``after_none_nodes``：旁支节点（拓扑序）。
      - ``merge_target``：旁支末端汇聚到主流的目标节点（None = 无汇聚）。
    """
    if not topo_nodes:
        return [], [], None
    indeg: dict[str, int] = {n: 0 for n in topo_nodes}
    for src, dst in edges:
        if dst in indeg:
            indeg[dst] += 1
    # entry = topo_nodes[0]（与 build_from_workflow 一致）；其余 indeg==0 是旁支。
    # 但 entry 节点本身 indeg 也是 0，需排除。
    entry = topo_nodes[0]
    after_none = [n for n in topo_nodes if n != entry and indeg[n] == 0]
    main = [n for n in topo_nodes if n not in after_none]
    # 旁支末端汇聚：旁支节点 routes 指向主流节点的目标（如有）
    after_none_set = set(after_none)
    main_set = set(main)
    merge_target: str | None = None
    for src, dst in edges:
        if src in after_none_set and dst in main_set:
            merge_target = dst
            break
    return main, after_none, merge_target


__all__ = [
    "NodeProjection",
    "FALLBACK_PARALLEL_THRESHOLD",
    "_MIN_BOX_INNER_WIDTH",
    "_MAX_NAME_CHARS",
    "_ERROR_PREVIEW_LEN",
    "truncate_name",
    "format_tokens",
    "format_elapsed",
    "box_render",
    "fan_in_annotation",
    "should_fallback_to_outline",
    "render_main_branch_nodes",
    "render_after_none_section",
    "split_main_and_after_none",
]
