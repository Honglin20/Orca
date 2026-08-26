// components/runlist/KpiStrip.tsx —— KPI 概览带 = 状态过滤器（SPEC web-board-cardgrid §2.2）。
//
// 契约：
//   - 位置：品牌行下方、main 滚动区**之外**（固定区 ``shrink-0``），独占一行
//     ``h-10 shrink-0 orca-bg-surface orca-border border-b px-6``。
//   - 内容：4 个状态胶囊 + 1 个总数，顺序 **运行 N · 待决策 N · 失败 N · 完成 N …… 共 N runs**
//     （失败提前到完成前，与 §4.1 section 顺序「…失败→已完成」对齐，落实「失败优先可见」）。
//   - 计数从 ``runs`` 实时算，**不受 q/status 过滤影响**（始终显全量分布；SPEC §2.2）：
//       运行 = count(status ∈ {running, queued})
//       待决策 = count(status === blocked)
//       失败 = count(status ∈ {failed, cancelled})
//       完成 = count(status === completed)
//       共 = runs.length
//   - 胶囊 = 过滤器（替代顶栏 StatusFilterChips）：点胶囊 → setStatus(filter)；
//     active 胶囊用强选中态（``border-transparent bg-orca-accent text-[rgb(var(--app-bg))]``）；
//     点「共 N runs」或已 active 胶囊再次点击 → setStatus("all")。
//   - dot 色取 ``STATUS_DOT_BG[sourceStatus]``（与原 StatusFilterChips 同源 DRY）。
//   - 失败计数 >0 时胶囊强提示：文字 + dot 用 ``text-orca-failed``（即便非 active）。
//
// data-testid：``kpi-strip``（根）；``kpi-chip-<running|blocked|completed|failed>``；``kpi-chip-all``。
//
// ``StatusFilter`` 类型从 ``StatusFilterChips`` 迁入此文件（SPEC §10 强制顺序 step ①）。

import {
  STATUS_DOT_BG,
  type RunStatus,
} from "@/components/layout/status-badge";

/** 状态过滤维度（SPEC §2.2；从 StatusFilterChips 迁入）。 */
export type StatusFilter = "all" | "running" | "blocked" | "completed" | "failed";

interface ChipDef {
  /** 状态胶囊 key（不含 "all"——「共 N runs」单独渲染）。 */
  key: Exclude<StatusFilter, "all">;
  label: string;
  /** dot 取色源 status（无则不显 dot） */
  sourceStatus?: RunStatus;
  /** dot tooltip（可选） */
  dotTooltip?: string;
}

// 胶囊顺序：运行 · 待决策 · 失败 · 完成（SPEC §2.2，失败提前到完成前）。
const CHIPS: ChipDef[] = [
  {
    key: "running",
    label: "运行",
    sourceStatus: "running",
    dotTooltip: "含排队中",
  },
  { key: "blocked", label: "待决策", sourceStatus: "blocked" },
  { key: "failed", label: "失败", sourceStatus: "failed" },
  { key: "completed", label: "完成", sourceStatus: "completed" },
];

export interface KpiCounts {
  running: number;
  blocked: number;
  failed: number;
  completed: number;
  total: number;
}

interface Props {
  counts: KpiCounts;
  active: StatusFilter;
  onChange: (s: StatusFilter) => void;
}

export function KpiStrip({ counts, active, onChange }: Props) {
  // 计数映射：filter key → 计数值。
  const countOf: Record<ChipDef["key"], number> = {
    running: counts.running,
    blocked: counts.blocked,
    failed: counts.failed,
    completed: counts.completed,
  };

  return (
    <div
      data-testid="kpi-strip"
      className="orca-bg-surface orca-border orca-text flex h-10 shrink-0 items-center gap-2 border-b px-6 text-xs"
    >
      {CHIPS.map((c) => {
        const isActive = active === c.key;
        const n = countOf[c.key];
        const dotCls = c.sourceStatus ? STATUS_DOT_BG[c.sourceStatus] : undefined;
        // 失败计数 >0 时强提示色（SPEC §2.2：即便非 active 也红）。
        const failedAlert = c.key === "failed" && n > 0;
        return (
          <button
            key={c.key}
            type="button"
            data-testid={`kpi-chip-${c.key}`}
            onClick={() => onChange(isActive ? "all" : c.key)}
            aria-pressed={isActive}
            className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 tabular-nums ${
              isActive
                ? "border-transparent bg-orca-accent text-[rgb(var(--app-bg))]"
                : failedAlert
                  ? "orca-border orca-bg-surface text-orca-failed hover:orca-bg-surface-2"
                  : "orca-border orca-text-muted orca-bg-surface hover:orca-bg-surface-2"
            }`}
          >
            {dotCls && (
              <span
                title={c.dotTooltip}
                className={`h-1.5 w-1.5 rounded-full ${
                  isActive ? "bg-[rgb(var(--app-bg)/0.8)]" : dotCls
                } ${c.sourceStatus === "running" ? "animate-pulse" : ""}`}
              />
            )}
            <span>{c.label}</span>
            <span className="font-semibold">{n}</span>
          </button>
        );
      })}
      {/* 总数（点 → setStatus("all")，SPEC §2.2） */}
      <button
        type="button"
        data-testid="kpi-chip-all"
        onClick={() => onChange("all")}
        className="orca-text-muted hover:orca-text ml-auto inline-flex items-center gap-1 tabular-nums"
      >
        共 <span className="font-semibold orca-text">{counts.total}</span> runs
      </button>
    </div>
  );
}
