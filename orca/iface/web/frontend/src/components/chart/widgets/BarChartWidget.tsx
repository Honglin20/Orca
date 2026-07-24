// components/chart/widgets/BarChartWidget.tsx —— recharts BarChart（迁移自 AgentHarness，半透明填充 + 圆角 + chartTheme）。

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
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
  getXAxisLabelProp,
  getYAxisLabelProp,
} from "../chartTheme";
import { computeNiceTicks, extractNumericValues, formatTick } from "../axisUtils";
import { pivotByHue } from "../pivot";
import { ChartCaption } from "../ChartCaption";

export function BarChartWidget({ payload }: { payload: ChartPayload }) {
  const { data, x, y, hue, color, title, caption } = payload;
  const xKey = x ?? "x";
  const yKey = y ?? "y";
  const gridProps = getGridProps();
  const axisTick = getAxisTick();
  const tooltipStyle = getTooltipStyle();
  // P5a：Bar/Pareto → cursor={fill:rgba(0,0,0,0.04)} 极淡灰高亮（原硬编码迁移到 getCursor）。
  const tooltipCursor = getCursor(false);
  const tooltipTextStyle = getTooltipTextStyle();
  // 轴标签：x_label/y_label 优先，空回退字段名。
  const xAxisLabel = getXAxisLabelProp(payload);
  const yAxisLabel = getYAxisLabelProp(payload);

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
        {caption && <ChartCaption text={caption} />}
      </div>
    );
  }

  // color 分支：per-row 着色（单 series 内逐行上色，调用方每行写合法 CSS 色串）。
  // 与 hue 互斥：hue 优先（上面 hue 分支已 return），color 仅在 hue 缺席时生效。
  // 不渲染 Legend（per-row 着色无意义系列图例；含义靠 title 文案表达，如 "(coral=selected)"）。
  if (color) {
    const allYValues = extractNumericValues(data, yKey);
    const yConfig = computeNiceTicks(allYValues);
    return (
      <div data-testid="chart-widget">
        <h4 className="orca-text-muted mb-2 text-xs font-medium">{title}</h4>
        <div className="aspect-[4/3] w-full">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={CHART_MARGIN}>
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
              <Bar
                dataKey={yKey}
                fillOpacity={BOX_FILL_OPACITY}
                strokeWidth={BOX_STROKE_WIDTH}
                radius={[BOX_RADIUS, BOX_RADIUS, 0, 0]}
              >
                {data.map((row, i) => {
                  // 每行的 color 字段值是合法 CSS 色串（如 "#D4605A"）；缺席回退 PALETTE[0]
                  // 防 NaN/undefined 渲染异常（fail-soft 但可见：默认色一眼能看出未着色行）。
                  const c = String(row[color] ?? PALETTE[0]);
                  return <Cell key={i} fill={c} stroke={c} />;
                })}
              </Bar>
            </BarChart>
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
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={CHART_MARGIN}>
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
      {caption && <ChartCaption text={caption} />}
    </div>
  );
}
