// components/graph/nodes/EndNodeWidget.tsx —— $end 终止哨兵节点（小圆点）。

import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { WorkflowNodeData } from "../graph-layout";

function EndNodeWidgetBase({ data }: NodeProps) {
  const d = data as WorkflowNodeData;
  return (
    <div
      className="flex h-6 w-6 items-center justify-center rounded-full border-2 border-slate-400 bg-slate-100 text-[9px] text-slate-500"
      data-testid={`node-${d.name}`}
    >
      end
      <Handle type="target" position={Position.Top} />
    </div>
  );
}

export const EndNodeWidget = memo(EndNodeWidgetBase);
