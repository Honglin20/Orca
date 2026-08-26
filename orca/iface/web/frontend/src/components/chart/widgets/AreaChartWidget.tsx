// components/chart/widgets/AreaChartWidget.tsx —— recharts AreaChart（迁移自 AgentHarness，chartTheme 学术配色）。
//
// 迁移自 AgentHarness AreaChartWidget.tsx —— 主要改动：
//   - prop 名 chart → payload（对齐 SPEC §2.2 + Orca 命名）
//   - hue 分组用共享 pivotByHue（DRY，与 Line/Bar 一致）
//   - chartTheme 复用 Orca 版（同 PALETTE/getGridProps/getAxisTick/getTooltipStyle）

import {
  Area,
  AreaChart,
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
  BOX_STROKE_WIDTH,
  CHART_MARGIN,
  LEGEND_STYLE,
  PALETTE,
  getAxisTick,
  getCursor,
  getGridProps,
  getTooltipStyle,
  getTooltipTextStyle,
  getXAxisLabelProp,
  getYAxisLabelProp,
} from "../chartTheme";
import { computeNiceTicks, extractNumericValues, formatTick } from "../axisUtils";
import { pivotByHue } from "../pivot";
import { ChartCaption } from "../ChartCaption";

export function AreaChartWidget({ payload }: { payload: ChartPayload }) {
  const { data, x, y, hue, title, caption } = payload;
  const xKey = x ?? "x";
  const yKey = y ?? "y";
  const gridProps = getGridProps();
  const axisTick = getAxisTick();
  const tooltipStyle = getTooltipStyle();
  const tooltipCursor = getCursor(true);
  const tooltipTextStyle = getTooltipTextStyle();
  // 轴标签：x_label/y_label 优先，空回退字段名。
  const xAxisLabel = getXAxisLabelProp(payload);
  const yAxisLabel = getYAxisLabelProp(payload);

  if (hue) {
    // hue 多系列：长格式 → 宽格式 pivot（共享 helper，DRY）
    const { pivoted: pivotedData, hueValues } = pivotByHue(data, xKey, hue, yKey);
    const pivotedYValues = hueValues.flatMap((hv) =>
      extractNumericValues(pivotedData, hv),
    );
    const yConfig = computeNiceTicks(pivotedYValues);

    return (
      <div data-testid="chart-widget">
        <h4 className="orca-text-muted mb-2 text-xs font-medium">{title}</h4>
        <div className="aspect-[4/3] w-full">
          <ResponsiveContainer width="100%" height="100%" minHeight={200} minWidth={300}>
            <AreaChart data={pivotedData} margin={{ ...CHART_MARGIN, right: 60 }}>
              <CartesianGrid {...gridProps} />
              <XAxis dataKey={xKey} tick={axisTick} label={xAxisLabel} />
              <YAxis
                tick={axisTick}
                domain={yConfig.domain}
                ticks={yConfig.ticks}
                tickFormatter={formatTick}
                label={yAxisLabel}
              />
              <Tooltip
                contentStyle={tooltipStyle}
                cursor={tooltipCursor}
                labelStyle={tooltipTextStyle}
                itemStyle={tooltipTextStyle}
              />
              <Legend wrapperStyle={LEGEND_STYLE} />
              {hueValues.map((val, i) => {
                const color = PALETTE[i % PALETTE.length];
                return (
                  <Area
                    key={val}
                    dataKey={val}
                    stroke={color}
                    fill={color}
                    fillOpacity={BOX_FILL_OPACITY}
                    strokeWidth={BOX_STROKE_WIDTH}
                    type="monotone"
                  />
                );
              })}
            </AreaChart>
          </ResponsiveContainer>
        </div>
        {caption && <ChartCaption text={caption} />}
      </div>
    );
  }

  const allYValues = extractNumericValues(data, yKey);
  const yConfig = computeNiceTicks(allYValues);

  return (
    <div data-testid="chart-widget">
      <h4 className="orca-text-muted mb-2 text-xs font-medium">{title}</h4>
      <div className="aspect-[4/3] w-full">
        <ResponsiveContainer width="100%" height="100%" minHeight={200} minWidth={300}>
          <AreaChart data={data} margin={CHART_MARGIN}>
            <CartesianGrid {...gridProps} />
            <XAxis dataKey={xKey} tick={axisTick} label={xAxisLabel} />
            <YAxis
              tick={axisTick}
              domain={yConfig.domain}
              ticks={yConfig.ticks}
              tickFormatter={formatTick}
              label={yAxisLabel}
            />
            <Tooltip
              contentStyle={tooltipStyle}
              cursor={tooltipCursor}
              labelStyle={tooltipTextStyle}
              itemStyle={tooltipTextStyle}
            />
            <Area
              dataKey={yKey}
              stroke={PALETTE[0]}
              fill={PALETTE[0]}
              fillOpacity={BOX_FILL_OPACITY}
              strokeWidth={BOX_STROKE_WIDTH}
              type="monotone"
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
      {caption && <ChartCaption text={caption} />}
    </div>
  );
}
