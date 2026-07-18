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
      className="ml-1 border-l-2 orca-border pl-2"
      data-testid="tool-group"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="hover:orca-bg-surface-2 flex w-full items-center gap-2 rounded px-1 py-1 text-left text-xs"
        aria-expanded={open}
      >
        <span className="shrink-0 orca-text-faint">{open ? "▼" : "▸"}</span>
        <span className="orca-text-muted font-medium">
          {pairs.length} tools
        </span>
        {pendingCount > 0 && (
          // P0：pending = running 语义（与 ToolRow spinner 同色），不再 amber。
          <span className="text-[10px] text-orca-running">
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
