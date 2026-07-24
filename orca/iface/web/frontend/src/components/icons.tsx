// components/icons.tsx —— 图标单一入口（P1 lucide 统一）。
//
// 全项目图标走 lucide-react 细线条（strokeWidth 1.5），统一尺寸：行内 14 / 标题 16。
// lucide 图标继承 ``currentColor``，故配色由父级 className（如 ``text-orca-failed``）决定。
//
// **唯一保留 Unicode**：流式光标 ``▎``（SPEC §5.3 文本光标契约，是文本非图标，见
// MessageBlock.tsx / AgentsRail.tsx）。
//
// StatusIcon 是 WorkflowStatus → 图标的 DRY 出口（与 status-style.ts statusColor 同源），
// 供 TopBar / AgentsRail / DAG / 未来 minimap 复用。

import {
  Circle,
  CircleDot,
  CircleCheck,
  CircleX,
  CircleSlash,
  PauseCircle,
  type LucideProps,
} from "lucide-react";
import type { WorkflowStatus } from "@/types/store-types";

/** 统一图标尺寸常量（行内 / 标题）。 */
export const ICON_SIZE_INLINE = 14;
export const ICON_SIZE_TITLE = 16;
export const ICON_STROKE = 1.5;

const STATUS_ICON_COMPONENT: Record<
  WorkflowStatus,
  React.ComponentType<LucideProps>
> = {
  idle: Circle,
  queued: Circle,
  running: CircleDot,
  completed: CircleCheck,
  failed: CircleX,
  cancelled: CircleSlash,
  blocked: PauseCircle,
};

/**
 * WorkflowStatus → lucide 图标组件。
 * 配色由父级 className 决定（继承 currentColor），典型用法：
 *   <StatusIcon status={s} className={statusColor(s)} />
 */
export function StatusIcon({
  status,
  size = ICON_SIZE_INLINE,
  ...rest
}: { status: WorkflowStatus } & LucideProps) {
  const Cmp = STATUS_ICON_COMPONENT[status];
  return <Cmp size={size} strokeWidth={ICON_STROKE} aria-hidden {...rest} />;
}
