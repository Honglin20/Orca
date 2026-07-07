// components/conversation/StatusLine.tsx —— dim 状态行（SPEC §5.3）。
//
// retry_* / interrupt_* / validator_* / wait_* / foreach_* → dim 单行摘要，**默认折叠**
// （SPEC §5.3 折叠规则：「默认折叠：retry/interrupt/validator/wait 状态行」）。
//
// D7（Chunk B 偏离修正）：Chunk B 把 StatusLine 做成单行不可折叠（YAGNI 偏离）。SPEC
// §5.3 明确这些状态行默认折叠 → 用户可展开看完整 data。本 Chunk 修正为可折叠（保持
// 默认折叠 + dim 一行摘要 + ▸/▼ chevron + 展开看 JSON 详情）。validator_failed 是例外：
// 失败信息用户高敏感 → **默认展开**（SPEC §5.3 闭 review #29 错误转录精神）。

import { useState } from "react";
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

/** 失败类状态行（validator_failed）：默认展开，凸显错误。 */
const DEFAULT_OPEN_TYPES = new Set<WebEvent["type"]>(["validator_failed"]);

export function StatusLine({ event }: { event: WebEvent }) {
  const icon = ICONS[event.type] ?? "·";
  // SPEC §5.3 默认折叠；validator_failed 例外（错误信息高敏感，默认展开）。
  const [open, setOpen] = useState(DEFAULT_OPEN_TYPES.has(event.type));
  const isExpandable = event.data != null && Object.keys(event.data ?? {}).length > 0;

  return (
    <div
      className="px-1 py-0.5 text-[11px] text-slate-400 dark:text-slate-500"
      data-testid="status-line"
    >
      <div className="flex items-center gap-1.5">
        <span className="shrink-0">{icon}</span>
        <span className="truncate flex-1">{summarize(event)}</span>
        {isExpandable && (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            aria-label={open ? "折叠状态详情" : "展开状态详情"}
            data-testid="status-line-toggle"
            className="shrink-0 rounded px-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-700/40"
          >
            {open ? "▼" : "▸"}
          </button>
        )}
      </div>
      {open && isExpandable && (
        <pre
          className="mt-0.5 ml-5 max-h-32 overflow-auto rounded bg-slate-100 p-1 font-mono text-[10px] text-slate-600 dark:bg-slate-800/60 dark:text-slate-300"
          data-testid="status-line-detail"
        >
          {JSON.stringify(event.data ?? {}, null, 2)}
        </pre>
      )}
    </div>
  );
}
