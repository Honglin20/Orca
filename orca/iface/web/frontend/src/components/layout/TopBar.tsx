// components/layout/TopBar.tsx —— 顶栏：run + status + elapsed（SPEC §5.1 / §0 D5 / §P5）。
//
// 纯渲染（铁律 6）：所有值 = store fold 派生 + selector 派生。
// - status icon：``<StatusIcon/>``（lucide，P1 替换原 emoji ●/✓/✗/⊘/⏸）；配色继承父级 currentColor
// - elapsed（D5）：running 时 wall-clock tick（``useElapsedNow`` 单一共享 tick，页根
//   控制 active）；完成 snap ``workflow_completed.data.elapsed`` 停 tick（防 wall-clock
//   成前端真相，铁律 1）。``selectWorkflowElapsed`` 实现这一语义。
//
// **P5a**：去掉 cost span（``🪙 $X``）+ ``top-cost`` testid。``store.cost`` 保留（fold
// 仍在，不破坏幂等 / agent_usage 累加测试），只是不再在 UI 显示——计费视觉移除属用户决策。
//
// **P5b**：配色迁移到 design token（``orca-*`` utility class 读 CSS var，明暗自适应）。
// brand「Orca」用 ``orca-accent``（钢蓝 = PALETTE[0]），是 active/品牌强调色统一入口。
//
// 单一 timer 约束（SPEC §5.2）：本组件用 ``useElapsedNow()`` 订阅模块级 singleton
// tick，自身不开 setInterval。tick 启停由 RunDetailPage 页根的 ``useElapsedTickActive`` 控制。

import { Timer } from "lucide-react";
import { useWorkflowStore } from "@/stores/workflow-store";
import { useElapsedNow } from "@/hooks/use-elapsed-tick";
import { selectWorkflowElapsed, formatElapsed } from "@/selectors";
import { StatusIcon } from "@/components/icons";
import { statusColor } from "./status-style";

export function TopBar({ runId }: { runId?: string }) {
  const status = useWorkflowStore((s) => s.status);
  const workflowName = useWorkflowStore((s) => s.workflowName);

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
      className="orca-bg-surface orca-border orca-text flex h-12 items-center gap-6 border-b px-4"
      data-testid="top-bar"
    >
      <span className="orca-accent text-lg font-semibold">Orca</span>
      <span className="orca-text-muted font-mono text-sm">
        {runId ? runId.slice(0, 8) : "—"}
      </span>
      {workflowName && (
        <span className="orca-text-faint text-sm">{workflowName}</span>
      )}
      <span
        className={`text-sm inline-flex items-center gap-1 ${statusColor(status)}`}
        data-testid="top-status"
      >
        <StatusIcon status={status} />
        {status}
      </span>
      <span
        className="orca-text-faint text-sm inline-flex items-center gap-1"
        data-testid="top-elapsed"
      >
        <Timer size={14} strokeWidth={1.5} aria-hidden />
        {workflowElapsed !== null ? formatElapsed(workflowElapsed, "tenths") : "—"}
      </span>
    </header>
  );
}
