// components/layout/StatusBar.tsx —— 底栏：当前 run + event count + status（骨架）。
//
// 纯渲染 store 派生态（铁律 6）。

import { useWorkflowStore } from "@/stores/workflow-store";

export function StatusBar() {
  const activeRunId = useWorkflowStore((s) => s.activeRunId);
  const eventCount = useWorkflowStore((s) => s.events.length);
  const status = useWorkflowStore((s) => s.status);

  return (
    <footer className="flex h-8 items-center gap-4 border-t border-slate-200 bg-white px-4 text-xs text-slate-500">
      {activeRunId ? (
        <>
          <span>
            run <span className="font-mono">{activeRunId.slice(0, 8)}</span>
          </span>
          <span>status: {status}</span>
          <span>events: {eventCount}</span>
        </>
      ) : (
        <span>无活跃 run</span>
      )}
    </footer>
  );
}
