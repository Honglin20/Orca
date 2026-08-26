// components/graph/constants.ts —— 节点状态颜色（SPEC §1.5）。
//
// hex 单一真相源（code-reviewer Y1：与 tailwind.config ``orca.*`` palette 同源，消除
// 「DAG/色条 blue-500 vs TopBar 钢蓝」的色相割裂）。DAG 节点边框、AgentsRail 色条、
// 状态点读此 hex（inline style 用）；TopBar/ThinkingBlock/ToolRow 等文字色走 ``orca.*``
// palette utility——二者**等价**（同 hex 值），只是消费入口不同（inline vs class）。
//
// 对齐后端 orca/schema/state.py Status Literal（"pending"|"running"|"done"|"failed"|
// "skipped"，纯字符串，后端不依赖 hex）+ 前端补 "blocked"（gate 阻塞语义，9d 派生）。

export const NODE_STATUS_HEX: Record<string, string> = {
  pending: "#94a3b8", // slate-400 = orca.pending（未开始）
  running: "#5b8db8", // 钢蓝 = orca.running / --accent（运行中）
  done: "#10b981", // emerald = orca.done（完成）
  failed: "#ef4444", // red = orca.failed（失败）
  skipped: "#94a3b8", // slate-400 = orca.pending（跳过同 pending 灰）
  blocked: "#a78bfa", // violet = orca.skipped（gate 阻塞，与 statusColor 同源）
};

/** 终止哨兵节点 id（后端 routes 的 to="$end"）。渲染为一个小终止标记。 */
export const END_NODE_ID = "$end";
