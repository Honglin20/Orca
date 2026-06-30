// components/chart/widgets/LineChartWidget.tsx —— recharts LineChart（迁移自 AgentHarness，chartTheme 学术配色）。
//
// 迁移自 AgentHarness LineChartWidget.tsx —— 主要改动：
//   - prop 名 chart → payload（对齐 SPEC §2.2 + Orca 命名）
//   - 去掉 EndLabel（简化，SPEC §3 只要求 recharts-line 渲染 + PALETTE 着色）
//   - hue 分组保留（多系列 pivot，反 AgentHarness 多源复杂度）

import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ChartPayload } from "../types";
import {
  CHART_MARGIN,
  LEGEND_STYLE,
  PALETTE,
  getAxisTick,
  getGridProps,
  getTooltipStyle,
} from "../chartTheme";
import { computeNiceTicks, extractNumericValues, formatTick } from "../axisUtils";
import { pivotByHue } from "../pivot";

export function LineChartWidget({ payload }: { payload: ChartPayload }) {
  const { data, x, y, hue, title } = payload;
  const xKey = x ?? "x";
  const yKey = y ?? "y";
  const gridProps = getGridProps();
  const axisTick = getAxisTick();
  const tooltipStyle = getTooltipStyle();

  if (hue) {
    // hue 多系列：长格式 → 宽格式 pivot（共享 helper，DRY）
    const { pivoted: pivotedData, hueValues } = pivotByHue(data, xKey, hue, yKey);
    const pivotedYValues = hueValues.flatMap((hv) =>
      extractNumericValues(pivotedData, hv),
    );
    const yConfig = computeNiceTicks(pivotedYValues);

    return (
      <div data-testid="chart-widget">
        <h4 className="mb-2 text-xs font-medium text-slate-700">{title}</h4>
        <div className="aspect-[4/3] w-full">
          <ResponsiveContainer width="100%" height="100%" minHeight={200} minWidth={300}>
            <LineChart data={pivotedData} margin={{ ...CHART_MARGIN, right: 60 }}>
              <CartesianGrid {...gridProps} />
              <XAxis dataKey={xKey} tick={axisTick} />
              <YAxis
                tick={axisTick}
                domain={yConfig.domain}
                ticks={yConfig.ticks}
                tickFormatter={formatTick}
              />
              <Tooltip contentStyle={tooltipStyle} />
              <Legend wrapperStyle={LEGEND_STYLE} />
              {hueValues.map((val, i) => {
                const color = PALETTE[i % PALETTE.length];
                return (
                  <Line
                    key={val}
                    dataKey={val}
                    stroke={color}
                    strokeWidth={2}
                    dot={{ r: 3, fill: color, strokeWidth: 0 }}
                    activeDot={{ r: 5, strokeWidth: 2, stroke: "#fff", fill: color }}
                  />
                );
              })}
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>
    );
  }

  const allYValues = extractNumericValues(data, yKey);
  const yConfig = computeNiceTicks(allYValues);

  return (
    <div data-testid="chart-widget">
      <h4 className="mb-2 text-xs font-medium text-slate-700">{title}</h4>
      <div className="aspect-[4/3] w-full">
        <ResponsiveContainer width="100%" height="100%" minHeight={200} minWidth={300}>
          <LineChart data={data} margin={CHART_MARGIN}>
            <CartesianGrid {...gridProps} />
            <XAxis dataKey={xKey} tick={axisTick} />
            <YAxis
              tick={axisTick}
              domain={yConfig.domain}
              ticks={yConfig.ticks}
              tickFormatter={formatTick}
            />
            <Tooltip contentStyle={tooltipStyle} />
            <Line
              dataKey={yKey}
              stroke={PALETTE[0]}
              strokeWidth={2}
              dot={{ r: 3, fill: PALETTE[0], strokeWidth: 0 }}
              activeDot={{ r: 5, strokeWidth: 2, stroke: "#fff", fill: PALETTE[0] }}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
