// components/conversation/ThinkingBlock.tsx —— agent_thinking 折叠块（SPEC §5.3）。
//
// 琥珀色折叠「💭 Thinking」+ markdown；流式时 `…` 脉动。默认折叠。

import { useState } from "react";
import { Brain, ChevronDown, ChevronRight } from "lucide-react";
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
    // P0：thinking = running 的子状态，语义色走 ``orca-running``（= --accent 钢蓝）。
    // 原 amber-500 系列（warning 语义）无对应 palette entry；plan §P0a 显式规定
    // blocked→skipped（violet）替代 amber，此处同源沿用「替换 amber 为 palette 语义」。
    // 视觉色相从 amber → 钢蓝有偏移，但语义一致性 > 严格保色（与 indigo→accent 同 spirit）。
    <div
      className="rounded-md border border-orca-running/30 bg-orca-running/5"
      data-testid="thinking-block"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-orca-running hover:text-orca-accent flex w-full items-center gap-1.5 px-2 py-1 text-xs"
        aria-expanded={open}
      >
        <span className="shrink-0 inline-flex items-center">{open ? <ChevronDown size={12} strokeWidth={1.5} aria-hidden /> : <ChevronRight size={12} strokeWidth={1.5} aria-hidden />}</span>
        <span className="inline-flex items-center gap-1"><Brain size={13} strokeWidth={1.5} aria-hidden /> Thinking</span>
        {stepLabel && (
          <span className="text-[10px] text-orca-running/80">· {stepLabel}</span>
        )}
        {streaming && (
          <span className="animate-pulse text-orca-running" data-testid="thinking-pulse">
            …
          </span>
        )}
      </button>
      {open && (
        <div className="border-t border-orca-running/20 px-2 py-1.5 max-h-64 overflow-y-auto">
          <MarkdownText>{text}</MarkdownText>
        </div>
      )}
    </div>
  );
}
