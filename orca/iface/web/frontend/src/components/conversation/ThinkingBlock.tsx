// components/conversation/ThinkingBlock.tsx —— agent_thinking 折叠块（SPEC §5.3）。
//
// 琥珀色折叠「💭 Thinking」+ markdown；流式时 `…` 脉动。默认折叠。

import { useState } from "react";
import type { WebEvent } from "@/types/events";
import { MarkdownText } from "./MarkdownText";

interface ThinkingBlockProps {
  event: WebEvent;
  /** 附着的 step marker（agent_step_started），渲染「第 N 步」前缀。 */
  stepMarker?: WebEvent;
  /** 是否流式中（脉动 `…`）。 */
  streaming: boolean;
}

export function ThinkingBlock({ event, stepMarker, streaming }: ThinkingBlockProps) {
  const [open, setOpen] = useState(false);
  const text = String(event.data?.text ?? "");
  const stepLabel = stepMarker
    ? String(stepMarker.data?.step_reason ?? "step")
    : null;

  return (
    <div
      className="rounded-md border border-amber-500/30 bg-amber-500/5"
      data-testid="thinking-block"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 px-2 py-1 text-xs text-amber-700 dark:text-amber-300 hover:text-amber-500"
        aria-expanded={open}
      >
        <span className="shrink-0">{open ? "▼" : "▸"}</span>
        <span>💭 Thinking</span>
        {stepLabel && (
          <span className="text-[10px] text-amber-500/80">· {stepLabel}</span>
        )}
        {streaming && (
          <span className="animate-pulse text-amber-400" data-testid="thinking-pulse">
            …
          </span>
        )}
      </button>
      {open && (
        <div className="border-t border-amber-500/20 px-2 py-1.5 max-h-64 overflow-y-auto">
          <MarkdownText>{text}</MarkdownText>
        </div>
      )}
    </div>
  );
}
