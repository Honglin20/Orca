// components/runlist/ListSkeleton.tsx —— 首屏骨架（SPEC §4/§2.8/§5.1）。
//
// 契约：
//   - 仅 ``animate-pulse`` + ``orca-bg-surface-2``（禁新动效/扫光）。
//   - 分组头骨架：chevron 位 + 项目名条 + path 条。
//   - 行骨架 ×4：badge 位 + workflow 条 + run_id 条 + 4 metric 条。

export function ListSkeleton() {
  return (
    <div
      data-testid="list-skeleton"
      className="space-y-3"
      role="status"
      aria-label="加载中"
      aria-live="polite"
    >
      <SkeletonGroup />
      <SkeletonGroup />
    </div>
  );
}

function SkeletonGroup() {
  return (
    <section className="rounded bg-[rgb(var(--surface-2)/0.3)] p-2">
      {/* 分组头：chevron + folder + 名 + path */}
      <div className="flex animate-pulse items-center gap-2 px-2 py-2">
        <div className="orca-bg-surface-2 h-3 w-3 rounded" />
        <div className="orca-bg-surface-2 h-3 w-3 rounded" />
        <div className="orca-bg-surface-2 h-3 w-24 rounded" />
        <div className="orca-bg-surface-2 ml-2 h-3 flex-1 rounded" />
      </div>
      {/* 4 行骨架 */}
      <div className="space-y-1.5 px-2 pb-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <SkeletonRow key={i} />
        ))}
      </div>
    </section>
  );
}

function SkeletonRow() {
  return (
    <div className="orca-bg-surface orca-border relative flex animate-pulse items-center gap-3 rounded border px-3 py-2 pl-4 shadow-sm">
      {/* 状态竖条 */}
      <div className="orca-bg-surface-2 absolute inset-y-0 left-0 w-0.5 rounded-l" />
      {/* checkbox 位 */}
      <div className="orca-bg-surface-2 h-4 w-4 rounded" />
      {/* badge 位 */}
      <div className="orca-bg-surface-2 h-5 w-20 rounded-full" />
      {/* workflow 条 */}
      <div className="orca-bg-surface-2 h-3 flex-1 rounded" />
      {/* 4 metric 条 */}
      <div className="orca-bg-surface-2 h-3 w-12 rounded" />
      <div className="orca-bg-surface-2 h-3 w-12 rounded" />
      <div className="orca-bg-surface-2 h-3 w-12 rounded" />
      <div className="orca-bg-surface-2 h-3 w-12 rounded" />
    </div>
  );
}
