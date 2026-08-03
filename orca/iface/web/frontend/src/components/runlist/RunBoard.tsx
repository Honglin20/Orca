// components/runlist/RunBoard.tsx —— 看板根（SPEC §10.2/§10.4）。
//
// 契约：
//   - 状态列左→右：``排队 | 运行中 | 待决策 | 已完成 | 失败``（与状态 chips 同语义；空列仍渲染占位）。
//   - 列容器水平排布 ``flex gap-3 overflow-x-auto``。
//   - 列内卡片按用户 sort field 排序（同列表 §3.3，复用 sortRuns DRY）。
//   - **已完成/失败列**：限长最近 10 + 「显示更多」（避免历史撑爆列；展开态本会话记忆）。
//   - 状态映射：cancelled → 失败；live-pending → 排队（SPEC 仅五列，需归桶）。
//
// 共享契约（§10.4）：与列表共用 store / selection ``Set<run_id>`` / sort / search / status chips /
// theme / refresh / WS。看板下不显 project 分组折叠（按状态分列已够；project 是卡片副标）。

import { useMemo } from "react";
import type { RunSummary } from "@/stores/run-list-store";
import { statusToRunStatus, type RunStatus } from "@/components/layout/status-badge";
import { BoardColumn } from "./BoardColumn";
import { sortRuns } from "./sort-runs";
import type { SortState } from "@/hooks/use-list-sort";

/** 看板列定义：左→右顺序 + 各列接受哪些 RunStatus。 */
interface ColumnDef {
  status: RunStatus;
  label: string;
  /** 该列接受的 RunStatus 集合（多 status 归桶，例如 cancelled → 失败列） */
  accept: Set<RunStatus>;
  emphasize: boolean;
  ringWhenNonEmpty: boolean;
  /** 限长（已完成/失败列 10）；Infinity = 不限。 */
  initialLimit: number;
}

const COMPLETED_LIMIT = 10;

const COLUMNS: ColumnDef[] = [
  {
    status: "queued",
    label: "排队",
    accept: new Set<RunStatus>(["queued", "live-pending"]),
    emphasize: false,
    ringWhenNonEmpty: false,
    initialLimit: Infinity,
  },
  {
    status: "running",
    label: "运行中",
    accept: new Set<RunStatus>(["running"]),
    emphasize: true,
    ringWhenNonEmpty: false,
    initialLimit: Infinity,
  },
  {
    status: "blocked",
    label: "待决策",
    accept: new Set<RunStatus>(["blocked"]),
    emphasize: true,
    ringWhenNonEmpty: true,
    initialLimit: Infinity,
  },
  {
    status: "completed",
    label: "已完成",
    accept: new Set<RunStatus>(["completed"]),
    emphasize: false,
    ringWhenNonEmpty: false,
    initialLimit: COMPLETED_LIMIT,
  },
  {
    status: "failed",
    label: "失败",
    accept: new Set<RunStatus>(["failed", "cancelled"]),
    emphasize: false,
    ringWhenNonEmpty: false,
    initialLimit: COMPLETED_LIMIT,
  },
];

interface Props {
  runs: RunSummary[];
  sort: SortState;
  selectedIds: Set<string>;
  deletingIds: Set<string>;
  onToggleRun: (id: string, shiftKey: boolean) => void;
  onOpenRun: (id: string) => void;
  onDeleteRun: (id: string) => void;
}

export function RunBoard({
  runs,
  sort,
  selectedIds,
  deletingIds,
  onToggleRun,
  onOpenRun,
  onDeleteRun,
}: Props) {
  // 全局排序后分桶（SPEC §3.3：先排序再分桶；列内顺序 = 全局 sort）。
  const sorted = useMemo(() => sortRuns(runs, sort), [runs, sort]);
  const buckets = useMemo(() => {
    const m = new Map<RunStatus, RunSummary[]>();
    for (const c of COLUMNS) m.set(c.status, []);
    for (const r of sorted) {
      const rs = statusToRunStatus(r.status);
      for (const c of COLUMNS) {
        if (c.accept.has(rs)) {
          m.get(c.status)!.push(r);
          break;
        }
      }
    }
    return m;
  }, [sorted]);

  return (
    <div
      data-testid="board"
      className="flex gap-3 overflow-x-auto pb-2"
      role="list"
      aria-label="按状态分列的 run 看板"
    >
      {COLUMNS.map((c) => (
        <BoardColumn
          key={c.status}
          status={c.status}
          label={c.label}
          emphasize={c.emphasize}
          ringWhenNonEmpty={c.ringWhenNonEmpty}
          runs={buckets.get(c.status) ?? []}
          selectedIds={selectedIds}
          deletingIds={deletingIds}
          onToggleRun={onToggleRun}
          onOpenRun={onOpenRun}
          onDeleteRun={onDeleteRun}
          initialLimit={c.initialLimit}
        />
      ))}
    </div>
  );
}
