// components/conversation/CustomRow.tsx —— custom 事件渲染（SPEC §5.3 / D3）。
//
//   - custom(kind=chart) → 紧凑 ``📊 <title>`` 引用行（点击 → 切到 Charts 页签；D3）
//   - custom 非 chart → dim ``◆ custom(<kind>)`` 可展开看 raw

import { useState } from "react";
import type { WebEvent } from "@/types/events";
import { safeJson } from "./_shared";

interface CustomRowProps {
  event: WebEvent;
  /** 点击 chart 引用行的回调（Chunk C 实现 tab 切换；本 chunk 留 hook）。 */
  onChartClick?: () => void;
}

export function CustomRow({ event, onChartClick }: CustomRowProps) {
  const d = event.data ?? {};
  if (d.kind === "chart") {
    const chart = (d.chart as Record<string, unknown> | undefined) ?? {};
    const title = typeof chart.title === "string" ? chart.title : "chart";
    return (
      <button
        type="button"
        onClick={onChartClick}
        className="flex items-center gap-1.5 px-1 py-0.5 text-xs text-slate-600 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-400 hover:underline"
        data-testid="chart-ref-row"
      >
        <span>📊</span>
        <span className="truncate">{title}</span>
      </button>
    );
  }

  // 非 chart：dim 可展开
  const [open, setOpen] = useState(false);
  const kind = typeof d.kind === "string" ? d.kind : "unknown";
  return (
    <div data-testid="custom-generic-row">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 px-1 py-0.5 text-[11px] text-slate-400 dark:text-slate-500 hover:text-slate-600 dark:hover:text-slate-300"
        aria-expanded={open}
      >
        <span className="shrink-0">{open ? "▼" : "▸"}</span>
        <span className="shrink-0">◆</span>
        <span className="font-mono">custom({kind})</span>
      </button>
      {open && (
        <pre className="mt-1 ml-4 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-slate-100 p-2 text-[11px] text-slate-600 dark:bg-slate-800/60 dark:text-slate-300">
          {safeJson(d)}
        </pre>
      )}
    </div>
  );
}
