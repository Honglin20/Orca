// components/runlist/BoardCard.tsx —— 看板单卡（SPEC §10.3）。
//
// 视觉契约（§10.3 + §2 速查表）：
//   - 容器：``rounded border orca-border orca-bg-surface shadow-sm p-3`` + 左侧状态竖条（STATUS_BAR_HEX）。
//   - selected → ``ring-1 ring-orca-accent/40 bg-[rgb(var(--accent)/0.06)]``。
//   - blocked → 额外 ``ring-1 ring-inset ring-orca-skipped/30``（NM1，沿用 RunRow）。
//   - 圆角 rounded；阴影 shadow-sm；字号 text-sm/xs（禁 lg/xl/2xl、禁 text-[1[013]px]）。
//
// 内容（全部来自现有 RunSummary，零新字段）：
//   - 第一行：StatusBadge + workflow_name（truncate text-sm font-medium）+ project_name（text-xs muted）。
//   - 第二行（running/queued）：进度条 progress（按字符串解析百分比；失败 indeterminate pulse）。
//   - 第二行（blocked）：⚠ 等待 <elapsed>（紫）。
//   - 第三行：cost · elapsed · event_count（text-xs muted tabular-nums）。
//
// 交互：整卡 click → onOpen；hover 右上显 delete-btn（size=16，命中区≥32px）；hover 左上显 run-checkbox；
//       卡片 selected 时 ring 强调。
//
// data-testid：根 ``board-card``；内层内容 wrapper 挂 ``run-item``（兼容 9b ——
//   ``page.click("[data-testid=run-item]")`` 命中内层，事件冒泡到根触发 onOpen）。这与 RunRow
//   「外 ``run-row`` + 内 button ``run-item``」同模式（外层卡片 + 内层 run-item 标记）。

import { Trash2, AlertTriangle } from "lucide-react";
import type { RunSummary } from "@/stores/run-list-store";
import {
  STATUS_BAR_HEX,
  StatusBadge,
  statusToRunStatus,
} from "@/components/layout/status-badge";
import { fmtCost, fmtElapsed } from "./format-helpers";

interface Props {
  run: RunSummary;
  selected: boolean;
  deleting: boolean;
  onToggleSelect: (shiftKey: boolean) => void;
  onOpen: () => void;
  onDelete: () => void;
}

/** 解析 progress 字符串为 0..1 比例。失败/空 → null（indeterminate）。 */
function parseProgress(p: string | undefined | null): number | null {
  if (!p) return null;
  const s = String(p).trim();
  if (!s) return null;
  if (s.endsWith("%")) {
    const n = Number.parseFloat(s.slice(0, -1));
    if (Number.isNaN(n)) return null;
    return Math.min(1, Math.max(0, n / 100));
  }
  const n = Number.parseFloat(s);
  if (Number.isNaN(n)) return null;
  if (n <= 1) return n; // 视为比例
  if (n <= 100) return n / 100; // 视为百分数
  return null; // >100 不合理 → indeterminate
}

export function BoardCard({
  run,
  selected,
  deleting,
  onToggleSelect,
  onOpen,
  onDelete,
}: Props) {
  const rs = statusToRunStatus(run.status);
  const isBlocked = rs === "blocked";
  const isRunning = rs === "running" || rs === "queued";
  const progress = isRunning ? parseProgress(run.progress) : null;
  return (
    <div
      data-testid="board-card"
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen();
        }
      }}
      className={`group relative cursor-pointer rounded border orca-border orca-bg-surface px-3 py-2 pl-4 text-left shadow-sm transition-opacity hover:orca-bg-surface-2 ${
        selected ? "ring-1 ring-orca-accent/40 bg-[rgb(var(--accent)/0.06)]" : ""
      } ${isBlocked ? "ring-1 ring-inset ring-orca-skipped/30" : ""} ${
        deleting ? "opacity-40" : ""
      }`}
    >
      {/* 状态竖条（行内 hex 来自 STATUS_BAR_HEX，§1.2 约定允许） */}
      <div
        className="absolute inset-y-0 left-0 w-0.5 rounded-l"
        style={{ backgroundColor: STATUS_BAR_HEX[rs] }}
      />
      {/* hover 左上：run-checkbox（与列表共享 selection） */}
      <input
        type="checkbox"
        data-testid="run-checkbox"
        aria-label={`选择 ${run.run_id.slice(0, 8)}`}
        checked={selected}
        onChange={(e) => onToggleSelect((e.nativeEvent as MouseEvent).shiftKey)}
        onClick={(e) => e.stopPropagation()}
        className={`absolute left-1.5 top-1.5 h-4 w-4 transition-opacity ${
          selected ? "opacity-100" : "opacity-0 group-hover:opacity-100"
        }`}
      />
      {/* hover 右上：删除按钮（size=16，命中区≥32px，与 RunRow 同档） */}
      <button
        type="button"
        data-testid="delete-btn"
        onClick={(e) => {
          e.stopPropagation();
          onDelete();
        }}
        title="删除"
        aria-label="删除 run"
        className="absolute right-1.5 top-1.5 inline-flex min-h-[32px] min-w-[32px] items-center justify-center rounded text-[rgb(var(--text-faint)/0.55)] opacity-0 transition-colors hover:bg-orca-failed/10 hover:text-orca-failed group-hover:opacity-100 group-focus-within:opacity-100"
      >
        <Trash2 size={16} strokeWidth={1.5} aria-hidden />
      </button>
      {/*
        内层 run-item 标记（兼容 9b：``page.click("[data-testid=run-item]")`` 命中此 div，
        事件冒泡到外层根触发 onOpen）。同时是布局容器。
      */}
      <div data-testid="run-item">
        {/* 第一行：状态徽章 + workflow 名 */}
        <div className="flex items-center gap-2 pr-8">
          <StatusBadge status={rs} />
          <span className="truncate text-sm font-medium orca-text">
            {run.workflow_name}
          </span>
        </div>
        <div className="orca-text-muted mt-0.5 truncate text-xs">
          {run.project_name || "—"}
        </div>
        {/* 第二行：running/queued → 进度条；blocked → 等待时长 */}
        {isRunning && (
          <div className="orca-bg-surface-2 mt-2 h-1.5 overflow-hidden rounded">
            {progress !== null ? (
              <div
                className="bg-orca-accent h-full rounded transition-[width] duration-300"
                style={{ width: `${Math.round(progress * 100)}%` }}
              />
            ) : (
              // 解析失败 / 缺失 → indeterminate pulse 占满
              <div className="bg-orca-accent/60 h-full w-full animate-pulse rounded" />
            )}
          </div>
        )}
        {isBlocked && (
          <div className="mt-2 inline-flex items-center gap-1 text-xs text-orca-skipped">
            <AlertTriangle size={12} strokeWidth={1.5} aria-hidden />
            等待 {fmtElapsed(run.elapsed)}
          </div>
        )}
        {/* 第三行：cost · elapsed · event_count（muted tabular-nums） */}
        <div className="orca-text-muted mt-2 flex items-center gap-x-3 gap-y-1 text-xs tabular-nums">
          <span>{fmtCost(run.cost)}</span>
          <span className="orca-text-faint">·</span>
          <span>{fmtElapsed(run.elapsed)}</span>
          <span className="orca-text-faint">·</span>
          <span>{run.event_count ?? 0} 事件</span>
        </div>
      </div>
    </div>
  );
}
