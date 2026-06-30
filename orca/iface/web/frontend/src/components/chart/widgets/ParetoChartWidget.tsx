// components/chart/widgets/ParetoChartWidget.tsx —— 散点 + Pareto 前沿连线（迁移自 AgentHarness）。
//
// 迁移自 AgentHarness ParetoChartWidget.tsx —— 主要改动：
//   - prop chart → payload
//   - findParetoFront 算法逐字迁移（按 pareto_direction / pareto_x/y_direction 判支配）
//   - chartTheme PALETTE[0] 前沿 + NEUTRAL dominated（学术配色）

import {
  CartesianGrid,
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
  getGridProps,
  getTooltipStyle,
} from "../chartTheme";
import { computeNiceTicks, formatTick } from "../axisUtils";

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

export function ParetoChartWidget({ payload }: { payload: ChartPayload }) {
  const { data, x, y, title, pareto_direction, pareto_x_direction, pareto_y_direction } =
    payload;
  const xKey = x ?? "x";
  const yKey = y ?? "y";
  const xDir = pareto_x_direction ?? pareto_direction ?? "max";
  const yDir = pareto_y_direction ?? pareto_direction ?? "max";
  const gridProps = getGridProps();
  const axisTick = getAxisTick();
  const tooltipStyle = getTooltipStyle();

  const points = data.map((d) => ({
    x: Number(d[xKey]),
    y: Number(d[yKey]),
  }));

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

  return (
    <div data-testid="chart-widget">
      <h4 className="mb-2 text-xs font-medium text-slate-700">{title}</h4>
      <div className="aspect-[4/3] w-full">
        <ResponsiveContainer width="100%" height="100%" minHeight={200} minWidth={300}>
          <ComposedChart margin={CHART_MARGIN}>
            <CartesianGrid {...gridProps} />
            <XAxis
              dataKey="x"
              tick={axisTick}
              name={xKey}
              type="number"
              domain={xConfig.domain}
              ticks={xConfig.ticks}
              tickFormatter={formatTick}
            />
            <YAxis
              dataKey="y"
              tick={axisTick}
              name={yKey}
              type="number"
              domain={yConfig.domain}
              ticks={yConfig.ticks}
              tickFormatter={formatTick}
            />
            <ZAxis range={[40, 200]} />
            <Tooltip contentStyle={tooltipStyle} cursor={{ strokeDasharray: "3 3" }} />
            <Legend wrapperStyle={LEGEND_STYLE} />
            <Scatter name="Dominated" data={dominatedData} fill={NEUTRAL} fillOpacity={0.5} />
            <Scatter name="Pareto Front" data={frontData} fill={PALETTE[0]} fillOpacity={0.85} />
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
    </div>
  );
}
