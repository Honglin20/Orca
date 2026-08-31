// components/chart/widgets/ParetoChartWidget.tsx —— 散点 + Pareto 前沿连线（迁移自 AgentHarness）。
//
// 迁移自 AgentHarness ParetoChartWidget.tsx —— 主要改动：
//   - prop chart → payload
//   - findParetoFront 算法逐字迁移（按 pareto_direction / pareto_x/y_direction 判支配）
//   - chartTheme PALETTE[0] 前沿 + NEUTRAL dominated（学术配色）
//   - W-P3 修正（prof-opt v6 §10.2 消费侧）：x/y 为 null（达线未训占位）或非有限数
//     的点**不渲染**（原 ``Number(null)=0`` 强转把占位点画在 0 位，占位披露失真；
//     占位语义由推送方 caption 披露，widget 只负责不造假渲染）；payload.color 指定
//     per-row 状态色字段时按行着色（Cell 模式，同 ScatterChartWidget 惯例），
//     缺席回退前沿/被支配双色（零回归）。

import {
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
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
  getXAxisLabelValue,
  getYAxisLabelProp,
  getYAxisLabelValue,
} from "../chartTheme";
import { computeNiceTicks, formatTick } from "../axisUtils";
import { ChartCaption } from "../ChartCaption";

/** 计算非支配前沿（Pareto front）。迁移自 AgentHarness，逐字保留。 */
function findParetoFront(
  points: { x: number; y: number }[],
  xDir: "max" | "min",
  yDir: "max" | "min",
): Set<number> {
  const front = new Set<number>();
  for (let i = 0; i < points.length; i++) {
    let dominated = false;
    for (let j = 0; j < points.length; j++) {
      if (i === j) continue;
      const [ax, ay] = [points[i].x, points[i].y];
      const [bx, by] = [points[j].x, points[j].y];

      const xBetter = xDir === "max" ? bx >= ax : bx <= ax;
      const yBetter = yDir === "max" ? by >= ay : by <= ay;
      const xStrict = xDir === "max" ? bx > ax : bx < ax;
      const yStrict = yDir === "max" ? by > ay : by < ay;

      if (xBetter && yBetter && (xStrict || yStrict)) {
        dominated = true;
        break;
      }
    }
    if (!dominated) front.add(i);
  }
  return front;
}

/** 点是否可绘：null/undefined/空串/非有限数全部不可绘（null≠0，占位不落 0 位）。 */
function isPlottable(value: unknown): boolean {
  return (
    value !== null &&
    value !== undefined &&
    value !== "" &&
    Number.isFinite(Number(value))
  );
}

/**
 * W-P3 数据准备（导出供测试）：x/y 任一不可绘的行整点剔除——不进散点、
 * 不进轴刻度、不参与前沿支配判定（画在 0 位的假点会伪造支配关系）。
 * §10.2 的 y=null 占位语义由推送方 caption 披露，widget 只负责不造假渲染。
 */
export function prepareParetoPoints(
  data: Record<string, unknown>[],
  xKey: string,
  yKey: string
): {
  plottableRows: Record<string, unknown>[];
  points: { x: number; y: number }[];
} {
  const plottableRows = data.filter(
    (d) => isPlottable(d[xKey]) && isPlottable(d[yKey])
  );
  const points = plottableRows.map((d) => ({
    x: Number(d[xKey]),
    y: Number(d[yKey]),
  }));
  return { plottableRows, points };
}

/** per-row 状态色（W-P3，§10.2）：行缺色值（缺失/空串）回退 NEUTRAL（dumb 渲染
 * 不猜色——与 isPlottable 把空串判「缺」同一口径；ScatterChartWidget 的 `??`
 * 内联惯例不动它，红线：本次不改其他 widget）。 */
export function perRowColor(
  row: Record<string, unknown>,
  colorKey: string,
  fallback: string = NEUTRAL
): string {
  const raw = row[colorKey];
  return typeof raw === "string" && raw !== "" ? raw : fallback;
}

