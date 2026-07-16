// components/conversation/NodeDivider.tsx —— 节点/dialog/step 细分隔符（SPEC §5.3）。
//
//   - node_started / node_skipped → dim 细分隔
//   - ``node_completed`` 已升格为 output block（B1），不再作 divider——见 NodeOutputBlock。
//   - dialog_started / dialog_ended → ``── dialog ──``
//   - 无后续 message/thinking 的 step_marker → dim ``· step`` 分隔

import type { WebEvent } from "@/types/events";

export function NodeDivider({ event }: { event: WebEvent }) {
  let label: string;
  switch (event.type) {
    case "node_started":
      label = `▶ ${event.node ?? "node"} started`;
      break;
    case "node_skipped":
      label = `⊘ ${event.node ?? "node"} skipped`;
      break;
    default:
      label = event.type;
  }
  return (
    <div
      className="flex items-center gap-2 py-1 text-[10px] uppercase tracking-wide text-slate-300 dark:text-slate-600"
      data-testid="node-divider"
    >
      <span className="h-px flex-1 bg-slate-200 dark:bg-slate-700" />
      <span>{label}</span>
      <span className="h-px flex-1 bg-slate-200 dark:bg-slate-700" />
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
      className="flex items-center gap-2 py-1 text-[10px] uppercase tracking-wide text-slate-400 dark:text-slate-500"
      data-testid="dialog-divider"
    >
      <span className="h-px flex-1 bg-slate-200 dark:bg-slate-700" />
      <span>
        ── dialog{turns} ──
      </span>
      <span className="h-px flex-1 bg-slate-200 dark:bg-slate-700" />
    </div>
  );
}

export function StepMarker({ event }: { event: WebEvent }) {
  const reason = String(event.data?.step_reason ?? "step");
  return (
    <div
      className="py-0.5 text-[10px] italic text-slate-300 dark:text-slate-600"
      data-testid="step-marker"
    >
      · {reason}
    </div>
  );
}
