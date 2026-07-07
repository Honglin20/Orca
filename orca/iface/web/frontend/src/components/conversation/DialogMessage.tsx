// components/conversation/DialogMessage.tsx —— dialog_message（SPEC §5.3）。
//
// 多轮追问的 user/agent turn，agent_message 同款 markdown + turn 标记。

import type { WebEvent } from "@/types/events";
import { MarkdownText } from "./MarkdownText";

export function DialogMessage({ event }: { event: WebEvent }) {
  const role = String(event.data?.role ?? "user");
  const text = String(event.data?.text ?? "");
  const isAgent = role === "agent" || role === "assistant";

  return (
    <div
      className={`ml-2 border-l-2 pl-2 ${
        isAgent
          ? "border-blue-300 dark:border-blue-700"
          : "border-slate-300 dark:border-slate-600"
      }`}
      data-testid="dialog-message"
    >
      <div className="mb-0.5 text-[10px] uppercase tracking-wide text-slate-400">
        {role}
      </div>
      <MarkdownText>{text}</MarkdownText>
    </div>
  );
}
