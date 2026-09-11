// components/chart/widgets/LineChartWidget.tsx —— recharts LineChart（迁移自 AgentHarness，chartTheme 学术配色）。
//
// 迁移自 AgentHarness LineChartWidget.tsx —— 主要改动：
//   - prop 名 chart → payload（对齐 SPEC §2.2 + Orca 命名）
//   - 去掉 EndLabel（简化，SPEC §3 只要求 recharts-line 渲染 + PALETTE 着色）
//   - hue 分组保留（多系列 pivot，反 AgentHarness 多源复杂度）
//   - 2026-09-11（用户拍板，prof-opt training curves 多线看不清）：hue 分支加
//     纯组件内交互——图例点击 toggle 显隐（隐藏项灰显）+ 悬停高亮（线体或图例
//     项触发：悬停线加粗、其余 strokeOpacity 0.15）。state 跨 REPLACE 推送
//     重渲染保持；隐藏优先于悬停；非 hue 单线分支不动。

import { useState } from "react";
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
  NEUTRAL,
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

/** 系列三态视觉（C4，导出供测试）：隐藏 > 悬停 > 常态。隐藏 = 整线退场
 * （strokeWidth 0 + dot r 0，opacity 0 双保险）；悬停 = 本线加粗、他线变淡
 * （strokeOpacity 0.15）；无悬停 = 常态。 */
export function seriesVisual(
  val: string,
  hidden: ReadonlySet<string>,
  hovered: string | null,
): { isHidden: boolean; isHovered: boolean; dimmed: boolean } {
  const isHidden = hidden.has(val);
  const isHovered = !isHidden && hovered === val;
  const dimmed = !isHidden && hovered !== null && !isHovered;
  return { isHidden, isHovered, dimmed };
}

export function LineChartWidget({ payload }: { payload: ChartPayload }) {
  const { data, x, y, hue, title, caption } = payload;
  const xKey = x ?? "x";
  const yKey = y ?? "y";
  const gridProps = getGridProps();
  const axisTick = getAxisTick();
  const tooltipStyle = getTooltipStyle();
  // P5a：line=true → 细虚竖线 cursor + 中性 labelStyle/itemStyle 防 recharts 默认黄。
  const tooltipCursor = getCursor(true);
  const tooltipTextStyle = getTooltipTextStyle();
  // 轴标签：x_label/y_label 优先（人话），空回退字段名（schema 名作 label）。
  const xAxisLabel = getXAxisLabelProp(payload);
  const yAxisLabel = getYAxisLabelProp(payload);
  // C4 交互 state（hooks 顶层，非 hue 分支不消费）：跨 REPLACE 重渲染保持（同
  // identity = title 复用实例）；系列消失后的残留 hidden 项无害（永不命中任何
  // 线）。已知边界：title 变 → identity 变 → remount 重置（如 report 终推的
  // "(final)" 后缀图）——终图全量展示属预期，非丢状态缺陷。
  const [hidden, setHidden] = useState<ReadonlySet<string>>(new Set());
  const [hovered, setHovered] = useState<string | null>(null);

  const toggleSeries = (value: unknown) => {
    const key = String(value);
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

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
            <LineChart data={pivotedData} margin={{ ...CHART_MARGIN, right: 60 }}>
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
              <Legend
                wrapperStyle={{ ...LEGEND_STYLE, cursor: "pointer" }}
                onClick={(entry: { value?: unknown }) => toggleSeries(entry?.value)}
                formatter={(value: unknown) => {
                  const key = String(value);
                  const { isHidden } = seriesVisual(key, hidden, hovered);
                  return (
                    <span
                      style={{ color: isHidden ? NEUTRAL : undefined, cursor: "pointer" }}
                      onMouseEnter={() => setHovered(key)}
                      onMouseLeave={() => setHovered(null)}
                    >
                      {String(value)}
                    </span>
                  );
                }}
              />
              {hueValues.map((val, i) => {
                const color = PALETTE[i % PALETTE.length];
                const { isHidden, isHovered, dimmed } = seriesVisual(val, hidden, hovered);
                return (
                  <Line
                    key={val}
                    dataKey={val}
                    stroke={color}
                    strokeOpacity={isHidden ? 0 : dimmed ? 0.15 : 1}
                    strokeWidth={isHidden ? 0 : isHovered ? 3 : 2}
                    dot={{ r: isHidden ? 0 : 3, fill: color, strokeWidth: 0 }}
                    activeDot={
                      isHidden
                        ? { r: 0 }
                        : { r: 5, strokeWidth: 2, stroke: "#fff", fill: color }
                    }
                    onMouseEnter={() => setHovered(val)}
                    onMouseLeave={() => setHovered(null)}
                  />
                );
              })}
            </LineChart>
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
          <LineChart data={data} margin={CHART_MARGIN}>
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
      {caption && <ChartCaption text={caption} />}
    </div>
  );
}