export function ParetoChartWidget({ payload }: { payload: ChartPayload }) {
  const { data, x, y, color, title, caption, pareto_direction, pareto_x_direction, pareto_y_direction } =
    payload;
  const xKey = x ?? "x";
  const yKey = y ?? "y";
  const xDir = pareto_x_direction ?? pareto_direction ?? "max";
  const yDir = pareto_y_direction ?? pareto_direction ?? "max";
  const gridProps = getGridProps();
  const axisTick = getAxisTick();
  const tooltipStyle = getTooltipStyle();
  // P5a：Pareto 主结构是散点+线（ComposedChart）但语义偏柱状（高亮整列）→ 归 false。
  // 用统一 getCursor(false) 极淡灰填充替代原 strokeDasharray（避免与散点 stroke 视觉冲突）。
  const tooltipCursor = getCursor(false);
  const tooltipTextStyle = getTooltipTextStyle();
  // 轴标签：x_label/y_label 优先，空回退字段名。
  const xAxisLabel = getXAxisLabelProp(payload);
  const yAxisLabel = getYAxisLabelProp(payload);
  const xAxisName = getXAxisLabelValue(payload);
  const yAxisName = getYAxisLabelValue(payload);

  // W-P3：x/y 任一不可绘的行整点剔除（含 null 占位）——不进散点、不进轴刻度、
  // 不参与前沿支配判定（一个画在 0 位的假点会伪造支配关系）。
  const { plottableRows, points } = prepareParetoPoints(data, xKey, yKey);

  const xConfig = computeNiceTicks(points.map((p) => p.x));
  const yConfig = computeNiceTicks(points.map((p) => p.y));

  const frontIndices = findParetoFront(points, xDir, yDir);
  const dominatedData = points
    .filter((_, i) => !frontIndices.has(i))
    .map((p) => ({ x: p.x, y: p.y }));
  const frontData = points
    .filter((_, i) => frontIndices.has(i))
    .map((p) => ({ x: p.x, y: p.y }));
  // 前沿连线数据：按 x 排序（阶梯状 Pareto front line）。recharts Line 走 chart-level data
  // 不便（ComposedChart 多 series 共享轴），用 per-series data；真实浏览器（playwright）下渲染，
  // happy-dom 单测下可能不出现（playwright 9d 集成测试补验证）。
  const sortedFront = [...frontData].sort((a, b) => a.x - b.x);
  // W-P3：per-row 状态着色（prof-opt §10.2 推送方按行携带状态色）。color 命中的行
  // 缺色值 → 回退 NEUTRAL（与 ScatterChartWidget 同惯例，dumb 渲染不猜色）。
  const colorKey = color ?? "";

  return (
    <div data-testid="chart-widget">
      <h4 className="orca-text-muted mb-2 text-xs font-medium">{title}</h4>
      <div className="aspect-[4/3] w-full">
        <ResponsiveContainer width="100%" height="100%" minHeight={200} minWidth={300}>
          <ComposedChart margin={CHART_MARGIN}>
            <CartesianGrid {...gridProps} />
            <XAxis
              dataKey="x"
              tick={axisTick}
              name={xAxisName}
              type="number"
              domain={xConfig.domain}
              ticks={xConfig.ticks}
              tickFormatter={formatTick}
              label={xAxisLabel}
            />
            <YAxis
              dataKey="y"
              tick={axisTick}
              name={yAxisName}
              type="number"
              domain={yConfig.domain}
              ticks={yConfig.ticks}
              tickFormatter={formatTick}
              label={yAxisLabel}
            />
            <ZAxis range={[40, 200]} />
            <Tooltip
              contentStyle={tooltipStyle}
              cursor={tooltipCursor}
              labelStyle={tooltipTextStyle}
              itemStyle={tooltipTextStyle}
            />
            <Legend wrapperStyle={LEGEND_STYLE} />
            {colorKey ? (
              // per-row 状态着色：单散点系列 + Cell 逐点落色（前沿语义保留在下方
              // 前沿连线，着色语义归推送方状态，不再用 dominated/front 双色覆盖）。
              <Scatter name="variants" data={points} fill={NEUTRAL} fillOpacity={0.85}>
                {plottableRows.map((d, i) => {
                  const c = perRowColor(d, colorKey);
                  return <Cell key={i} fill={c} stroke={c} />;
                })}
              </Scatter>
            ) : (
              <>
                <Scatter name="Dominated" data={dominatedData} fill={NEUTRAL} fillOpacity={0.5} />
                <Scatter name="Pareto Front" data={frontData} fill={PALETTE[0]} fillOpacity={0.85} />
              </>
            )}
            {sortedFront.length > 1 && (
              <Line
                name="Front Line"
                data={sortedFront}
                dataKey="y"
                stroke={PALETTE[0]}
                strokeWidth={2}
                strokeDasharray="6 3"
                dot={false}
                type="linear"
              />
            )}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      {caption && <ChartCaption text={caption} />}
    </div>
  );
}
