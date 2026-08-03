// components/runlist/BoardColumn.tsx —— 看板单列（SPEC §10.2）。
//
// 视觉契约（§10.2 + §2 速查表）：
//   - 列容器：``min-w-[260px] flex-1 rounded-md bg-[rgb(var(--surface-2)/0.2)] p-2``。
//   - 列头：状态 dot + label + 计数（``text-sm font-semibold``）。
//   - **运行中/待决策列**：左侧 3px 色条（STATUS_BAR_HEX）+ 列头加粗 + 计数用状态色。
//   - **待决策列计数>0**：整列 ring（``ring-1 ring-orca-skipped/20``）。
//   - 已完成/失败列：仅显最近 N=10 + 「显示更多（共 X）」展开（本会话记忆）。
//   - 空列：居中 faint 占位「暂无」。
//
// data-testid：``board-column-<status>``。

import { useEffect, useState } from "react";
import type { RunSummary } from "@/stores/run-list-store";
import {
  STATUS_BAR_HEX,
  STATUS_DOT_BG,
  type RunStatus,
} from "@/components/layout/status-badge";
import { BoardCard } from "./BoardCard";

/**
 * 列头强调态文字色（完整 class string，让 Tailwind JIT 扫到；禁模板拼接）。
 * 与 status-badge.tsx STATUS_TEXT 同色源，但本组件需要独立 map——避免 export 私有常量
 * 破坏 status-badge 封装。颜色集合是 orca palette 的稳定子集。
 */
const COLUMN_EMPHASIS_TEXT: Record<RunStatus, string> = {
  running: "text-orca-running",
  completed: "text-orca-done",
  failed: "text-orca-failed",
  cancelled: "text-orca-pending",
  blocked: "text-orca-skipped",
  queued: "text-orca-pending",
  "live-pending": "text-orca-pending",
};

interface Props {
  status: RunStatus;
  label: string;
  /** 强调列（运行中/待决策）：色条 + 列头加粗 + 计数状态色 */
  emphasize: boolean;
  /** 待决策列：计数>0 时整列 ring */
  ringWhenNonEmpty: boolean;
  runs: RunSummary[];
  selectedIds: Set<string>;
  deletingIds: Set<string>;
  onToggleRun: (id: string, shiftKey: boolean) => void;
  onOpenRun: (id: string) => void;
  onDeleteRun: (id: string) => void;
  /** 已完成/失败列的限长（其它列传 Infinity） */
  initialLimit?: number;
}

export function BoardColumn({
  status,
  label,
  emphasize,
  ringWhenNonEmpty,
  runs,
  selectedIds,
  deletingIds,
  onToggleRun,
  onOpenRun,
  onDeleteRun,
  initialLimit = Infinity,
}: Props) {
  const total = runs.length;
  // 显示更多：展开态本会话记忆（SPEC §10.2/AC-22）。
  const [expanded, setExpanded] = useState(false);
  // 当列内 run 集合缩小到 limit 以内时，自动收回展开态（避免 expanded=true 但没东西可显）。
  useEffect(() => {
    if (expanded && total <= initialLimit) setExpanded(false);
  }, [expanded, total, initialLimit]);

  const visible = expanded ? runs : runs.slice(0, initialLimit);
  const hiddenCount = total - visible.length;
  const empty = total === 0;

  return (
    <section
      data-testid={`board-column-${status}`}
      className={`relative flex min-w-[260px] flex-1 flex-col rounded-md bg-[rgb(var(--surface-2)/0.2)] p-2 ${
        ringWhenNonEmpty && !empty ? "ring-1 ring-orca-skipped/20" : ""
      }`}
    >
      {/* 运行中/待决策列：左侧 3px 色条 */}
      {emphasize && !empty && (
        <div
          className="absolute inset-y-0 left-0 w-[3px] rounded-l"
          style={{ backgroundColor: STATUS_BAR_HEX[status] }}
        />
      )}
      {/* 列头：dot + label + 计数 */}
      <div className="mb-2 flex items-center gap-2 px-1 py-1">
        <span
          className={`h-1.5 w-1.5 rounded-full ${STATUS_DOT_BG[status]} ${
            status === "running" ? "animate-pulse" : ""
          }`}
          aria-hidden
        />
        <span
          className={`text-sm ${emphasize ? "font-bold" : "font-semibold"} ${
            emphasize ? COLUMN_EMPHASIS_TEXT[status] : "orca-text"
          }`}
        >
          {label}
        </span>
        <span
          className={`text-xs tabular-nums ${
            emphasize ? COLUMN_EMPHASIS_TEXT[status] : "orca-text-muted"
          }`}
        >
          {total}
        </span>
      </div>
      {/* 列内卡片 */}
      <div className="flex-1 space-y-2">
        {!empty &&
          visible.map((r) => (
            <BoardCard
              key={r.run_id}
              run={r}
              selected={selectedIds.has(r.run_id)}
              deleting={deletingIds.has(r.run_id)}
              onToggleSelect={(shiftKey) => onToggleRun(r.run_id, shiftKey)}
              onOpen={() => onOpenRun(r.run_id)}
              onDelete={() => onDeleteRun(r.run_id)}
            />
          ))}
        {empty && (
          <div className="orca-text-faint flex h-24 items-center justify-center text-xs">
            暂无
          </div>
        )}
      </div>
      {/* 已完成/失败列：显示更多 */}
      {hiddenCount > 0 && !expanded && (
        <button
          type="button"
          data-testid={`board-column-more-${status}`}
          onClick={() => setExpanded(true)}
          className="orca-text-muted hover:orca-text mt-2 rounded border border-dashed orca-border px-2 py-1 text-xs"
        >
          显示更多（共 {total}）
        </button>
      )}
    </section>
  );
}
