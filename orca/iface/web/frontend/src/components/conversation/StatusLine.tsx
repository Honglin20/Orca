// components/conversation/StatusLine.tsx —— dim 状态行（SPEC §5.3）。
//
// retry_* / interrupt_* / validator_* / wait_* / foreach_* → dim 单行摘要，默认折叠（或
// 单行不可折叠，本版选择单行不可折叠，YAGNI——展开内容有限）。

import type { WebEvent } from "@/types/events";

const ICONS: Partial<Record<WebEvent["type"], string>> = {
  retry_started: "↻",
  retry_succeeded: "↻",
  retry_exhausted: "↻",
  interrupt_requested: "⏸",
  interrupt_resolved: "▶",
  validator_started: "✓",
  validator_passed: "✓",
  validator_failed: "✗",
  wait_started: "⏱",
  wait_completed: "⏱",
  foreach_started: "⏵",
  foreach_item_started: "·",
  foreach_item_completed: "·",
  foreach_completed: "⏵",
};

function summarize(e: WebEvent): string {
  const d = e.data ?? {};
  switch (e.type) {
    case "retry_started":
      return `retry ${num(d.attempt)}/${num(d.max_attempts)} (${str(d.kind)})`;
    case "retry_succeeded":
      return `retry succeeded (total ${num(d.attempt_total)})`;
    case "retry_exhausted":
      return `retry exhausted (${num(d.attempts)} attempts)`;
    case "interrupt_requested":
      return `interrupt requested (${str(d.source)})`;
    case "interrupt_resolved":
      return `interrupt resolved (${str(d.action)})`;
    case "validator_started":
      return "validator running";
    case "validator_passed":
      return "validator passed";
    case "validator_failed":
      return `validator FAILED${d.message ? ": " + str(d.message) : ""}`;
    case "wait_started":
      return `wait ${num(d.duration_seconds)}s (${str(d.reason)})`;
    case "wait_completed":
      return `wait done (${num(d.elapsed_seconds)}s)`;
    case "foreach_started":
      return `foreach: ${num(d.item_count)} items`;
    case "foreach_item_started":
      return `foreach item[${num(d.index)}] started`;
    case "foreach_item_completed":
      return `foreach item[${num(d.index)}] done`;
    case "foreach_completed":
      return `foreach done (${num(d.count)})`;
    default:
      return e.type;
  }
}

function str(v: unknown): string {
  return v == null ? "" : String(v);
}
function num(v: unknown): number | string {
  const n = Number(v ?? 0);
  return Number.isFinite(n) ? n : String(v ?? "?");
}

export function StatusLine({ event }: { event: WebEvent }) {
  const icon = ICONS[event.type] ?? "·";
  return (
    <div
      className="flex items-center gap-1.5 px-1 py-0.5 text-[11px] text-slate-400 dark:text-slate-500"
      data-testid="status-line"
    >
      <span className="shrink-0">{icon}</span>
      <span className="truncate">{summarize(event)}</span>
    </div>
  );
}
