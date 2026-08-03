// components/runlist/EmptyState.tsx —— 空态 / 筛选空（SPEC §4/§5.1/§5.2）。
//
// 两种形态：
//   - ``mode="empty"``：完全无 run（runs.length === 0），大 Inbox icon + 主副文案。
//   - ``mode="filtered"``：有数据但被筛光，Search icon + 提示调整搜索（不跳全屏空态，§5.2 行内提示由 ProjectGroup 处理）。

import { Inbox, Search } from "lucide-react";

interface Props {
  mode: "empty" | "filtered";
}

export function EmptyState({ mode }: Props) {
  if (mode === "filtered") {
    return (
      <div
        data-testid="filtered-empty"
        className="flex h-80 flex-col items-center justify-center gap-2"
      >
        <Search size={36} strokeWidth={1} aria-hidden className="orca-text-faint" />
        <p className="orca-text text-sm font-medium">没有匹配的 run</p>
        <p className="orca-text-faint text-sm">试试调整搜索或过滤条件。</p>
      </div>
    );
  }
  return (
    <div
      data-testid="empty-state"
      className="flex h-80 flex-col items-center justify-center gap-2"
    >
      <Inbox size={48} strokeWidth={1} aria-hidden className="orca-text-faint" />
      <p className="orca-text text-base font-medium">暂无 run</p>
      <p className="orca-text-faint text-sm">
        在项目里运行 <code className="font-mono">orca run &lt;workflow&gt;</code> 即可在此看到。
      </p>
    </div>
  );
}
