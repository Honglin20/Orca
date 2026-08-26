// components/graph/nodes/NodeShell.tsx —— 节点 widget 共享外壳（DRY，SPEC §1.2）。
//
// 所有 kind 的 node widget 共用：边框色 = NODE_STATUS_HEX[status]、左上 kind 标签、
// 右上状态点。kind 特有内容（token/进度/输出摘要）走 children slot。

import { memo } from "react";
import { Handle, Position } from "@xyflow/react";
import { NODE_STATUS_HEX } from "../constants";
import type { WorkflowNodeData } from "../graph-layout";

/** 节点 widget 共享外壳：状态色边框 + kind 标签 + children。 */
function NodeShellBase({
  data,
  kindLabel,
  children,
}: {
  data: WorkflowNodeData;
  kindLabel: string;
  children?: React.ReactNode;
}) {
  const status = data.status ?? "pending";
  const color = NODE_STATUS_HEX[status] ?? NODE_STATUS_HEX.pending;
  return (
    <div
      className="relative rounded-md border-2 orca-bg-surface px-3 py-2 shadow-sm"
      style={{ borderColor: color, minWidth: 160 }}
      data-testid={`node-${data.name}`}
      data-status={status}
    >
      {/* kind 标签（左上角小字） */}
      <span className="absolute -top-2 left-2 rounded orca-bg-surface-2 px-1 text-[10px] uppercase orca-text-faint">
        {kindLabel}
      </span>
      {/* 状态点（右上角） */}
      <span
        className="absolute -top-1.5 -right-1.5 h-3 w-3 rounded-full border border-white"
        style={{ backgroundColor: color }}
        data-testid={`node-${data.name}-dot`}
      />
      <div className="font-mono text-sm font-medium orca-text">
        {data.name}
      </div>
      {children}
      {/* ReactFlow 连接桩（DAG 入/出） */}
      <Handle type="target" position={Position.Top} />
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

export const NodeShell = memo(NodeShellBase);
