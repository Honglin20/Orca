// components/chart/ChartRenderer.tsx —— 主入口：用 selectCharts 选择 custom(chart) 事件
// → 按 group 分组渲染（SPEC §5.4 / §0 D3 / D7）。
//
// 铁律 4（SPEC §0.1）：chart 是事件不是图片——从 store.events filter type==="custom" &&
// data.kind==="chart"（D7 seq 升序 fold，序无关）。**不单独存 chart store/通道**。
//
// **去重真相出口 = selectCharts**（identity=title||chart_type+seq，upsert）。ChartGroup
// 不再二次去重（铁律 1：selectors 是唯一 view 输入）。

import { useMemo } from "react";
import { useWorkflowStore } from "@/stores/workflow-store";
import { selectCharts, type ChartEntry } from "@/selectors";
import { ChartGroup } from "./ChartGroup";
import type { ChartPayload } from "./types";

interface ChartRendererProps {
  /** 限定到某节点；undefined = 全部节点（ChartsView 用）。 */
  nodeId?: string;
}

/** 把 ChartEntry.payload 当作 ChartPayload（运行时 shape 由后端契约保证）。 */
function asChartPayload(entry: ChartEntry): ChartPayload | null {
  const p = entry.payload as Record<string, unknown>;
  if (!p || !p.chart_type || !Array.isArray(p.data)) return null;
  return p as unknown as ChartPayload;
}

export function ChartRenderer({ nodeId }: ChartRendererProps) {
  const state = useWorkflowStore();
  const { groups } = useMemo(() => selectCharts(state), [state]);

  // nodeId filter（可选）：限定到某节点
  const filtered = useMemo(() => {
    if (nodeId === undefined) return groups;
    return groups
      .map((g) => ({
        ...g,
        entries: g.entries.filter((e) => e.node === nodeId),
      }))
      .filter((g) => g.entries.length > 0);
  }, [groups, nodeId]);

  if (filtered.length === 0) {
    return (
      <p className="p-4 text-xs orca-text-faint" data-testid="chart-empty">
        暂无图表
      </p>
    );
  }

  return (
    <div className="space-y-4 p-3" data-testid="chart-renderer">
      {filtered.map(({ group, entries }) => {
        const charts = entries
          .map(asChartPayload)
          .filter((c): c is ChartPayload => c !== null);
        return <ChartGroup key={group} label={group} charts={charts} />;
      })}
    </div>
  );
}
