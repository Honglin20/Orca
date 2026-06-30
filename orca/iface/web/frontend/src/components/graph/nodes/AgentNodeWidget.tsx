// components/graph/nodes/AgentNodeWidget.tsx —— agent 节点（状态色 + spinner）。
//
// 9c 不显示 token 数（token 在 detail panel 里看）；running 时显示 spinner。

import { memo } from "react";
import type { NodeProps } from "@xyflow/react";
import { NodeShell } from "./NodeShell";
import type { WorkflowNodeData } from "../graph-layout";

function AgentNodeWidgetBase({ data }: NodeProps) {
  const d = data as WorkflowNodeData;
  return (
    <NodeShell data={d} kindLabel="agent">
      {d.status === "running" && (
        <span className="mt-1 inline-block animate-spin text-xs">◌</span>
      )}
    </NodeShell>
  );
}

export const AgentNodeWidget = memo(AgentNodeWidgetBase);
