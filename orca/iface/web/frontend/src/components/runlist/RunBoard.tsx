// components/runlist/RunBoard.tsx —— 看板根（SPEC web-board-cardgrid §2.3）。
//
// 契约（重构：横向列 → section 垂直堆叠）：
//   - section = 当前 dim 的桶（``groupRuns(sorted, dim)``），垂直堆叠 ``space-y-5``。
//   - **无横向滚动**（去除旧横向列布局的滚动容器，SPEC AC-B1）。
//   - section 内卡片按用户 sort field 排序（同列表 §3.3，复用 sortRuns DRY；输入已全局 stable 排序）。
//   - status dim：运行中→排队→待决策→失败→已完成（§4.1）；运行中/待决策 section 强调；
//     含 blocked run 的 section ring（§2.3 I9，不限 dim）。
//   - 空桶隐藏（``showEmpty=false``）由调用方 filter 桶后传入；``showEmpty=true`` 时 section 渲染占位。
//
// 共享契约（§10.4）：与列表共用 store / selection ``Set<run_id>`` / sort / search / status /
// theme / refresh / WS / dim / showEmpty；section 折叠与列表段折叠共享 ``"dim:key"``（§2.3 I10）。

import { useMemo } from "react";
import type { RunSummary } from "@/stores/run-list-store";
import { CardGridSection } from "./CardGridSection";
import {
  groupRuns,
  bucketHasBlocked,
  EMPHASIS_STATUSES,
} from "./group-runs";
import type { GroupBy } from "@/hooks/use-group-by";

interface Props {
  /** 调用方应**已全局排序**（SPEC §3.3：先排序再分桶；section 内顺序 = 输入顺序，stable）。 */
  runs: RunSummary[];
  /** 当前分组维度（两视图共用，§10.8）。 */
  dim: GroupBy;
  /** 空桶显隐（两视图共用，§10.9）：false → 0-run 桶不渲染 section。 */
  showEmpty: boolean;
  selectedIds: Set<string>;
  /** 切换 run 选择（Shift 范围选用）；orderedIds 由本组件按当前 section 收窄（M-4，与 ProjectGroup 一致）。 */
  onToggleRun: (id: string, shiftKey: boolean, orderedIds: string[]) => void;
  onOpenRun: (id: string) => void;
  onDeleteRun: (id: string) => void;
  /** section 折叠态查询（父级 RunListPage.isBucketOpen，含搜索/过滤穿透逻辑）。 */
  isBucketOpen: (bucketKey: string) => boolean;
  onToggleBucket: (bucketKey: string) => void;
  /** q 非空或 status 过滤激活 → 强制展开 + 放开限显（SPEC §2.3）。 */
  forceExpandAll: boolean;
  /** 搜索态（q 非空时显命中数）。 */
  q: string;
  searching: boolean;
}

export function RunBoard({
  runs,
  dim,
  showEmpty,
  selectedIds,
  onToggleRun,
  onOpenRun,
  onDeleteRun,
  isBucketOpen,
  onToggleBucket,
  forceExpandAll,
  q,
  searching,
}: Props) {
  // 分桶（输入已由调用方全局 stable 排序，SPEC §3.3）。
  const buckets = useMemo(() => groupRuns(runs, dim), [runs, dim]);
  // 空桶隐藏（§10.9/AC-25）：showEmpty=false → 过滤 0-run 桶。
  const visible = useMemo(
    () => buckets.filter((b) => showEmpty || b.runs.length > 0),
    [buckets, showEmpty],
  );

  return (
    <div
      data-testid="board"
      className="space-y-5"
      role="list"
      aria-label="按维度分组的 run 看板"
    >
      {visible.map((b) => {
        const status = b.status;
        const emphasize = !!status && EMPHASIS_STATUSES.has(status);
        const hasBlocked = bucketHasBlocked(b);
        // M-4：Shift 范围选 orderedIds 收窄到当前 section（桶），anchor 跨 section 时退化普通点。
        const bucketIds = b.runs.map((r) => r.run_id);
        return (
          <CardGridSection
            key={`${dim}:${b.key}`}
            bucketKey={b.key}
            status={status}
            label={b.label}
            emphasize={emphasize}
            hasBlocked={hasBlocked}
            runs={b.runs}
            open={isBucketOpen(b.key)}
            onToggleOpen={() => onToggleBucket(b.key)}
            forceExpandAll={forceExpandAll}
            searchHitCount={searching ? b.runs.length : undefined}
            q={q}
            selectedIds={selectedIds}
            onToggleRun={(id, shiftKey) => onToggleRun(id, shiftKey, bucketIds)}
            onOpenRun={onOpenRun}
            onDeleteRun={onDeleteRun}
          />
        );
      })}
    </div>
  );
}
