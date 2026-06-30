// components/graph/constants.ts —— 节点状态颜色（SPEC §1.5）。
//
// 5 色映射（pending/running/done/failed/blocked），对齐后端 orca/schema/state.py Status
// Literal（"pending"|"running"|"done"|"failed"|"skipped"）+ 前端补 "blocked"（gate 阻塞语义，
// 由 9d gate 弹窗派生；9c 先定义色值，避免硬编码）。

export const NODE_STATUS_HEX: Record<string, string> = {
  pending: "#9CA3AF", // 灰：未开始
  running: "#3B82F6", // 蓝：运行中
  done: "#22C55E", // 绿：完成
  failed: "#EF4444", // 红：失败
  skipped: "#9CA3AF", // 灰：跳过（同 pending 色）
  blocked: "#F59E0B", // 黄：gate 阻塞（9d）
};

/** 终止哨兵节点 id（后端 routes 的 to="$end"）。渲染为一个小终止标记。 */
export const END_NODE_ID = "$end";
