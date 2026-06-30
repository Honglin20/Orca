// components/chart/chartTheme.ts —— 学术配色 + 主题感知样式（SPEC §2.5，迁移自 AgentHarness）。
//
// 铁律 5（SPEC §0.1）：复用 AgentHarness 学术资产。PALETTE 8 色值逐字迁移（钢蓝/暖琥珀/灰珊瑚/鼠尾草青/橄榄绿/古金/柔紫/灰粉），
// POSITIVE/NEGATIVE/NEUTRAL 语义色，主题感知（读 CSS 变量 --border/--muted-foreground 等，明暗自适应）。
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
function getCSSVar(name: string): string {
  if (typeof document === "undefined") return "#888";
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  if (!raw) return "#888"; // 未定义 → fallback（不静默崩）
  // 支持 "H S L"（空格）或 "H S L / A%" 或 hex；空格格式按 hsl() 包裹
  if (/^\d/.test(raw) && raw.includes(" ")) return `hsl(${raw})`;
  return raw;
}

export function getGridStroke(): string {
  return getCSSVar("--border");
}

export function getAxisTick(): { fontSize: number; fill: string } {
  return { fontSize: 11, fill: getCSSVar("--muted-foreground") };
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
