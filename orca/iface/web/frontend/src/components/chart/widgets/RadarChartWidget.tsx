// components/chart/widgets/RadarChartWidget.tsx —— recharts RadarChart（迁移自 AgentHarness，chartTheme 学术配色）。
//
// 迁移自 AgentHarness RadarChartWidget.tsx —— 主要改动：
//   - prop 名 chart → payload（对齐 SPEC §2.2 + Orca 命名）
//   - hue 分组用共享 pivotByHue（DRY，与 Line/Bar/Area 一致 —— 同样是 (axis, hue, value) → 宽格式）
//   - chartTheme 复用 Orca 版（同 PALETTE/getAxisTick/getTooltipStyle；getGridStroke 用 PolarGrid stroke）
//
// 雷达图数据：payload.data 为长格式 record array，每行一个 (维度, 值[, hue]) 元组；
// 默认 xKey="dimension"、yKey="value"（与 AgentHarness 一致）。单系列（无 hue）也可：
// 此时无需 pivot，直接喂 data。

import {
  PolarAngleAxis,
  PolarGrid,
  PolarRadiusAxis,
  Radar,
  RadarChart,
  ResponsiveContainer,
  Tooltip,
  Legend,
} from "recharts";
import type { ChartPayload } from "../types";
import {
  BOX_FILL_OPACITY,
  BOX_STROKE_WIDTH,
  LEGEND_STYLE,
  PALETTE,
  getAxisTick,
  getCursor,
  getGridStroke,
  getTooltipStyle,
  getTooltipTextStyle,
} from "../chartTheme";
import { computeNiceTicks, extractNumericValues } from "../axisUtils";
import { pivotByHue } from "../pivot";

export function RadarChartWidget({ payload }: { payload: ChartPayload }) {
  const { data, x, y, hue, title } = payload;
  // 默认维度列 dimension、值列 value（与 AgentHarness 雷达图惯例一致）
  const xKey = x ?? "dimension";
  const yKey = y ?? "value";
  const gridStroke = getGridStroke();
  const axisTick = getAxisTick();
  const tooltipStyle = getTooltipStyle();
  // P5a：Radar 归 line 类（细虚竖线 cursor；RadarChart cursor 实为十字辅助线，统一即可）。
  const tooltipCursor = getCursor(true);
  const tooltipTextStyle = getTooltipTextStyle();

  if (hue) {
    // hue 多系列：(维度, hue, 值) → 宽格式（每维度一行，各 hue 值作列）
    const { pivoted: pivotedData, hueValues } = pivotByHue(data, xKey, hue, yKey);
    const allValues = hueValues.flatMap((hv) =>
      extractNumericValues(pivotedData, hv),
    );
    const yConfig = computeNiceTicks(allValues);

    return (
      <div data-testid="chart-widget">
        <h4 className="orca-text-muted mb-2 text-xs font-medium">{title}</h4>
        <div className="aspect-square w-full max-w-[400px] mx-auto">
          <ResponsiveContainer width="100%" height="100%" minHeight={200} minWidth={300}>
            <RadarChart data={pivotedData} cx="50%" cy="50%" outerRadius="75%">
              <PolarGrid stroke={gridStroke} />
              <PolarAngleAxis
                dataKey={xKey}
                tick={{ fontSize: 10, fill: axisTick.fill }}
              />
              <PolarRadiusAxis
                tick={{ fontSize: 9, fill: axisTick.fill }}
                domain={yConfig.domain}
                ticks={yConfig.ticks}
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
                  <Radar
                    key={val}
                    name={val}
                    dataKey={val}
                    stroke={color}
                    fill={color}
                    fillOpacity={BOX_FILL_OPACITY}
                    strokeWidth={BOX_STROKE_WIDTH}
                  />
                );
              })}
            </RadarChart>
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
      <div className="aspect-square w-full max-w-[400px] mx-auto">
        <ResponsiveContainer width="100%" height="100%" minHeight={200} minWidth={300}>
          <RadarChart data={data} cx="50%" cy="50%" outerRadius="75%">
            <PolarGrid stroke={gridStroke} />
            <PolarAngleAxis
              dataKey={xKey}
              tick={{ fontSize: 10, fill: axisTick.fill }}
            />
            <PolarRadiusAxis
              tick={{ fontSize: 9, fill: axisTick.fill }}
              domain={yConfig.domain}
              ticks={yConfig.ticks}
            />
            <Tooltip
              contentStyle={tooltipStyle}
              cursor={tooltipCursor}
              labelStyle={tooltipTextStyle}
              itemStyle={tooltipTextStyle}
            />
            <Radar
              dataKey={yKey}
              stroke={PALETTE[0]}
              fill={PALETTE[0]}
              fillOpacity={BOX_FILL_OPACITY}
              strokeWidth={BOX_STROKE_WIDTH}
            />
          </RadarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
