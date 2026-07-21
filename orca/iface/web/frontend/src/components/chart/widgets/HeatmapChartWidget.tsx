// components/chart/widgets/HeatmapChartWidget.tsx —— heatmap：长格式 record → 行×列矩阵 cell 着色。
//
// recharts 无原生 heatmap；本组件用 CSS Grid + 线性色阶实现（无新依赖，铁律：依赖要轻）。
//
// 数据契约（types.ts ChartPayload.value）：
//   - 长格式扁平 record array，每行一个 cell：``{recipe, bitwidth, accuracy}``。
//   - ``y`` = 行轴字段（如 recipe），``x`` = 列轴字段（如 bitwidth），
//     ``value`` = 着色字段（如 accuracy）。三者**必非空**——后端 ``_validate`` fail loud
//     拒收任一为空（防前端 pivot 退化成 1×1 垃圾矩阵）。
//   - 渲染器把长格式 pivot 成网格：unique yValues × unique xValues，缺失 / 非数值 cell 显示空位。
//
// 色阶：浅钢蓝（min）→ PALETTE[0] 钢蓝（max）。textColor 在 t > 0.55 切白色（对比度可读）。
// 单值矩阵（max == min）→ 全部 PALETTE[0]，避免除零。

import { Fragment } from "react";
import type { ChartPayload } from "../types";
import { PALETTE } from "../chartTheme";
import { formatTick } from "../axisUtils";
import { ChartCaption } from "../ChartCaption";

// 色阶端点：极浅 → PALETTE[0]（钢蓝 #5B8DB8 = rgb(91,141,184)）。
const SCALE_LIGHT: readonly [number, number, number] = [245, 248, 251];
const SCALE_DARK: readonly [number, number, number] = [
  parseInt(PALETTE[0].slice(1, 3), 16),
  parseInt(PALETTE[0].slice(3, 5), 16),
  parseInt(PALETTE[0].slice(5, 7), 16),
];

/**
 * 把 raw 值转 number 或 null（m1）：
 *   - number 且 finite → 直接返回；
 *   - 非空字符串且可解析为 finite number → 解析返回；
 *   - null / undefined / 空串 / 非数字字符串 / 布尔 → null（视为缺失，**不静默 coerce 成 0**）。
 *
 * ``Number(null)===0`` / ``Number("")===0`` / ``Number(false)===0`` 会让缺失 cell 被画成
 * 深色端（min=0），视觉误导。本 helper 显式拒绝这些边界。
 */
