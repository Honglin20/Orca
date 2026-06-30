// components/graph/nodes/ScriptNodeWidget.tsx —— script 节点（状态色，无额外内容）。

import { memo } from "react";
import type { NodeProps } from "@xyflow/react";
import { NodeShell } from "./NodeShell";
import type { WorkflowNodeData } from "../graph-layout";

function ScriptNodeWidgetBase({ data }: NodeProps) {
  const d = data as WorkflowNodeData;
  return (
    <NodeShell data={d} kindLabel="script" />
  );
}

export const ScriptNodeWidget = memo(ScriptNodeWidgetBase);
