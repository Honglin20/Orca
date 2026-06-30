// components/chart/axisUtils.ts —— 轴 nice ticks 计算（迁移自 AgentHarness，纯数学工具零依赖）。
//
// 提供 computeNiceTicks（nice 轴刻度 + domain）/ formatTick（K/M 缩写）/ extractNumericValues。
// 迁移自 AgentHarness frontend/src/components/output/charts/axisUtils.ts —— 零改动（纯函数，无 AgentHarness 模块依赖）。

/**
 * Compute a "nice" number approximately equal to `range`.
 * Rounds to 1, 2, or 5 times a power of 10.
 */
function niceNum(range: number, round: boolean): number {
  const exp = Math.floor(Math.log10(Math.abs(range) || 1));
  const frac = range / Math.pow(10, exp);
  let nice: number;
  if (round) {
    nice = frac < 1.5 ? 1 : frac < 3 ? 2 : frac < 7 ? 5 : 10;
  } else {
    nice = frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 5 ? 5 : 10;
  }
  return nice * Math.pow(10, exp);
}

/**
 * Compute evenly-spaced nice tick values for an axis.
 *
 * @param values - all data values on this axis
 * @param tickCount - target number of ticks (default 5)
 * @returns array of tick values and the [min, max] domain
 */
export function computeNiceTicks(
  values: number[],
  tickCount = 5,
): { ticks: number[]; domain: [number, number] } {
  if (values.length === 0) {
    return { ticks: [0], domain: [0, 1] };
  }

  const dataMin = Math.min(...values);
  const dataMax = Math.max(...values);

  // All non-negative and min is small relative to range → start from 0
  const startFromZero = dataMin >= 0 && dataMin < (dataMax - dataMin) * 0.2;

  const rangeLo = startFromZero ? 0 : dataMin;
  const rangeHi = dataMax;
  const range = rangeHi - rangeLo || 1;

  const spacing = niceNum(range / (tickCount - 1), true);
  const niceMin = Math.floor(rangeLo / spacing) * spacing;
  const niceMax = Math.ceil(rangeHi / spacing) * spacing;

  const ticks: number[] = [];
  for (let t = niceMin; t <= niceMax + spacing * 0.5; t += spacing) {
    ticks.push(Math.round(t * 1e10) / 1e10); // avoid float drift
  }

  return { ticks, domain: [niceMin, niceMax] };
}

/**
 * Format a tick value for display.
 * Large numbers: 1200 → "1.2K", 1500000 → "1.5M"
 * Small numbers: 0.00123 → "0.001"
 */
export function formatTick(value: number): string {
  const abs = Math.abs(value);
  if (abs === 0) return "0";

  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (abs >= 10_000) return `${(value / 1_000).toFixed(0)}K`;
  if (abs >= 1_000) return `${(value / 1_000).toFixed(1).replace(/\.0$/, "")}K`;

  if (abs < 0.01 && abs > 0) return value.toPrecision(2);
  if (abs < 1) return value.toPrecision(3);

  if (Number.isInteger(value)) return String(value);

  return value.toPrecision(4).replace(/\.?0+$/, "");
}

/** Extract numeric values from data rows for a given key (non-numeric skipped). */
export function extractNumericValues(
  data: Record<string, unknown>[],
  key: string,
): number[] {
  return data.map((d) => Number(d[key])).filter((v) => !isNaN(v));
}
