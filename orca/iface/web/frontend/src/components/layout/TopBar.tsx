// components/layout/TopBar.tsx —— 顶栏：run + status + elapsed + cost（SPEC §5.1 / §0 D5）。
//
// 纯渲染（铁律 6）：所有值 = store fold 派生 + selector 派生。
// - status icon：●running / ✓completed / ✗failed / cancelled / blocked
// - elapsed（D5）：running 时 wall-clock tick（``useElapsedNow`` 单一共享 tick，页根
//   控制 active）；完成 snap ``workflow_completed.data.elapsed`` 停 tick（防 wall-clock
//   成前端真相，铁律 1）。``selectWorkflowElapsed`` 实现这一语义。
// - cost：累计 cost_usd（agent_usage fold）
//
// 单一 timer 约束（SPEC §5.2）：本组件用 ``useElapsedNow()`` 订阅模块级 singleton
// tick，自身不开 setInterval。tick 启停由 RunDetailPage 页根的 ``useElapsedTickActive`` 控制。

import { useWorkflowStore } from "@/stores/workflow-store";
import { useElapsedNow } from "@/hooks/use-elapsed-tick";
import { selectWorkflowElapsed, formatElapsed } from "@/selectors";
import type { WorkflowStatus } from "@/types/store-types";

const STATUS_ICON: Record<WorkflowStatus, string> = {
  idle: "○",
  queued: "○",
  running: "●",
  completed: "✓",
  failed: "✗",
  cancelled: "⊘",
  blocked: "⏸",
};

function statusColor(status: WorkflowStatus): string {
  if (status === "failed") return "text-red-600";
  if (status === "running") return "text-emerald-600";
  if (status === "completed") return "text-emerald-600";
  if (status === "cancelled") return "text-slate-500";
  if (status === "blocked") return "text-amber-600";
  return "text-slate-600";
}

/** 把秒数格式化为 ``Ns`` / ``Nm Ns``（DRY：用共享 formatElapsed）。 */

export function TopBar({ runId }: { runId?: string }) {
  const status = useWorkflowStore((s) => s.status);
  const workflowName = useWorkflowStore((s) => s.workflowName);
  const cost = useWorkflowStore((s) => s.cost);

  // 单一共享 tick：所有 TopBar / AgentsRail 共用。completed 时 selector snap → 停 tick
  // 后值固定（useElapsedNow 仍可调用，但页根 active=false 后不再刷新）。
  const now = useElapsedNow();
  // 注意：workflowElapsed/status/workflowStartedAt 都在同一 store 上派生；分别订阅
  // 避免无关字段变更触发不必要的 re-render。
  const workflowElapsed = useWorkflowStore((s) =>
    selectWorkflowElapsed(s, now)
  );

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
        className={`text-sm ${statusColor(status)}`}
        data-testid="top-status"
      >
        {STATUS_ICON[status]} {status}
      </span>
      <span className="text-sm text-slate-500" data-testid="top-elapsed">
        ⏱ {workflowElapsed !== null ? formatElapsed(workflowElapsed, "tenths") : "—"}
      </span>
      <span className="text-sm text-slate-500" data-testid="top-cost">
        🪙 ${cost.toFixed(4)}
      </span>
    </header>
  );
}
