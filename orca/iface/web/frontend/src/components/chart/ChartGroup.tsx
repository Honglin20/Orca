// components/chart/ChartGroup.tsx —— 按 label 分组（可折叠）+ 同 label+title 替换（实时更新，SPEC §2.4 §2.7）+ 按 chart_type 分派 widget。
//
// 实时时更新（SPEC §2.7）：同 label+title 取最新事件（dedupeByLabelTitle 后只 1 个 chart）。
// 折叠：UI 交互态（local useState，非业务真相）—— 点击折叠/展开该组 charts。

import { useState } from "react";
import type { ChartPayload } from "./types";
import { ChartWidget } from "./ChartWidget";

/** 同 label+title 取最新（实时更新：迭代过程图表刷新不堆积，SPEC §2.7）。 */
export function dedupeByLabelTitle(charts: ChartPayload[]): ChartPayload[] {
  // 用 Map 按 title 覆盖，保留首次出现顺序
  const byTitle = new Map<string, ChartPayload>();
  for (const c of charts) {
    byTitle.set(c.title, c); // 后者覆盖前者
  }
  return Array.from(byTitle.values());
}

export function ChartGroup({
  label,
  charts,
}: {
  label: string;
  charts: ChartPayload[];
}) {
  // collapsed 仅 UI 交互态（非业务真相，铁律 2）——与 gate 状态不同，折叠是纯展示层
  const [collapsed, setCollapsed] = useState(false);
  const latest = dedupeByLabelTitle(charts);

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
        <span className="text-xs text-slate-400">{latest.length} 图</span>
      </button>
      {!collapsed && (
        <div className="space-y-3 border-t border-slate-100 p-3">
          {latest.map((c) => (
            <ChartWidget key={c.title} payload={c} />
          ))}
        </div>
      )}
    </div>
  );
}
