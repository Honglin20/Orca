// components/runlist/BoardColumn.tsx —— 看板单列（SPEC §10.2 / §10.8 含泛化）。
//
// 视觉契约（§10.2 + §10.8 + §2 速查表）：
//   - 列容器：``min-w-[260px] flex-1 rounded-md bg-[rgb(var(--surface-2)/0.2)] p-2``。
//   - 列头：状态 dot（仅 status dim）+ label + 计数（``text-sm font-semibold``）。
//   - **运行中/待决策列**（status dim）：左侧 3px 色条（STATUS_BAR_HEX[status]）+ 列头加粗 + 计数状态色。
//   - **含 blocked run 的桶**（任意 dim，§10.8 紫条穿透）：``hasBlocked`` 时左色条变 STATUS_BAR_HEX.blocked。
//   - **待决策列计数>0**（status dim blocked）：整列 ring（``ring-1 ring-orca-skipped/20``）。
//   - 已完成/失败列（status dim）：仅显最近 N=10 + 「显示更多（共 X）」展开（本会话记忆）。
//   - 空列：居中 faint 占位「暂无」（``showEmpty=true`` 时才进得到这里）。
//
// data-testid：``board-column-<columnKey>``（columnKey = 桶 key：status dim 为 "queued" 等，
// 其它 dim 为 project/workflow/time 名）。

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
 * 真相源 = status-badge.tsx 的私有 ``STATUS_TEXT``（同 orca palette）；此处独立 map 以不 export
 * 私有常量破坏 status-badge 封装。若 status-badge 配色改了，**此处需同步**（防双源漂移）。
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
  /** testid 用（status dim = "queued"/"running"/...；其它 dim = 桶 key）。 */
  columnKey: string;
  /** 仅 status dim 提供（emphasize 色条 + dot + 计数状态色用）；其它 dim 缺省。 */
  status?: RunStatus;
  label: string;
  /** 强调列（status dim 的 running/blocked）：色条 + 列头加粗 + 计数状态色 */
  emphasize: boolean;
  /** 待决策列：计数>0 时整列 ring（仅 status dim blocked） */
  ringWhenNonEmpty: boolean;
  /** 含 blocked run → 紫色条穿透提示（不限 dim，§10.8） */
  hasBlocked: boolean;
  runs: RunSummary[];
  selectedIds: Set<string>;
  onToggleRun: (id: string, shiftKey: boolean) => void;
  onOpenRun: (id: string) => void;
  onDeleteRun: (id: string) => void;
  /** 已完成/失败列的限长（其它列传 Infinity） */
  initialLimit?: number;
}

export function BoardColumn({
  columnKey,
  status,
  label,
  emphasize,
  ringWhenNonEmpty,
  hasBlocked,
  runs,
  selectedIds,
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

  // 左色条规则（review A-M2：非状态 dim 列也需锚点——看板不再退化）：
  //   - 所有非空列都显色条（showBar = !empty），消除非状态 dim 列「无锚」的视觉退化。
  //   - 颜色优先级：hasBlocked（紫穿透）> emphasize（STATUS_BAR_HEX[status]）> 默认 accent/0.4。
  //   - status dim 的 running/blocked 列（emphasize）用其状态色；非状态 dim 默认 accent/0.4。
  const showBar = !empty;
  const barColor = hasBlocked
    ? STATUS_BAR_HEX.blocked
    : emphasize && status
      ? STATUS_BAR_HEX[status]
      : "rgb(var(--accent)/0.4)";
  // 列头强调态文字色（仅 status dim emphasize）。
  const emphasisText = status ? COLUMN_EMPHASIS_TEXT[status] : "orca-text";

  return (
    <section
      data-testid={`board-column-${columnKey}`}
      className={`relative flex min-w-[260px] flex-1 flex-col rounded-md bg-[rgb(var(--surface-2)/0.45)] p-2 ${
        ringWhenNonEmpty && !empty ? "ring-1 ring-orca-skipped/20" : ""
      }`}
    >
      {/* 左侧 3px 色条：emphasize 或 hasBlocked 时显（见模块注释规则） */}
      {showBar && (
        <div
          className="absolute inset-y-0 left-0 w-[3px] rounded-l"
          style={{ backgroundColor: barColor }}
        />
      )}
      {/* 列头：dot（仅 status dim）+ label + 计数 */}
      <div className="mb-2 flex items-center gap-2 px-1 py-1">
        {status && (
          <span
            className={`h-1.5 w-1.5 rounded-full ${STATUS_DOT_BG[status]} ${
              status === "running" ? "animate-pulse" : ""
            }`}
            aria-hidden
          />
        )}
        <span
          className={`text-sm ${emphasize ? "font-bold" : "font-semibold"} ${
            emphasize ? emphasisText : "orca-text"
          }`}
        >
          {label}
        </span>
        <span
          className={`text-xs tabular-nums ${
            emphasize ? emphasisText : "orca-text-muted"
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
          data-testid={`board-column-more-${columnKey}`}
          onClick={() => setExpanded(true)}
          className="orca-text-muted hover:orca-text mt-2 rounded border border-dashed orca-border bg-[rgb(var(--surface-2)/0.5)] px-2 py-1 text-xs"
        >
          显示更多（共 {total}）
        </button>
      )}
    </section>
  );
}
