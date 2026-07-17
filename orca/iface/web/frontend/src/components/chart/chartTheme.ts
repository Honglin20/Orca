// components/chart/chartTheme.ts —— 学术配色 + 主题感知样式（SPEC §2.5，迁移自 AgentHarness）。
//
// 铁律 5（SPEC §0.1）：复用 AgentHarness 学术资产。PALETTE 8 色值逐字迁移（钢蓝/暖琥珀/灰珊瑚/鼠尾草青/橄榄绿/古金/柔紫/灰粉），
// POSITIVE/NEGATIVE/NEUTRAL 语义色，主题感知（读 CSS 变量 --border / --axis-tick / --foreground /
// --background / --accent 等，明暗自适应）。P5a：getAxisTick 改用 --axis-tick（slate-700 亮 /
// slate-300 暗），fontSize 11→12；新增 getCursor + getTooltipTextStyle 统一各 widget hover 行为。
//
// 迁移自 AgentHarness frontend/src/components/output/charts/chartTheme.ts —— 仅去掉 React
// 顶层 import（getTooltipStyle 改返回纯对象，CSS vars 由 index.css 提供），PALETTE 数值零改动。

// 8-color categorical palette — low saturation, Nature/IEEE style（迁移自 AgentHarness）
export const PALETTE = [
  "#5B8DB8", // muted steel blue
  "#E29D3E", // warm amber
  "#D4605A", // dusty coral
  "#6BA5A0", // sage teal
  "#6B9E5C", // olive green
  "#C9A843", // antique gold
  "#9A7BA8", // soft mauve
  "#E08E9B", // dusty rose
];

// Semantic colors for positive / negative / neutral
export const POSITIVE = "#6B9E5C";
export const NEGATIVE = "#D4605A";
export const NEUTRAL = "#9CA3AF";

// ── Theme-aware dynamic helpers（render-time 读 CSS vars，index.css 定义）──
// P5 R1 修复：index.css 所有颜色 token 是 ``R G B`` 三元组（与 Tailwind v3
// ``rgb(var(--x) / <alpha>)`` 兼容）。getCSSVar 必须用 ``rgb()`` 包裹，**不能用 ``hsl()``**
// —— CSS Color 4 要求 ``hsl(H S L)`` 中 S/L 为百分比；裸数字（如 ``hsl(51 65 85)``）
// 被浏览器静默判为非法，SVG fill 退回默认值（fail-quiet，违反 Rule 12）。
function getCSSVar(name: string): string {
  if (typeof document === "undefined") return "#888";
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  if (!raw) return "#888"; // 未定义 → fallback（不静默崩）
  // 支持 "R G B"（空格 RGB 三元组，index.css token 格式）或 "R G B / A" 或 hex。
  // RGB 三元组用 ``rgb()`` 包裹（合法 CSS Color 4）；hex 直接返回。
  if (/^\d/.test(raw) && raw.includes(" ")) return `rgb(${raw})`;
  return raw;
}

export function getGridStroke(): string {
  return getCSSVar("--border");
}

export function getAxisTick(): { fontSize: number; fill: string } {
  // P5a：fill 用 --axis-tick（slate-700 亮 / slate-300 暗，比旧 --muted-foreground
  // slate-500 更深一档），fontSize 11→12 提高可读性。解 SPEC §P5 症状「坐标轴太暗」。
  return { fontSize: 12, fill: getCSSVar("--axis-tick") };
}

export function getTooltipStyle(): React.CSSProperties {
  return {
    backgroundColor: getCSSVar("--background"),
    borderRadius: 8,
    border: `1px solid ${getCSSVar("--border")}`,
    boxShadow: "0 2px 8px rgba(0,0,0,0.08)",
    fontSize: 12,
    padding: "8px 12px",
    color: getCSSVar("--foreground"),
  };
}

/**
 * Tooltip 内文字（label + 各 item）的中性前景色（P5a：防 recharts 默认暖色显黄）。
 * 同时用于 ``<Tooltip labelStyle={...} itemStyle={...}/>``。
 */
export function getTooltipTextStyle(): { color: string } {
  return { color: getCSSVar("--foreground") };
}

/**
 * Tooltip cursor（P5a：解「hover 黄色刺眼」根因）。
 * - ``line=true``（Line/Area/Scatter/Radar）：细虚竖线无填充——既给定位反馈又不遮数据。
 * - ``line=false``（Bar/Pareto）：极淡灰填充——柱状高亮轻量，不抢色。
 *
 * 根因（SPEC §P5）：Line/Area/Scatter 的 ``<Tooltip>`` 原本缺 ``cursor``，recharts 默认
 * 高亮带 + PALETTE 暖色（amber #E29D3E / gold #C9A843）显黄。统一 cursor 后消除默认行为。
 */
export function getCursor(line: boolean): { stroke?: string; strokeWidth?: number; strokeDasharray?: string; fill?: string } {
  return line
    ? { stroke: getCSSVar("--border"), strokeWidth: 1, strokeDasharray: "3 3" }
    : { fill: "rgba(0,0,0,0.04)" };
}

export function getGridProps() {
  return {
    strokeDasharray: "3 3",
    stroke: getGridStroke(),
    vertical: false,
  };
}

// ── Structural constants（theme-independent）──
export const LEGEND_STYLE = { fontSize: 11 };
export const CHART_MARGIN = { top: 8, right: 24, bottom: 8, left: 0 };

// 半透明填充 + 圆角（柱状/pareto dominated 用）
export const BOX_FILL_OPACITY = 0.2;
export const BOX_STROKE_WIDTH = 1.5;
export const BOX_RADIUS = 3;
