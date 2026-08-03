// components/runlist/RunRow.tsx —— 单行（SPEC §4/§6.6）。
//
// **DOM 重构**（D1 M5）：checkbox 不嵌 button（旧实现整个行是 button，点 checkbox 跳详情页 bug）。
// 结构：``<li role=row>`` 内：左 checkbox（独立 input，stopPropagation）+ 主体 button（点 → 详情页）
// + 操作组（打开/删除，size=16，命中区≥32px，常显）。
//
// 视觉契约（§6.6/§2.6）：
//   - 状态竖条 ``STATUS_BAR_HEX[rs]`` inline（行内 hex 仅限 STATUS_BAR_HEX）。
//   - 待决策 ring：``ring-1 ring-inset ring-orca-skipped/30``。
//   - 删除按钮：size=16 p-1.5，三级 opacity（默认 faint/0.55 → 行 hover faint → 自身 hover failed），
//     **常显**（无 opacity-0 group-hover），命中区 ``min-w-[32px] min-h-[32px]``。
//   - checkbox：opacity-40 → 行 hover/selected → 100%（SPEC §5.5）。

import {
  Activity,
  Coins,
  ExternalLink,
  Timer,
  Trash2,
} from "lucide-react";
import type { RunSummary } from "@/stores/run-list-store";
import {
  STATUS_BAR_HEX,
  StatusBadge,
  statusToRunStatus,
} from "@/components/layout/status-badge";
import { fmtAgo, fmtCost, fmtElapsed, highlightMatch } from "./format-helpers";

interface Props {
  run: RunSummary;
  q: string;
  selected: boolean;
  onToggleSelect: (shiftKey: boolean) => void;
  onOpen: () => void;
  onDelete: () => void;
}

export function RunRow({
  run,
  q,
  selected,
  onToggleSelect,
  onOpen,
  onDelete,
}: Props) {
  const rs = statusToRunStatus(run.status);
  const isBlocked = rs === "blocked";
  const qLower = q.trim().toLowerCase();

  return (
    <li
      role="row"
      data-testid="run-row"
      className={`group relative flex items-center gap-3 rounded border orca-border orca-bg-surface px-3 py-2 pl-4 shadow-sm transition-opacity duration-200 hover:orca-bg-surface-2 ${
        isBlocked ? "ring-1 ring-inset ring-orca-skipped/30" : ""
      }`}
    >
      {/* 状态竖条（行内 hex 来自 STATUS_BAR_HEX，§1.2 约定允许） */}
      <div
        className="absolute inset-y-0 left-0 w-0.5 rounded-l"
        style={{ backgroundColor: STATUS_BAR_HEX[rs] }}
      />
      {/* checkbox 独立 input（不嵌 button，D1 M5） */}
      <input
        type="checkbox"
        data-testid="run-checkbox"
        aria-label={`选择 ${run.run_id.slice(0, 8)}`}
        checked={selected}
        onChange={(e) => onToggleSelect((e.nativeEvent as MouseEvent).shiftKey)}
        onClick={(e) => e.stopPropagation()}
        className={`h-4 w-4 shrink-0 transition-opacity ${
          selected ? "opacity-100" : "opacity-40 group-hover:opacity-100"
        }`}
      />
      {/* 主体可点区（点 → 详情页），checkbox 在其外 */}
      <button
        type="button"
        onClick={onOpen}
        data-testid="run-item"
        className="flex min-w-0 flex-1 items-center gap-3 text-left"
      >
        <span className="w-24 shrink-0">
          <StatusBadge status={rs} />
        </span>
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="flex items-center gap-2">
            <span className="truncate text-sm font-medium orca-text">
              {highlightMatch(run.workflow_name, qLower)}
            </span>
          </span>
          <span className="flex items-center gap-2 font-mono text-xs orca-text-muted">
            <span>{run.run_id.slice(0, 18)}…</span>
            <span className="orca-text-faint">·</span>
            <span>{fmtAgo(run.started_at)}</span>
          </span>
        </span>
        <span className="hidden shrink-0 flex-wrap items-center gap-x-4 gap-y-1 text-xs orca-text-muted md:flex">
          <Metric icon={Activity} value={run.progress ?? "?"} label="进度" />
          <Metric icon={Coins} value={fmtCost(run.cost)} label="花费" />
          <Metric icon={Timer} value={fmtElapsed(run.elapsed)} label="耗时" />
          <Metric
            icon={Activity}
            value={String(run.event_count ?? 0)}
            label="事件数"
          />
        </span>
      </button>
      {/* 操作组：打开 + 删除（size=16，命中区≥32px，常显） */}
      <span className="flex shrink-0 items-center gap-1">
        <button
          type="button"
          data-testid="open-btn"
          onClick={(e) => {
            e.stopPropagation();
            onOpen();
          }}
          title="打开"
          aria-label="打开 run"
          className="orca-text-faint hover:orca-accent inline-flex min-h-[32px] min-w-[32px] items-center justify-center"
        >
          <ExternalLink size={16} strokeWidth={1.5} aria-hidden />
        </button>
        <button
          type="button"
          data-testid="delete-btn"
          onClick={(e) => {
            e.stopPropagation();
            onDelete();
          }}
          title="删除"
          aria-label="删除 run"
          className="inline-flex min-h-[32px] min-w-[32px] items-center justify-center rounded text-[rgb(var(--text-faint)/0.55)] transition-colors hover:bg-orca-failed/10 hover:text-orca-failed group-hover:orca-text-faint"
        >
          <Trash2 size={16} strokeWidth={1.5} aria-hidden />
        </button>
      </span>
    </li>
  );
}

function Metric({
  icon: Icon,
  value,
  label,
}: {
  icon: typeof Coins;
  value: string;
  label: string;
}) {
  return (
    <span className="inline-flex items-center gap-1 orca-text-muted" title={label}>
      <Icon size={12} strokeWidth={1.5} aria-hidden />
      <span className="tabular-nums">{value}</span>
    </span>
  );
}
