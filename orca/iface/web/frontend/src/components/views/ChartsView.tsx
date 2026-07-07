// components/views/ChartsView.tsx —— 中栏「图表」页签（SPEC §5.4）。
//
// Chunk A：复用 ChartRenderer（已用 selectCharts selector），无 nodeId 过滤（全量）。
// 完整 ChartsView（IntersectionObserver 懒挂 / ChartGroup collapsible / 7 widget 精化）
// 留给后续 chunk。

import { ChartRenderer } from "@/components/chart/ChartRenderer";

export function ChartsView() {
  return (
    <div data-testid="charts-view" className="h-full overflow-auto">
      <ChartRenderer />
    </div>
  );
}
