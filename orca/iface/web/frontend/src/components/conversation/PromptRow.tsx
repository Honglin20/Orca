// components/conversation/PromptRow.tsx —— prompt_rendered 折叠行（SPEC §5.3）。
//
// 默认折叠 ``▸ user prompt``；展开看 data.preview markdown 渲染。

import { useState } from "react";
import type { WebEvent } from "@/types/events";
import { MarkdownText } from "./MarkdownText";

export function PromptRow({ event }: { event: WebEvent }) {
  const [open, setOpen] = useState(false);
  // fallback：translator 可能写 data.text / data.prompt 而非 preview（兼容历史 / 异构 translator）。
  const preview = String(
    event.data?.preview ?? event.data?.text ?? event.data?.prompt ?? ""
  );

  return (
    <div data-testid="prompt-row">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 text-xs text-slate-600 dark:text-slate-300 hover:text-slate-900 dark:hover:text-slate-100"
        aria-expanded={open}
      >
        <span className="shrink-0">{open ? "▼" : "▸"}</span>
        <span className="font-medium">user prompt</span>
      </button>
      {open && preview && (
        <div className="mt-1 rounded-md border border-slate-200 dark:border-slate-700 bg-slate-50/80 dark:bg-slate-800/40 px-2 py-1.5 max-h-64 overflow-y-auto">
          <MarkdownText>{preview}</MarkdownText>
        </div>
      )}
    </div>
  );
}
