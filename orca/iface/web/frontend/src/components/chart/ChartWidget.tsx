// components/chart/ChartWidget.tsx —— 按 chart_type 分派（SPEC §2.4）。
//
// 7 种 chart_type：line/bar/area/scatter/pareto/radar/table。未知类型 fail loud（显示提示，不静默崩）。

import type { ChartPayload } from "./types";
import { LineChartWidget } from "./widgets/LineChartWidget";
import { BarChartWidget } from "./widgets/BarChartWidget";
import { AreaChartWidget } from "./widgets/AreaChartWidget";
import { ScatterChartWidget } from "./widgets/ScatterChartWidget";
import { ParetoChartWidget } from "./widgets/ParetoChartWidget";
import { RadarChartWidget } from "./widgets/RadarChartWidget";
import { DataTableWidget } from "./widgets/DataTableWidget";

export function ChartWidget({ payload }: { payload: ChartPayload }) {
  switch (payload.chart_type) {
    case "line":
      return <LineChartWidget payload={payload} />;
    case "bar":
      return <BarChartWidget payload={payload} />;
    case "area":
      return <AreaChartWidget payload={payload} />;
    case "scatter":
      return <ScatterChartWidget payload={payload} />;
    case "pareto":
      return <ParetoChartWidget payload={payload} />;
    case "radar":
      return <RadarChartWidget payload={payload} />;
    case "table":
      return <DataTableWidget payload={payload} />;
    default:
      // fail loud（未知类型不静默，显示提示让用户/开发者发现）
      return (
        <div className="p-2 text-xs text-orca-failed" data-testid="chart-unknown">
          未知的 chart_type: {String((payload as { chart_type?: string }).chart_type)}
        </div>
      );
  }
}
