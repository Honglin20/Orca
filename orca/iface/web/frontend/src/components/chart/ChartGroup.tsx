// components/chart/ChartGroup.tsx —— 按 label 分组（可折叠）+ 响应式 grid + 懒挂（SPEC §5.4）。
//
// **去重真相出口在 selectCharts**（铁律 1：selectors 是唯一 view 输入）。selectCharts 已按
// SPEC §5.4 identity（``title || chart_type+seq``）upsert 去重；ChartGroup 不再二次去重——
// 否则空 title 的多 chart 会被压成最后一个（违反 identity 契约）。
//
// SPEC audit-c §4.2 E3/BLOCKER-1：签名改为 ``{ identity, payload }[]``，``key={chart.identity}``
// （非 seq——合成 seq 在 loadFull 前不可知；identity 对 titled chart 跨 huge→full 稳定，
// 无 title 允许 remount + partition dev warn）。
//
// 折叠：UI 交互态（local useState，非业务真相）—— 点击折叠/展开该组 charts。
//
// 布局：响应式 grid ``repeat(auto-fit, minmax(300px, 1fr))``（SPEC §5.4）—— 容器宽度
// 自适应；每列最小 300px，超出自动 wrap。chart widget 内部 ``aspect-[4/3]`` 限高。
//
// 表格独占整行（用户布局契约）：``chart_type === "table"`` 的 widget 不参与图片流——
// 分区后排在最后（图…图→表…表，类内稳定序），并 ``gridColumn: 1 / -1`` 横跨整行，
// 杜绝「图-表-图」同排。
//
// 懒挂：每 chart 包 ``LazyChartWidget``（IntersectionObserver + 300px skeleton）。

import { useMemo, useState } from "react";
import { ChevronRight, ChevronDown } from "lucide-react";
import type { ChartPayload } from "./types";
import { LazyChartWidget } from "./LazyChartWidget";

const GRID_STYLE: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))",
  gap: 12,
};

/** 表格独占一行：横跨全部 grid 列；minWidth 0 防超宽内容撑破 auto-fit 列。 */
const TABLE_ROW_STYLE: React.CSSProperties = { gridColumn: "1 / -1", minWidth: 0 };

interface ChartGroupItem {
  identity: string;
  payload: ChartPayload;
}

export function ChartGroup({
  label,
  charts,
}: {
  label: string;
  charts: ChartGroupItem[];
}) {
  // collapsed 仅 UI 交互态（非业务真相，铁律 2）——与 gate 状态不同，折叠是纯展示层
  const [collapsed, setCollapsed] = useState(false);

  // 布局分区（纯展示层，不动 selectCharts 序）：非 table 图保持原序在前，table 收尾
  // （类内 filter 保稳定序），配合 TABLE_ROW_STYLE 实现「图-图-表」自适应排布。
  const { visuals, tables } = useMemo(() => {
    const visuals: ChartGroupItem[] = [];
    const tables: ChartGroupItem[] = [];
    for (const c of charts) {
      (c.payload.chart_type === "table" ? tables : visuals).push(c);
    }
    return { visuals, tables };
  }, [charts]);

  return (
    <div
      className="rounded border orca-border orca-bg-surface"
      data-testid="chart-group"
      data-label={label}
    >
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        className="flex w-full items-center justify-between px-3 py-2 text-left text-sm font-medium orca-text-muted hover:orca-bg-surface-2"
        data-testid="chart-group-toggle"
      >
        <span className="inline-flex items-center gap-1">
          {collapsed ? <ChevronRight size={14} strokeWidth={1.5} aria-hidden /> : <ChevronDown size={14} strokeWidth={1.5} aria-hidden />} {label}
        </span>
        <span className="text-xs orca-text-faint">{charts.length} 项</span>
      </button>
      {!collapsed && (
        <div className="border-t orca-border p-3" style={GRID_STYLE}>
          {visuals.map((c) => (
            // SPEC audit-c E3/BLOCKER-1：key=identity（titled 跨 huge→full 稳定；无 title 允许 remount）
            <LazyChartWidget
              key={c.identity}
              payload={c.payload}
            />
          ))}
          {tables.map((c) => (
            <div key={c.identity} style={TABLE_ROW_STYLE}>
              <LazyChartWidget payload={c.payload} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
