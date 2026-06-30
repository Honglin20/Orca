// hooks/use-run-events.ts —— 懒加载 + 卸载（SPEC §4.1，铁律 1）。
//
// mount（有 runId）→ store.loadRun（GET /api/runs/<id>/events → replayState）
// unmount / runId 变 → store.unloadRun（清派生态，不累积，懒加载红线）
//
// 关键：**点开 run 才拉 events**，列表页不调本 hook（只调 useRunsList 元数据）。

import { useEffect } from "react";
import { useWorkflowStore } from "@/stores/workflow-store";

export function useRunEvents(runId: string | undefined): void {
  const loadRun = useWorkflowStore((s) => s.loadRun);
  const unloadRun = useWorkflowStore((s) => s.unloadRun);

  useEffect(() => {
    if (!runId) return; // 无 runId（非详情页）不加载
    void loadRun(runId);
    return () => {
      // 切走 → 卸载当前 run 事件缓存（懒加载红线，SPEC §2.3）
      unloadRun();
    };
  }, [runId, loadRun, unloadRun]);
}
