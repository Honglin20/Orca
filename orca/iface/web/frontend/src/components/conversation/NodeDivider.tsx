// components/conversation/NodeDivider.tsx —— 节点/dialog/step 细分隔符（SPEC §5.3）。
//
//   - node_started / node_skipped → dim 细分隔（P1：▶/⊘ emoji → lucide Play/CircleSlash）
//   - ``node_completed`` 已升格为 output block（B1），不再作 divider——见 NodeOutputBlock。
//   - dialog_started / dialog_ended → ``── dialog ──``（ASCII 装饰线，保留）
//   - 无后续 message/thinking 的 step_marker → dim ``· step`` 分隔（保留）

import { Play, CircleSlash } from "lucide-react";
import type { WebEvent } from "@/types/events";

export function NodeDivider({ event }: { event: WebEvent }) {
  let icon: React.ReactNode = null;
  let text: string;
  switch (event.type) {
    case "node_started":
      icon = <Play size={11} strokeWidth={1.5} aria-hidden />;
      text = `${event.node ?? "node"} started`;
      break;
    case "node_skipped":
      icon = <CircleSlash size={11} strokeWidth={1.5} aria-hidden />;
      text = `${event.node ?? "node"} skipped`;
      break;
    default:
      text = event.type;
  }
  return (
    <div
      className="orca-text-faint flex items-center gap-2 py-1 text-[10px] uppercase tracking-wide"
      data-testid="node-divider"
    >
      <span className="orca-bg-border h-px flex-1" />
      <span className="inline-flex items-center gap-1">
        {icon}
        {text}
      </span>
      <span className="orca-bg-border h-px flex-1" />
    </div>
  );
}

export function DialogDivider({ event }: { event: WebEvent }) {
  const turns =
    event.type === "dialog_ended"
      ? ` (${Number(event.data?.total_turns ?? 0)} turns)`
      : "";
  return (
    <div
      className="orca-text-faint flex items-center gap-2 py-1 text-[10px] uppercase tracking-wide"
      data-testid="dialog-divider"
    >
      <span className="orca-bg-border h-px flex-1" />
      <span>
        ── dialog{turns} ──
      </span>
      <span className="orca-bg-border h-px flex-1" />
    </div>
  );
}

export function StepMarker({ event }: { event: WebEvent }) {
  const reason = String(event.data?.step_reason ?? "step");
  return (
    <div
      className="orca-text-faint py-0.5 text-[10px] italic"
      data-testid="step-marker"
    >
      · {reason}
    </div>
  );
}
