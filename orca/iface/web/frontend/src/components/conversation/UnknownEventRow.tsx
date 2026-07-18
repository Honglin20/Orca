// components/conversation/UnknownEventRow.tsx —— unknown_event dim 可展开（SPEC §5.3 / D8）。
//
// D8：unknown_event MUST no-op in reducer，但渲染层仍可让用户看到 raw（debug 用）。

import { useState } from "react";
import type { WebEvent } from "@/types/events";
import { safeJson } from "./_shared";

export function UnknownEventRow({ event }: { event: WebEvent }) {
  const [open, setOpen] = useState(false);
  const source = String(event.data?.source ?? "?");

  return (
    <div data-testid="unknown-event-row">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="orca-text-faint hover:orca-text-muted flex items-center gap-1.5 px-1 py-0.5 text-[11px]"
        aria-expanded={open}
      >
        <span className="shrink-0">{open ? "▼" : "▸"}</span>
        <span className="shrink-0">?</span>
        <span className="font-mono">unknown ({source})</span>
      </button>
      {open && (
        <pre className="orca-bg-surface-2 orca-text-muted mt-1 ml-4 max-h-48 overflow-auto whitespace-pre-wrap rounded p-2 text-[11px]">
          {safeJson(event.data?.raw ?? event.data)}
        </pre>
      )}
    </div>
  );
}
