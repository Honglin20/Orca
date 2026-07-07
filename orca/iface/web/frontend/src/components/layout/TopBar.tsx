// components/layout/TopBar.tsx —— 顶栏：run + status + elapsed + cost（SPEC §5.1）。
//
// 纯渲染（铁律 6）：所有值 = store fold 派生。
// - status icon：●running / ✓completed / ✗failed / cancelled / blocked
// - elapsed（D5）：running 时 wall-clock tick；完成 snap workflow_completed.data.elapsed
// - cost：累计 cost_usd（agent_usage fold）
//
// Chunk A：elapsed 用 placeholder（live tick 留给后续 chunk 的 useElapsedTick）。

import { useWorkflowStore } from "@/stores/workflow-store";

const STATUS_ICON: Record<string, string> = {
  idle: "○",
  queued: "○",
  running: "●",
  completed: "✓",
  failed: "✗",
  cancelled: "⊘",
  blocked: "⏸",
};

export function TopBar({ runId }: { runId?: string }) {
  const status = useWorkflowStore((s) => s.status);
  const workflowName = useWorkflowStore((s) => s.workflowName);
  const cost = useWorkflowStore((s) => s.cost);
  const workflowElapsed = useWorkflowStore((s) => s.workflowElapsed);

  return (
    <header
      className="flex h-12 items-center gap-6 border-b border-slate-200 bg-white px-4"
      data-testid="top-bar"
    >
      <span className="text-lg font-semibold text-slate-900">Orca</span>
      <span className="font-mono text-sm text-slate-700">
        {runId ? runId.slice(0, 8) : "—"}
      </span>
      {workflowName && (
        <span className="text-sm text-slate-500">{workflowName}</span>
      )}
      <span
        className={`text-sm ${
          status === "failed"
            ? "text-red-600"
            : status === "running"
              ? "text-emerald-600"
              : "text-slate-600"
        }`}
        data-testid="top-status"
      >
        {STATUS_ICON[status] ?? "?"} {status}
      </span>
      <span className="text-sm text-slate-500" data-testid="top-elapsed">
        ⏱ {workflowElapsed !== null ? `${workflowElapsed.toFixed(1)}s` : "—"}
      </span>
      <span className="text-sm text-slate-500" data-testid="top-cost">
        🪙 ${cost.toFixed(4)}
      </span>
    </header>
  );
}
