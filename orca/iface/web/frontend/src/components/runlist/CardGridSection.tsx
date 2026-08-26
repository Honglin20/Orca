// components/runlist/CardGridSection.tsx —— 分组 section + 卡片网格（SPEC web-board-cardgrid §2.3）。
//
// 替代旧 ``BoardColumn``（横向列 → section 垂直堆叠的一个 section）。
//
// 契约：
//   - **section 头**（``h-9``，可点折叠）：左色条 + label + 计数 + 右侧「收起/展开」。
//     左色条优先级：hasBlocked（紫穿透）> status dim emphasize（STATUS_BAR_HEX[status]）> 默认 accent/0.4。
//     status dim 强调列（running/blocked）label 用状态色（STATUS_TEXT，DRY 单出口）。
//   - **待决策 section 高亮（I9）**：``bucketHasBlocked`` 时 section 根加 ``ring-1 ring-orca-skipped/20``，
//     无论 showEmpty。
//   - **卡片网格**（展开态）：``grid gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4``。
//   - **限显 + 折叠**：默认显前 ``SECTION_LIMIT=6``；超出显「▾ 展开剩余 X」（本会话 ``expanded`` state）；
//     section 头折叠整个 section（``open`` 由父级控制，持久走 ``use-collapsed-buckets``）。
//   - **搜索/过滤穿透**：``forceExpandAll``（q 非空或 status 过滤激活）→ 显全部匹配 + 放开限显。
//   - 空桶（``showEmpty=true``）：显占位「暂无」。
//
// data-testid：``card-section-<bucketKey>``；``card-section-header-<bucketKey>``；
//   ``card-section-more-<bucketKey>``。

import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { RunSummary } from "@/stores/run-list-store";
import {
  STATUS_BAR_HEX,
  STATUS_TEXT,
  type RunStatus,
} from "@/components/layout/status-badge";
import { BoardCard } from "./BoardCard";

/** 每 section 默认显前 N 张卡（SPEC §2.3/§4.1，统一所有 section，替代旧 LIMITED_STATUSES）。 */
export const SECTION_LIMIT = 6;

interface Props {
  /** testid 用（status dim = "running" 等；其它 dim = 桶 key）。 */
  bucketKey: string;
  /** 仅 status dim 提供（emphasize 色条 + label 状态色用）；其它 dim 缺省。 */
  status?: RunStatus;
  label: string;
  /** 强调（status dim 的 running/blocked）：色条 + label 状态色 */
  emphasize: boolean;
  /** 含 blocked run → 紫色条穿透提示 + section ring（不限 dim，§2.3 I9）。 */
  hasBlocked: boolean;
  runs: RunSummary[];
  /** section 折叠态（父级控制，持久走 use-collapsed-buckets）。 */
  open: boolean;
  onToggleOpen: () => void;
  /** q 非空或 status 过滤激活时传入 → 强制展开 + 放开限显（SPEC §2.3）。 */
  forceExpandAll: boolean;
  /** 搜索命中数（q 非空时显「搜索：X · 命中 N」）。 */
  searchHitCount?: number;
  q: string;
  selectedIds: Set<string>;
  onToggleRun: (id: string, shiftKey: boolean) => void;
  onOpenRun: (id: string) => void;
  onDeleteRun: (id: string) => void;
}

