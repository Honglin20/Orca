// components/layout/status-badge.tsx
//
// 通用 RunStatus 徽章（SPEC §6.3 / D10 / mockup 复用）。
//
// 从 RunListPageMockup 抽出来公用——列表页 + 未来其它地方（详情页标题栏等）共用同一映射。
// 配色复用 ``orca.*`` tailwind palette（pending/running/done/failed/skipped）+ CSS 变量。

export type RunStatus =
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "blocked"
  | "queued"
  | "live-pending";

const STATUS_DOT_BG: Record<RunStatus, string> = {
  running: "bg-orca-running",
  completed: "bg-orca-done",
  failed: "bg-orca-failed",
  cancelled: "bg-orca-pending",
  blocked: "bg-orca-skipped",
  queued: "bg-orca-pending",
  "live-pending": "bg-orca-pending",
};

const STATUS_TEXT: Record<RunStatus, string> = {
  running: "text-orca-running",
  completed: "text-orca-done",
  failed: "text-orca-failed",
  cancelled: "text-orca-pending",
  blocked: "text-orca-skipped",
  queued: "text-orca-pending",
  "live-pending": "text-orca-pending",
};

const STATUS_BORDER: Record<RunStatus, string> = {
  running: "border-orca-running/30",
  completed: "border-orca-done/30",
  failed: "border-orca-failed/30",
  cancelled: "border-orca-pending/30",
  blocked: "border-orca-skipped/30",
  queued: "border-orca-pending/30",
  "live-pending": "border-orca-pending/30",
};

const STATUS_LABEL: Record<RunStatus, string> = {
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  blocked: "待决策",
  queued: "排队中",
  "live-pending": "同步中",
};

export const STATUS_BAR_HEX: Record<RunStatus, string> = {
  running: "#5b8db8",
  completed: "#10b981",
  failed: "#ef4444",
  cancelled: "#94a3b8",
  blocked: "#a78bfa",
  queued: "#94a3b8",
  "live-pending": "#94a3b8",
};

export function StatusBadge({ status }: { status: RunStatus }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium ${STATUS_TEXT[status]} ${STATUS_BORDER[status]}`}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${STATUS_DOT_BG[status]} ${
          status === "running" ? "animate-pulse" : ""
        }`}
      />
      {STATUS_LABEL[status]}
    </span>
  );
}

export function statusToRunStatus(s: string): RunStatus {
  if (s === "running" || s === "completed" || s === "failed" || s === "cancelled" || s === "queued" || s === "live-pending" || s === "blocked") {
    return s;
  }
  // 兜底：未知 status → live-pending（前端中性态）
  return "live-pending";
}
