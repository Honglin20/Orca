// hooks/use-run-events.ts —— 懒加载 + 卸载（SPEC §4.1，铁律 1；web-attach §3 huge-mode）。
//
// mount（有 runId）→ store.loadRunWithMeta（GET /api/runs/<id>/meta → 判 huge 分支：
// 非 huge 走原 loadRun 全量；huge 走 tail=500 + serverOverview）
// unmount / runId 变 → store.unloadRun（清派生态 + huge-mode 状态，不累积，懒加载红线）

import { useEffect } from "react";
import { useWorkflowStore } from "@/stores/workflow-store";

export function useRunEvents(runId: string | undefined): void {
  const loadRunWithMeta = useWorkflowStore((s) => s.loadRunWithMeta);
  const unloadRun = useWorkflowStore((s) => s.unloadRun);

  useEffect(() => {
    if (!runId) return;
    void loadRunWithMeta(runId);
    return () => {
      unloadRun();
    };
  }, [runId, loadRunWithMeta, unloadRun]);
}