export function CardGridSection({
  bucketKey,
  status,
  label,
  emphasize,
  hasBlocked,
  runs,
  open,
  onToggleOpen,
  forceExpandAll,
  searchHitCount,
  q,
  selectedIds,
  onToggleRun,
  onOpenRun,
  onDeleteRun,
}: Props) {
  const total = runs.length;
  // 展开剩余：本会话记忆（SPEC §2.3，不持久）。
  const [expanded, setExpanded] = useState(false);
  // 当 section 内 run 集合缩小到 limit 以内时，自动收回展开态（避免 expanded=true 但没东西可显）。
  useEffect(() => {
    if (expanded && total <= SECTION_LIMIT) setExpanded(false);
  }, [expanded, total]);

  const empty = total === 0;

  // 左色条规则（迁移自 BoardColumn，review A-M2）：
  //   hasBlocked（紫穿透）> emphasize（STATUS_BAR_HEX[status]）> 默认 accent/0.4。
  const barColor = hasBlocked
    ? STATUS_BAR_HEX.blocked
    : emphasize && status
      ? STATUS_BAR_HEX[status]
      : "rgb(var(--accent)/0.4)";
  // status dim 强调列 label 状态色（STATUS_TEXT DRY 单出口，替代旧 COLUMN_EMPHASIS_TEXT）。
  const labelTextCls = emphasize && status ? STATUS_TEXT[status] : "orca-text";

  // 限显：forceExpandAll（搜索/过滤）→ 显全部；否则默认 6 + expanded 放开。
  const limit = forceExpandAll || expanded ? Infinity : SECTION_LIMIT;
  const visible = runs.slice(0, limit);
  const hiddenCount = total - visible.length;

  return (
    <section
      data-testid={`card-section-${bucketKey}`}
      className={`relative rounded-md ${hasBlocked ? "ring-1 ring-orca-skipped/20" : ""}`}
    >
      {/* section 头（h-9，可点折叠）。
          forceExpandAll（搜索/过滤态）时折叠被禁止——isBucketOpen 忽略 collapsed，
          此处 onToggleOpen 无效。显式禁用点击 + 改文案，避免用户操作被静默吞掉（fail-loud）。 */}
      <div
        data-testid={`card-section-header-${bucketKey}`}
        role="button"
        tabIndex={forceExpandAll ? -1 : 0}
        aria-disabled={forceExpandAll}
        onClick={forceExpandAll ? undefined : onToggleOpen}
        onKeyDown={
          forceExpandAll
            ? undefined
            : (e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onToggleOpen();
                }
              }
        }
        className={`orca-text flex h-9 items-center gap-2 px-1 outline-none ${
          forceExpandAll ? "cursor-default" : "cursor-pointer"
        }`}
      >
        {/* 左色条（3px） */}
        <div
          className="h-5 w-[3px] shrink-0 rounded-full"
          style={{ backgroundColor: barColor }}
        />
        <span className={`text-sm font-semibold ${labelTextCls}`}>{label}</span>
        <span className="orca-text-muted text-xs tabular-nums">{total}</span>
        {/* 搜索命中数（搜索穿透） */}
        {q && searchHitCount !== undefined && (
          <span className="orca-text-muted ml-2 shrink-0 text-xs">
            搜索：{q} · 命中 {searchHitCount}
          </span>
        )}
        {/* 右侧收起/展开 + chevron。
            forceExpandAll 时显「过滤中」代替「收起」（折叠被禁止）。 */}
        <span className="orca-text-faint ml-auto flex shrink-0 items-center gap-1 text-xs">
          {forceExpandAll ? (
            <span>过滤中</span>
          ) : (
            <>
              <span>{open ? "收起" : "展开"}</span>
              {open ? (
                <ChevronDown size={14} strokeWidth={1.5} aria-hidden />
              ) : (
                <ChevronRight size={14} strokeWidth={1.5} aria-hidden />
              )}
            </>
          )}
        </span>
      </div>
      {/* 卡片网格（展开态） */}
      {open && (
        <>
          <div className="grid gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
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
          </div>
          {empty && (
            <div className="orca-text-faint flex h-24 items-center justify-center text-xs">
              暂无
            </div>
          )}
          {/* 展开剩余（限显放开后不显） */}
          {hiddenCount > 0 && (
            <button
              type="button"
              data-testid={`card-section-more-${bucketKey}`}
              onClick={() => setExpanded(true)}
              className="orca-text-muted hover:orca-text mt-3 rounded border border-dashed orca-border px-2 py-1 text-xs"
            >
              ▾ 展开剩余 {hiddenCount}
            </button>
          )}
        </>
      )}
    </section>
  );
}
