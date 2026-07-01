// components/chart/types.ts —— ChartPayload 契约（SPEC §2.2，迁移自 AgentHarness 扁平 record-array spec）。
//
// claude 调 render_chart MCP 工具（phase 10）→ executor emit custom 事件 → tape。
// 9d 范围 7 种 chart_type：line/bar/area/scatter/pareto/radar/table。

export type ChartType =
  | "line"
  | "bar"
  | "area"
  | "scatter"
  | "pareto"
  | "radar"
  | "table";

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
  /** pareto 特有：前沿方向 */
  pareto_direction?: "max" | "min";
  pareto_x_direction?: "max" | "min";
  pareto_y_direction?: "max" | "min";
}
