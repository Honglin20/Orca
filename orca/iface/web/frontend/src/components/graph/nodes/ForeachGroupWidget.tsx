// components/graph/nodes/ForeachGroupWidget.tsx —— foreach 节点（含进度计数）。
//
// foreach 是动态并行（运行时才知道分支数）。进度计数来自 events：foreach_started 的
// item_count（总数）+ foreach_item_completed 的 index（已完成）。9c store 从 events 派生
// 进度写入 node data（progress: "done/total"），widget 只读 data.progress。
//
// 此 widget 也复用给 parallel-group（静态并行组，branches 已知）：通过 data.branches 区分。

import { memo } from "react";
import type { NodeProps } from "@xyflow/react";
import { NodeShell } from "./NodeShell";
import type { WorkflowNodeData } from "../graph-layout";

function ForeachGroupWidgetBase({ data }: NodeProps) {
  const d = data as WorkflowNodeData;
  const isParallel = d.kind === "parallel-group";
  const kindLabel = isParallel ? "parallel" : "foreach";
  const progress = typeof d.progress === "string" ? d.progress : null;
  const branches = Array.isArray(d.branches) ? d.branches : null;
  return (
    <NodeShell data={d} kindLabel={kindLabel}>
      {progress && (
        <div className="mt-1 text-xs orca-text-faint" data-testid={`node-${d.name}-progress`}>
          {progress}
        </div>
      )}
      {branches && !progress && (
        <div className="mt-1 text-xs orca-text-faint">
          {branches.length} branches
        </div>
      )}
    </NodeShell>
  );
}

export const ForeachGroupWidget = memo(ForeachGroupWidgetBase);
