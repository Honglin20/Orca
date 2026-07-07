// components/chart/ChartGroup.tsx —— 按 label 分组（可折叠）+ 响应式 grid + 懒挂（SPEC §5.4）。
//
// **去重真相出口在 selectCharts**（铁律 1：selectors 是唯一 view 输入）。selectCharts 已按
// SPEC §5.4 identity（``title || chart_type+seq``）upsert 去重；ChartGroup 不再二次去重——
// 否则空 title 的多 chart 会被压成最后一个（违反 identity 契约）。
//
// 折叠：UI 交互态（local useState，非业务真相）—— 点击折叠/展开该组 charts。
//
// 布局：响应式 grid ``repeat(auto-fit, minmax(300px, 1fr))``（SPEC §5.4）—— 容器宽度
// 自适应；每列最小 300px，超出自动 wrap。chart widget 内部 ``aspect-[4/3]`` 限高。
//
// 懒挂：每 chart 包 ``LazyChartWidget``（IntersectionObserver + 300px skeleton）。

import { useState } from "react";
import type { ChartPayload } from "./types";
import { LazyChartWidget } from "./LazyChartWidget";

const GRID_STYLE: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))",
  gap: 12,
};

export function ChartGroup({
  label,
  charts,
}: {
  label: string;
  charts: ChartPayload[];
}) {
  // collapsed 仅 UI 交互态（非业务真相，铁律 2）——与 gate 状态不同，折叠是纯展示层
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div
      className="rounded border border-slate-200 bg-white"
      data-testid="chart-group"
      data-label={label}
    >
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        className="flex w-full items-center justify-between px-3 py-2 text-left text-sm font-medium text-slate-700 hover:bg-slate-50"
        data-testid="chart-group-toggle"
      >
        <span>
          {collapsed ? "▶" : "▼"} {label}
        </span>
        <span className="text-xs text-slate-400">{charts.length} 图</span>
      </button>
      {!collapsed && (
        <div className="border-t border-slate-100 p-3" style={GRID_STYLE}>
          {charts.map((c, i) => (
            // 用 title 优先作 key（selectCharts identity 的稳定部分）；无 title 回退 index
            <LazyChartWidget
              key={c.title || `chart-${i}`}
              payload={c}
            />
          ))}
        </div>
      )}
    </div>
  );
}
