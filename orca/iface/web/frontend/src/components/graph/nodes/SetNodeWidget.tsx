// components/graph/nodes/SetNodeWidget.tsx —— set 节点（状态色，无额外内容）。

import { memo } from "react";
import type { NodeProps } from "@xyflow/react";
import { NodeShell } from "./NodeShell";
import type { WorkflowNodeData } from "../graph-layout";

function SetNodeWidgetBase({ data }: NodeProps) {
  const d = data as WorkflowNodeData;
  return (
    <NodeShell data={d} kindLabel="set" />
  );
}

export const SetNodeWidget = memo(SetNodeWidgetBase);
