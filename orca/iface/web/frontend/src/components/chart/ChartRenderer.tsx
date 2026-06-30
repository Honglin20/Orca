// components/chart/ChartRenderer.tsx —— 主入口：订阅 store.events custom(chart) → 按 node filter → label 分组（SPEC §2.4）。
//
// 铁律 4（SPEC §0.1）：**chart 是事件不是图片** —— 从 store.events filter type==="custom" &&
// data.kind==="chart"，**不单独存 chart store/通道**（反 AgentHarness 三通道）。
//
// replay 同步（SPEC §2.7）：replay 模式只显示 events[0..replayPosition] —— 复用与 NodeDetail/
// LogStream 同一 events 切片逻辑（同一 store，自动同步）。
//
// nodeId 可选：undefined → 显示所有节点的 chart（Output Panel 用）。

import { useMemo } from "react";
import { useWorkflowStore } from "@/stores/workflow-store";
import type { ChartPayload } from "./types";
import { ChartGroup } from "./ChartGroup";

interface ChartRendererProps {
  /** 限定到某节点；undefined = 全部节点（Output Panel 用）。 */
  nodeId?: string;
}

/** 从 custom 事件提取 ChartPayload（data.kind==="chart"）。 */
function extractChart(e: { data: Record<string, unknown> }): ChartPayload | null {
  const data = e.data;
  if (!data || data.kind !== "chart") return null;
  const chart = data.chart as ChartPayload | undefined;
  if (!chart || !chart.chart_type || !Array.isArray(chart.data)) return null;
  return chart;
}

export function ChartRenderer({ nodeId }: ChartRendererProps) {
  const events = useWorkflowStore((s) => s.events);
  const replayMode = useWorkflowStore((s) => s.replayMode);
  const replayPosition = useWorkflowStore((s) => s.replayPosition);

  // replay-aware events 切片（与 NodeDetail/LogStream 同一逻辑，SPEC §2.7 replay 同步）
  const charts = useMemo(() => {
    const end = replayMode ? replayPosition + 1 : events.length;
    const out: ChartPayload[] = [];
    for (const e of events.slice(0, end)) {
      if (e.type !== "custom") continue;
      if (nodeId !== undefined && e.node !== nodeId) continue;
      const payload = extractChart(e);
      if (payload) out.push(payload);
    }
    return out;
  }, [events, nodeId, replayMode, replayPosition]);

  // 按 label 分组（保持插入顺序）
  const groups = useMemo(() => {
    const map = new Map<string, ChartPayload[]>();
    for (const c of charts) {
      const arr = map.get(c.label);
      if (arr) arr.push(c);
      else map.set(c.label, [c]);
    }
    return Array.from(map.entries());
  }, [charts]);

  if (groups.length === 0) {
    return (
      <p className="p-4 text-xs text-slate-400" data-testid="chart-empty">
        暂无图表
      </p>
    );
  }

  return (
    <div className="space-y-4 p-3" data-testid="chart-renderer">
      {groups.map(([label, groupCharts]) => (
        <ChartGroup key={label} label={label} charts={groupCharts} />
      ))}
    </div>
  );
}
