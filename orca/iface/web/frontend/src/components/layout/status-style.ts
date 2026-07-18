// components/layout/status-style.ts —— WorkflowStatus 视觉风格单一真相源（DRY）。
//
// 色值映射单一出口；图标映射见 ``components/icons.tsx`` 的 ``StatusIcon``（P1 lucide 化后，
// 原 ``STATUS_ICON`` 字符常量已删除，由 ``<StatusIcon status=.../>`` 组件替代）。
//
// 色值映射遵循 tailwind.config.js ``orca.*`` palette（语义色 = brand 入口）：
//   - failed    → ``text-orca-failed``   (= #ef4444 red-500)
//   - running   → ``text-orca-running``  (= #5b8db8 钢蓝 brand 强调色)
//   - completed → ``text-orca-done``     (= #10b981 emerald-500)
//   - cancelled → ``text-orca-pending``  (= #94a3b8 slate-400，中性灰，与 idle 同语义)
//   - blocked   → ``text-orca-skipped``  (= #a78bfa violet-400；plan §P0a 决策：
//                                         blocked 无独立 palette entry，复用 skipped 紫，
//                                         替代原 ``amber-600``——amber 非 brand token）
//   - 其余      → ``text-orca-pending``  (idle/queued 默认中性)

import type { WorkflowStatus } from "@/types/store-types";

/**
 * WorkflowStatus → tailwind 语义色 class（``text-orca-*``，读 ``orca.*`` palette）。
 * 调用方拼接到 className 字符串中（如 ``text-sm ${statusColor(status)}``）。
 */
export function statusColor(status: WorkflowStatus): string {
  if (status === "failed") return "text-orca-failed";
  if (status === "running") return "text-orca-running";
  if (status === "completed") return "text-orca-done";
  if (status === "cancelled") return "text-orca-pending";
  if (status === "blocked") return "text-orca-skipped";
  return "text-orca-pending";
}
