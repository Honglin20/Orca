// components/conversation/ToolGroup.tsx —— 连续工具成组（SPEC §5.3 折叠规则）。
//
// SPEC §5.3：连续 agent_tool_call+agent_tool_result 对（中间无 agent_message/
// agent_thinking）→ ``▸ N tools``（默认折叠；展开看每个 ToolRow）。

import { useState } from "react";
import type { ToolPair } from "./entries";
import { ToolRow } from "./ToolRow";

interface ToolGroupProps {
  pairs: ToolPair[];
}

export function ToolGroup({ pairs }: ToolGroupProps) {
  const [open, setOpen] = useState(false);
  if (pairs.length === 0) return null;

  const pendingCount = pairs.filter((p) => !p.result).length;

  return (
    <div
      className="ml-1 border-l-2 border-slate-200 pl-2 dark:border-slate-700"
      data-testid="tool-group"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 rounded px-1 py-1 text-left text-xs hover:bg-slate-100/60 dark:hover:bg-slate-700/40"
        aria-expanded={open}
      >
        <span className="shrink-0 text-slate-500">{open ? "▼" : "▸"}</span>
        <span className="font-medium text-slate-600 dark:text-slate-300">
          {pairs.length} tools
        </span>
        {pendingCount > 0 && (
          <span className="text-[10px] text-amber-500">
            ({pendingCount} running)
          </span>
        )}
      </button>
      {open && (
        <div className="mt-1 space-y-1">
          {pairs.map((p) => (
            <ToolRow key={p.tool_call_id} pair={p} defaultOpen={false} />
          ))}
        </div>
      )}
    </div>
  );
}