function toNumberOrNull(raw: unknown): number | null {
  if (typeof raw === "number" && Number.isFinite(raw)) return raw;
  if (typeof raw === "string" && raw.trim() !== "") {
    const n = Number(raw);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

export function HeatmapChartWidget({ payload }: { payload: ChartPayload }) {
  const { data, x, y, value, title, x_label, y_label, caption } = payload;
  // 后端 _validate 已强制 heatmap 必非空 value/x/y（fail loud）。前端不做 fallback
  // （fallback 会掩盖问题）——若 value/x/y 缺，显示 fail loud 提示（兼容历史 tape 或未走
  // _render 的 custom 事件）。
  const xKey = x ?? "";
  const yKey = y ?? "";
  const valueKey = value ?? "";

  if (!data || data.length === 0) {
    return (
      <div data-testid="chart-widget">
        {title && (
          <h4 className="orca-text-muted mb-2 text-xs font-medium">{title}</h4>
        )}
        <p className="p-2 text-xs orca-text-faint" data-testid="heatmap-empty">
          暂无数据
        </p>
        {caption && <ChartCaption text={caption} />}
      </div>
    );
  }

  if (!valueKey || !xKey || !yKey) {
    // fail loud 提示（后端 _validate 已挡，此处仅防御未走 _render 的 custom 事件 / 历史 tape）。
    const missing: string[] = [];
    if (!xKey) missing.push("x（列轴字段名）");
    if (!yKey) missing.push("y（行轴字段名）");
    if (!valueKey) missing.push("value（cell 着色字段名）");
    return (
      <div data-testid="chart-widget">
        {title && (
          <h4 className="orca-text-muted mb-2 text-xs font-medium">{title}</h4>
        )}
        <p className="p-2 text-xs text-orca-failed" data-testid="heatmap-no-value">
          heatmap 缺必填字段：{missing.join("、")}
        </p>
        {caption && <ChartCaption text={caption} />}
      </div>
    );
  }

  // Pivot 长格式 → 矩阵。unique xValues（列）/ yValues（行）保首次出现序（与 pivotByHue 一致）。
  const xValues = Array.from(new Set(data.map((d) => String(d[xKey]))));
  const yValues = Array.from(new Set(data.map((d) => String(d[yKey]))));

  // cell 映射：``y|x`` → 数值（null 表示缺失 / 非数值 / 空串 / 布尔 —— 都视为「无值」，
  // 不静默 coerce 成 0 否则误导色阶，m1 修）。
  const cellMap = new Map<string, number | null>();
  for (const row of data) {
    const key = `${String(row[yKey])}|${String(row[xKey])}`;
    cellMap.set(key, toNumberOrNull(row[valueKey]));
  }

  // 计算 min/max 用于色阶。用 reduce 而非 ``Math.min(...arr)``：避免 spread 大数组时
  // V8/JSC 栈溢出（默认 max_points=2000 远低，但 script 可传更大 → 防御性）。
  const numericValues: number[] = [];
  for (const v of cellMap.values()) {
    if (v !== null) numericValues.push(v);
  }
  const min = numericValues.length > 0
    ? numericValues.reduce((a, b) => (b < a ? b : a), Infinity)
    : 0;
  const max = numericValues.length > 0
    ? numericValues.reduce((a, b) => (b > a ? b : a), -Infinity)
    : 0;

  function normalize(v: number): number {
    if (max === min) return 1; // 单值矩阵 → 全用深色端（避免除零 + 视觉一致）
    return (v - min) / (max - min);
  }

  function colorFor(v: number | null): string {
    if (v === null) return ""; // 缺失 cell 用 orca-bg-surface-2 class（CSS token 适配明暗）
    const t = normalize(v);
    const r = Math.round(SCALE_LIGHT[0] + (SCALE_DARK[0] - SCALE_LIGHT[0]) * t);
    const g = Math.round(SCALE_LIGHT[1] + (SCALE_DARK[1] - SCALE_LIGHT[1]) * t);
    const b = Math.round(SCALE_LIGHT[2] + (SCALE_DARK[2] - SCALE_LIGHT[2]) * t);
    return `rgb(${r}, ${g}, ${b})`;
  }

  function textColorFor(v: number | null): string {
    if (v === null) return "";
    // 深色 cell 用白字，浅色 cell 用深字（对比度 WCAG AA）。
    return normalize(v) > 0.55 ? "#FFFFFF" : "#1F2937";
  }

  // 色阶 legend 渐变：min → max 线性。单值时两端同色（legend 仍渲染，只是色阶退化为单色）。
  const legendGradient = `linear-gradient(to right, ${colorFor(min)}, ${colorFor(max)})`;

  return (
    <div data-testid="chart-widget">
      {title && (
        <h4 className="orca-text-muted mb-2 text-xs font-medium">{title}</h4>
      )}
      <div
        className="overflow-auto rounded border orca-border"
        data-testid="heatmap-grid"
        style={{
          display: "grid",
          // 首列 minmax(80px, auto) 容纳行标签；其余列等分（minmax(40px, 1fr)）。
          gridTemplateColumns: `minmax(80px, auto) repeat(${xValues.length}, minmax(40px, 1fr))`,
          gap: 1,
          // gap 缝隙色（inline style 不能用 var(--border)——tokens 是 RGB 三元组，见 chartTheme
          // P5 R1 fix；用低对比 rgba 在明暗主题均可接受的折中）。
          backgroundColor: "rgba(120,120,120,0.18)",
        }}
      >
        {/* 表头行：左上角空 + 各列标签 */}
        <div className="orca-bg-surface-2" />
        {xValues.map((xv) => (
          <div
            key={xv}
            className="orca-bg-surface-2 orca-text-muted px-1 py-1 text-center text-[11px]"
          >
            {xv}
          </div>
        ))}
        {/* 数据行：行标签 + 单元格 */}
        {yValues.map((yv) => (
          <Fragment key={yv}>
            <div
              className="orca-bg-surface-2 orca-text-muted truncate px-2 py-1 text-right text-[11px]"
              title={yv}
            >
              {yv}
            </div>
            {xValues.map((xv) => {
              const v = cellMap.get(`${yv}|${xv}`) ?? null;
              const bg = colorFor(v);
              const fg = textColorFor(v);
              return (
                <div
                  key={`${yv}|${xv}`}
                  data-testid="heatmap-cell"
                  data-x={xv}
                  data-y={yv}
                  data-value={v ?? ""}
                  title={`${yv} × ${xv}: ${v === null ? "—" : formatTick(v)}`}
                  className={
                    v === null
                      ? "orca-bg-surface-2 px-1 py-1 text-center text-[11px]"
                      : "px-1 py-1 text-center text-[11px]"
                  }
                  style={
                    v === null
                      ? undefined
                      : {
                          backgroundColor: bg,
                          color: fg,
                          fontVariantNumeric: "tabular-nums",
                        }
                  }
                >
                  {v === null ? "" : formatTick(v)}
                </div>
              );
            })}
          </Fragment>
        ))}
      </div>
      {/* 色阶 legend：min → max 渐变条 + 两端数值。 */}
      <div className="mt-2 flex items-center gap-2 text-[10px] orca-text-faint">
        <span>{formatTick(min)}</span>
        <div
          data-testid="heatmap-legend"
          style={{
            width: 80,
            height: 8,
            borderRadius: 2,
            background: legendGradient,
            border: "1px solid rgba(120,120,120,0.25)",
          }}
        />
        <span>{formatTick(max)}</span>
        <span className="orca-text-faint">({valueKey})</span>
      </div>
      {/* 轴标题（x_label=列轴语义，y_label=行轴语义；空 span 不渲染避免占位）。 */}
      {(x_label || y_label) && (
        <div className="mt-1 flex justify-between text-[10px] orca-text-faint">
          {y_label && <span>{y_label}</span>}
          {x_label && <span>{x_label}</span>}
        </div>
      )}
      {caption && <ChartCaption text={caption} />}
    </div>
  );
}
