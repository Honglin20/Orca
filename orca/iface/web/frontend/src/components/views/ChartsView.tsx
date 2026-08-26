// components/views/ChartsView.tsx —— 中栏「图表」页签（SPEC §5.4）。
//
// 渲染 ``selectCharts(state)`` 的全部 chart（按 group 分组 + identity upsert，序无关 D7）。
// ChartGroup 负责 collapsible + 响应式 grid + IntersectionObserver 懒挂（300px skeleton）。
//
// 单一数据通道（铁律 1 + 5）：从同一 store 读 events → selectCharts 派生 → 渲染。

import { ChartRenderer } from "@/components/chart/ChartRenderer";

export function ChartsView() {
  // P5b：容器底色用 --app-bg（与三栏统一，明暗自适应）。
  return (
    <div data-testid="charts-view" className="orca-bg-app h-full overflow-auto">
      <ChartRenderer />
    </div>
  );
}
