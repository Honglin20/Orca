// components/conversation/ErrorBlock.tsx —— 红色 error block（SPEC §5.3 闭 review #29）。
//
// node_failed / workflow_failed：kind + message + phase 红块渲染。

import type { WebEvent } from "@/types/events";

export function ErrorBlock({ event }: { event: WebEvent }) {
  const d = event.data ?? {};
  const kind = String(d.kind ?? event.type);
  const message = String(d.message ?? "");
  const phase = d.phase != null ? String(d.phase) : null;

  return (
    <div
      className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-700 dark:text-red-300"
      data-testid="error-block"
    >
      <div className="flex items-center gap-1.5 font-medium">
        <span>✗</span>
        <span>{kind}</span>
        {phase && (
          <span className="text-[10px] text-red-500/80">@ {phase}</span>
        )}
      </div>
      {message && (
        <p className="mt-0.5 break-words text-red-700/90 dark:text-red-200/90">
          {message}
        </p>
      )}
    </div>
  );
}
