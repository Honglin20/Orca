// components/conversation/MessageBlock.tsx —— agent_message（SPEC §5.3）。
//
// **永不折叠**（含最终 report）。完整 markdown 渲染 + 流式 ▎ 光标（由父决定显示与否）。

import type { WebEvent } from "@/types/events";
import { MarkdownText } from "./MarkdownText";

interface MessageBlockProps {
  event: WebEvent;
  stepMarker?: WebEvent;
  /** 是否显示 ▎ 流式光标（selectStreamingCursor 派生）。 */
  showCursor: boolean;
}

export function MessageBlock({ event, stepMarker, showCursor }: MessageBlockProps) {
  const text = String(event.data?.text ?? "");
  const stepLabel = stepMarker
    ? String(stepMarker.data?.step_reason ?? "step")
    : null;

  return (
    <div className="py-1" data-testid="message-block">
      {stepLabel && (
        <div className="mb-1 text-[10px] uppercase tracking-wide orca-text-faint">
          · {stepLabel}
        </div>
      )}
      <div className="min-w-0 text-sm">
        <MarkdownText>{text}</MarkdownText>
        {showCursor && (
          <span
            className="orca-text animate-pulse"
            data-testid="streaming-cursor"
          >
            ▎
          </span>
        )}
      </div>
    </div>
  );
}
