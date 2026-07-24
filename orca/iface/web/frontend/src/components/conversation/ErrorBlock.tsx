// components/conversation/ErrorBlock.tsx —— 红色 error block（SPEC §5.3 闭 review #29）。
//
// node_failed / workflow_failed：kind + message + phase 红块渲染。

import { XCircle } from "lucide-react";
import type { WebEvent } from "@/types/events";

export function ErrorBlock({ event }: { event: WebEvent }) {
  const d = event.data ?? {};
  const kind = String(d.kind ?? event.type);
  const message = String(d.message ?? "");
  const phase = d.phase != null ? String(d.phase) : null;

  return (
    <div
      className="text-orca-failed rounded-md border border-orca-failed/40 bg-orca-failed/10 px-3 py-2 text-xs"
      data-testid="error-block"
    >
      <div className="flex items-center gap-1.5 font-medium">
        <span className="inline-flex items-center"><XCircle size={14} strokeWidth={1.5} aria-hidden /></span>
        <span>{kind}</span>
        {phase && (
          <span className="text-[10px] text-orca-failed/80">@ {phase}</span>
        )}
      </div>
      {message && (
        <p className="text-orca-failed/90 mt-0.5 break-words">
          {message}
        </p>
      )}
    </div>
  );
}
