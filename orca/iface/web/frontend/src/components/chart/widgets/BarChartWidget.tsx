// components/chart/widgets/BarChartWidget.tsx —— recharts BarChart（迁移自 AgentHarness，半透明填充 + 圆角 + chartTheme）。

import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ChartPayload } from "../types";
import {
  BOX_FILL_OPACITY,
  BOX_RADIUS,
  BOX_STROKE_WIDTH,
  CHART_MARGIN,
  LEGEND_STYLE,
  PALETTE,
  getAxisTick,
  getCursor,
  getGridProps,
  getTooltipStyle,
  getTooltipTextStyle,
} from "../chartTheme";
import { computeNiceTicks, extractNumericValues, formatTick } from "../axisUtils";
import { pivotByHue } from "../pivot";

export function BarChartWidget({ payload }: { payload: ChartPayload }) {
  const { data, x, y, hue, title } = payload;
  const xKey = x ?? "x";
  const yKey = y ?? "y";
  const gridProps = getGridProps();
  const axisTick = getAxisTick();
  const tooltipStyle = getTooltipStyle();
  // P5a：Bar/Pareto → cursor={fill:rgba(0,0,0,0.04)} 极淡灰高亮（原硬编码迁移到 getCursor）。
  const tooltipCursor = getCursor(false);
  const tooltipTextStyle = getTooltipTextStyle();

  if (hue) {
    const { pivoted: pivotedData, hueValues } = pivotByHue(data, xKey, hue, yKey);
    const pivotedYValues = hueValues.flatMap((hv) =>
      extractNumericValues(pivotedData, hv),
    );
    const yConfig = computeNiceTicks(pivotedYValues);

    return (
      <div data-testid="chart-widget">
        <h4 className="orca-text-muted mb-2 text-xs font-medium">{title}</h4>
        <div className="aspect-[4/3] w-full">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={pivotedData} margin={CHART_MARGIN}>
              <CartesianGrid {...gridProps} />
              <XAxis dataKey={xKey} tick={axisTick} />
              <YAxis
                tick={axisTick}
                domain={yConfig.domain}
                ticks={yConfig.ticks}
                tickFormatter={formatTick}
              />
              <Tooltip
                contentStyle={tooltipStyle}
                cursor={tooltipCursor}
                labelStyle={tooltipTextStyle}
                itemStyle={tooltipTextStyle}
              />
              <Legend wrapperStyle={LEGEND_STYLE} />
              {hueValues.map((val, i) => (
                <Bar
                  key={val}
                  dataKey={val}
                  fill={PALETTE[i % PALETTE.length]}
                  fillOpacity={BOX_FILL_OPACITY}
                  stroke={PALETTE[i % PALETTE.length]}
                  strokeWidth={BOX_STROKE_WIDTH}
                  radius={[BOX_RADIUS, BOX_RADIUS, 0, 0]}
                />
              ))}
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    );
  }

  const allYValues = extractNumericValues(data, yKey);
  const yConfig = computeNiceTicks(allYValues);

  return (
    <div data-testid="chart-widget">
      <h4 className="orca-text-muted mb-2 text-xs font-medium">{title}</h4>
        <div className="aspect-[4/3] w-full">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={CHART_MARGIN}>
              <CartesianGrid {...gridProps} />
              <XAxis dataKey={xKey} tick={axisTick} />
              <YAxis
                tick={axisTick}
                domain={yConfig.domain}
                ticks={yConfig.ticks}
                tickFormatter={formatTick}
              />
              <Tooltip
                contentStyle={tooltipStyle}
                cursor={tooltipCursor}
                labelStyle={tooltipTextStyle}
                itemStyle={tooltipTextStyle}
              />
              <Bar
                dataKey={yKey}
                fill={PALETTE[0]}
                fillOpacity={BOX_FILL_OPACITY}
                stroke={PALETTE[0]}
                strokeWidth={BOX_STROKE_WIDTH}
                radius={[BOX_RADIUS, BOX_RADIUS, 0, 0]}
              />
            </BarChart>
          </ResponsiveContainer>
      </div>
    </div>
  );
}
