// hooks/use-run-events.ts —— 懒加载 + 卸载（SPEC §4.1，铁律 1）。
//
// mount（有 runId）→ store.loadRun（GET /api/runs/<id>/events → loadFromEvents）
// unmount / runId 变 → store.unloadRun（清派生态，不累积，懒加载红线）

import { useEffect } from "react";
import { useWorkflowStore } from "@/stores/workflow-store";

export function useRunEvents(runId: string | undefined): void {
  const loadRun = useWorkflowStore((s) => s.loadRun);
  const unloadRun = useWorkflowStore((s) => s.unloadRun);

  useEffect(() => {
    if (!runId) return;
    void loadRun(runId);
    return () => {
      unloadRun();
    };
  }, [runId, loadRun, unloadRun]);
}
