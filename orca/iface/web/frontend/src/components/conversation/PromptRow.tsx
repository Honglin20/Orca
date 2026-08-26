// components/conversation/PromptRow.tsx —— prompt_rendered 折叠行（SPEC §5.3）。
//
// 默认折叠 ``▸ user prompt``；展开看 data.preview markdown 渲染。

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
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
        className="orca-text-muted hover:orca-text flex items-center gap-1.5 text-xs"
        aria-expanded={open}
      >
        <span className="shrink-0 inline-flex items-center">{open ? <ChevronDown size={12} strokeWidth={1.5} aria-hidden /> : <ChevronRight size={12} strokeWidth={1.5} aria-hidden />}</span>
        <span className="font-medium">user prompt</span>
      </button>
      {open && preview && (
        <div className="orca-border orca-bg-surface-2 mt-1 max-h-64 overflow-y-auto rounded-md border px-2 py-1.5">
          <MarkdownText>{preview}</MarkdownText>
        </div>
      )}
    </div>
  );
}
