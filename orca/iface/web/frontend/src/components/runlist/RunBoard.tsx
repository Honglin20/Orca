// components/runlist/RunBoard.tsx —— 看板根（SPEC §10.2/§10.4/§10.8）。
//
// 契约（§10.8 泛化后）：
//   - 列 = 当前 dim 的桶（``groupRuns(sorted, dim)``），不再是硬编码 5 状态列。
//   - 列容器水平排布 ``flex gap-3 overflow-x-auto``。
//   - 列内卡片按用户 sort field 排序（同列表 §3.3，复用 sortRuns DRY）。
//   - status dim：排队→运行中→待决策→已完成→失败；运行中/待决策列强调；待决策列>0 ring；
//     已完成/失败列限长 10 + 显示更多。
//   - 其它 dim：含 blocked run 的列紫条穿透提示（不限 dim，§10.8）。
//   - 空列隐藏（``showEmpty=false``）由调用方 filter 桶后传入；``showEmpty=true`` 时本组件渲染占位。
//
// 共享契约（§10.4）：与列表共用 store / selection ``Set<run_id>`` / sort / search / status chips /
// theme / refresh / WS / dim / showEmpty（§10.8/§10.9）。

import { useMemo } from "react";
import type { RunSummary } from "@/stores/run-list-store";
import { BoardColumn } from "./BoardColumn";
import {
  groupRuns,
  bucketHasBlocked,
  EMPHASIS_STATUSES,
  RING_STATUSES,
  LIMITED_STATUSES,
  COMPLETED_LIMIT,
} from "./group-runs";
import type { GroupBy } from "@/hooks/use-group-by";

interface Props {
  /** 调用方应**已全局排序**（SPEC §3.3：先排序再分桶；列内顺序 = 输入顺序，stable）。
   *  本组件不再二次排序（code-reviewer MAJOR-3：去冗余 sortRuns）。 */
  runs: RunSummary[];
  /** 当前分组维度（两视图共用，§10.8）。 */
  dim: GroupBy;
  /** 空桶显隐（两视图共用，§10.9）：false → 0-run 桶不渲染列。 */
  showEmpty: boolean;
  selectedIds: Set<string>;
  /** 切换 run 选择（Shift 范围选用）；orderedIds 由本组件按当前桶收窄（M-4，与 ProjectGroup 一致）。 */
  onToggleRun: (id: string, shiftKey: boolean, orderedIds: string[]) => void;
  onOpenRun: (id: string) => void;
  onDeleteRun: (id: string) => void;
}

export function RunBoard({
  runs,
  dim,
  showEmpty,
  selectedIds,
  onToggleRun,
  onOpenRun,
  onDeleteRun,
}: Props) {
  // 分桶（输入已由调用方全局 stable 排序，SPEC §3.3；不再二次排序——code-reviewer MAJOR-3）。
  const buckets = useMemo(() => groupRuns(runs, dim), [runs, dim]);
  // 空桶隐藏（§10.9/AC-25）：showEmpty=false → 过滤 0-run 桶。none 维度单桶永不为空（有 run 时）。
  const visible = useMemo(
    () => buckets.filter((b) => showEmpty || b.runs.length > 0),
    [buckets, showEmpty],
  );

  return (
    <div
      data-testid="board"
      className="flex gap-3 overflow-x-auto pb-2"
      role="list"
      aria-label="按维度分列的 run 看板"
    >
      {visible.map((b) => {
        const status = b.status;
        const emphasize = !!status && EMPHASIS_STATUSES.has(status);
        const ringWhenNonEmpty = !!status && RING_STATUSES.has(status);
        const initialLimit =
          status && LIMITED_STATUSES.has(status) ? COMPLETED_LIMIT : Infinity;
        const hasBlocked = bucketHasBlocked(b);
        // M-4：Shift 范围选 orderedIds 收窄到当前列（桶），anchor 跨列时退化普通点。
        const bucketIds = b.runs.map((r) => r.run_id);
        return (
          <BoardColumn
            key={`${dim}:${b.key}`}
            columnKey={b.key}
            status={status}
            label={b.label}
            emphasize={emphasize}
            ringWhenNonEmpty={ringWhenNonEmpty}
            hasBlocked={hasBlocked}
            runs={b.runs}
            selectedIds={selectedIds}
            onToggleRun={(id, shiftKey) => onToggleRun(id, shiftKey, bucketIds)}
            onOpenRun={onOpenRun}
            onDeleteRun={onDeleteRun}
            initialLimit={initialLimit}
          />
        );
      })}
    </div>
  );
}
