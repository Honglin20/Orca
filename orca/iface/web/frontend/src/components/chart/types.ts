// components/chart/types.ts —— ChartPayload 契约（SPEC §2.2，迁移自 AgentHarness 扁平 record-array spec）。
//
// claude 调 render_chart MCP 工具（phase 10）→ executor emit custom 事件 → tape。
// 9d 范围 8 种 chart_type：line/bar/area/scatter/pareto/radar/table/heatmap。

export type ChartType =
  | "line"
  | "bar"
  | "area"
  | "scatter"
  | "pareto"
  | "radar"
  | "table"
  | "heatmap";

export interface ChartPayload {
  chart_type: ChartType;
  /** 扁平 record array（迁移自 AgentHarness chart.py） */
  data: Record<string, unknown>[];
  /** 列表列名（用于 table 类型派生 columns；其余类型可空） */
  columns?: string[];
  x?: string;
  y?: string;
  /** 分组键（同 label+title 替换非追加，SPEC §2.7 实时时更新） */
  label: string;
  /** 图标题（同 label 下唯一键，实时替换） */
  title: string;
  /** hue 分组（line/bar/area/scatter/radar 多系列着色） */
  hue?: string;
  /** per-row fill 颜色字段名（bar/scatter）：每行该字段值为合法 CSS 色串，渲染时每根柱/点按行着色。
   *  与 hue 互斥语义：hue 缺席时生效（hue 优先 → 分组并排）。着色逻辑在调用脚本，前端 dumb 渲染。 */
  color?: string;
  /** 散点大小（scatter 类型作气泡图；ZAxis dataKey 映射，SPEC §5.4 / §0 D3） */
  size?: string;
  /**
   * heatmap cell 着色字段名（长格式 record 一个 cell：``{recipe, bitwidth, accuracy}``）。
   * 渲染器按 (y, x) pivot 成网格，按 value 做色阶（color scale）。chart_type='heatmap' 时必填
   * （后端 ``_validate`` fail loud 拒收空 value）。参考 scatter ``size`` 的字段名映射模式。
   */
  value?: string;
  /**
   * 多系列列名（备用扩展位，SPEC §5.4 列入契约但当前 7 widget 均用 ``hue`` 表达多系列，
   * 无 widget 消费 ``series``）。保留字段：未来若引入双轴 / 复合 chart（如 AH
   * DistOverlayChartWidget 的 series 配置）时使用，避免后续契约再扩。
   */
  series?: string;
  /** pareto 特有：前沿方向 */
  pareto_direction?: "max" | "min";
  pareto_x_direction?: "max" | "min";
  pareto_y_direction?: "max" | "min";
}
