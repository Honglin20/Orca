// components/chart/widgets/ScatterChartWidget.tsx —— recharts ScatterChart（迁移自 AgentHarness，chartTheme）。
//
// 当 ``payload.size`` 指定时切到气泡图（ZAxis 按 size 列映射半径，参考 AH BubbleChartWidget）。
// 无 size → 等径散点（ZAxis range=[36,36] 固定）。

import {
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
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
  getXAxisLabelValue,
  getYAxisLabelProp,
  getYAxisLabelValue,
} from "../chartTheme";
import { computeNiceTicks, extractNumericValues, formatTick } from "../axisUtils";
import { ChartCaption } from "../ChartCaption";

export function ScatterChartWidget({ payload }: { payload: ChartPayload }) {
  const { data, x, y, hue, color, size, title, caption } = payload;
  const xKey = x ?? "x";
  const yKey = y ?? "y";
  const sizeKey = size; // undefined → 等径散点
  const gridProps = getGridProps();
  const axisTick = getAxisTick();
  const tooltipStyle = getTooltipStyle();
  // P5a：Scatter cursor 统一为细虚竖线（原 strokeDasharray 单值缺 stroke/strokeWidth）。
  const tooltipCursor = getCursor(true);
  const tooltipTextStyle = getTooltipTextStyle();
  // 轴标签：x_label/y_label 优先，空回退字段名（与 XAxis/YAxis name 同源 → tooltip label 也用之）。
  const xAxisLabel = getXAxisLabelProp(payload);
  const yAxisLabel = getYAxisLabelProp(payload);
  const xAxisName = getXAxisLabelValue(payload);
  const yAxisName = getYAxisLabelValue(payload);

  const allXValues = extractNumericValues(data, xKey);
  const allYValues = extractNumericValues(data, yKey);
  const xConfig = computeNiceTicks(allXValues);
  const yConfig = computeNiceTicks(allYValues);

  // 行 → 散点数据：含可选 size（气泡）。z 字段固定名供 ZAxis dataKey 读取。
  // 缺失 size 列值 → z=1（最小气泡）：鲁棒回退，避免缺失字段导致 NaN 渲染失败。
  const toPoint = (d: Record<string, unknown>) => {
    const pt: Record<string, number> = {
      [xKey]: Number(d[xKey]),
      [yKey]: Number(d[yKey]),
    };
    if (sizeKey) {
      const z = Number(d[sizeKey] ?? 1);
      pt.z = Number.isFinite(z) ? z : 1;
    }
    return pt;
  };

  // ZAxis 配置：size 列存在 → 气泡（range [50,400] 与 AH 一致）；否则等径 [36,36]。
  const zAxisConfig = sizeKey
    ? { dataKey: "z", range: [50, 400] as [number, number] }
    : { dataKey: undefined as undefined, range: [36, 36] as [number, number] };

  if (hue) {
    const hueValues = Array.from(new Set(data.map((d) => String(d[hue]))));
    const scatterSets = hueValues.map((val) =>
      data.filter((d) => String(d[hue]) === val).map(toPoint),
    );

    return (
      <div data-testid="chart-widget">
        <h4 className="orca-text-muted mb-2 text-xs font-medium">{title}</h4>
        <div className="aspect-[4/3] w-full">
          <ResponsiveContainer width="100%" height="100%" minHeight={200} minWidth={300}>
            <ScatterChart margin={CHART_MARGIN}>
              <CartesianGrid {...gridProps} />
              <XAxis
                dataKey={xKey}
                tick={axisTick}
                name={xAxisName}
                type="number"
                domain={xConfig.domain}
                ticks={xConfig.ticks}
                tickFormatter={formatTick}
                label={xAxisLabel}
              />
              <YAxis
                dataKey={yKey}
                tick={axisTick}
                name={yAxisName}
                type="number"
                domain={yConfig.domain}
                ticks={yConfig.ticks}
                tickFormatter={formatTick}
                label={yAxisLabel}
              />
              <ZAxis dataKey={zAxisConfig.dataKey} range={zAxisConfig.range} />
              <Tooltip
                contentStyle={tooltipStyle}
                cursor={tooltipCursor}
                labelStyle={tooltipTextStyle}
                itemStyle={tooltipTextStyle}
              />
              <Legend wrapperStyle={LEGEND_STYLE} />
              {hueValues.map((val, i) => (
                <Scatter
                  key={val}
                  name={val}
                  data={scatterSets[i]}
                  fill={PALETTE[i % PALETTE.length]}
                  fillOpacity={sizeKey ? BOX_FILL_OPACITY : undefined}
                  stroke={PALETTE[i % PALETTE.length]}
                  strokeWidth={BOX_STROKE_WIDTH}
                />
              ))}
            </ScatterChart>
          </ResponsiveContainer>
        </div>
        {caption && <ChartCaption text={caption} />}
      </div>
    );
  }

  const scatterData = data.map(toPoint);

  return (
    <div data-testid="chart-widget">
      <h4 className="orca-text-muted mb-2 text-xs font-medium">{title}</h4>
      <div className="aspect-[4/3] w-full">
        <ResponsiveContainer width="100%" height="100%" minHeight={200} minWidth={300}>
          <ScatterChart margin={CHART_MARGIN}>
            <CartesianGrid {...gridProps} />
            <XAxis
              dataKey={xKey}
              tick={axisTick}
              name={xAxisName}
              type="number"
              domain={xConfig.domain}
              ticks={xConfig.ticks}
              tickFormatter={formatTick}
              label={xAxisLabel}
            />
            <YAxis
              dataKey={yKey}
              tick={axisTick}
              name={yAxisName}
              type="number"
              domain={yConfig.domain}
              ticks={yConfig.ticks}
              tickFormatter={formatTick}
              label={yAxisLabel}
            />
            <ZAxis dataKey={zAxisConfig.dataKey} range={zAxisConfig.range} />
            <Tooltip
              contentStyle={tooltipStyle}
              cursor={tooltipCursor}
              labelStyle={tooltipTextStyle}
              itemStyle={tooltipTextStyle}
            />
            <Scatter
              data={scatterData}
              fill={PALETTE[0]}
              fillOpacity={sizeKey ? BOX_FILL_OPACITY : undefined}
              stroke={PALETTE[0]}
              strokeWidth={BOX_STROKE_WIDTH}
            >
              {/* color：per-point 着色（hue 优先，hue 块已 return；此处仅单 series 生效）。
                  每行 color 字段值是合法 CSS 色串，缺席回退 PALETTE[0]。color 空 → 无 Cell 子节点，
                  走上方 fill={PALETTE[0]} 统一色（零回归）。 */}
              {color
                ? data.map((d, i) => {
                    const c = String(d[color] ?? PALETTE[0]);
                    return <Cell key={i} fill={c} stroke={c} />;
                  })
                : null}
            </Scatter>
          </ScatterChart>
        </ResponsiveContainer>
      </div>
      {caption && <ChartCaption text={caption} />}
    </div>
  );
}
