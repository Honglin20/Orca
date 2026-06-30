// components/pages/RunDetailPage.tsx —— `/runs/:runId` 详情（SPEC §6.2）。
//
// 关键（铁律 1 + 5）：
//   - useRunEvents(runId)：mount → 懒加载 GET /events；unmount → unloadRun（清派生态）
//   - useWebSocket(runId)：mount → 全量重拉 + subscribe；unmount → 关 WS（无 leak）
//   - tab 切换（dag/log/output/yaml）—— 9c/9d 填充具体视图，本阶段占位。

import { useState } from "react";
import { useParams } from "react-router-dom";
import { useRunEvents } from "@/hooks/use-run-events";
import { useWebSocket } from "@/hooks/use-websocket";
import { useWorkflowStore } from "@/stores/workflow-store";

type Tab = "dag" | "log" | "output" | "yaml";

const TABS: Tab[] = ["dag", "log", "output", "yaml"];

export function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  // 懒加载（铁律 1）+ WS 按需订阅（铁律 5）
  useRunEvents(runId);
  useWebSocket(runId);

  const [tab, setTab] = useState<Tab>("dag");
  const activeRunId = useWorkflowStore((s) => s.activeRunId);
  const status = useWorkflowStore((s) => s.status);
  const eventCount = useWorkflowStore((s) => s.events.length);

  if (!runId) {
    return <p className="p-4 text-sm text-slate-500">缺少 runId</p>;
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-slate-200 p-4">
        <h1 className="text-lg font-semibold">
          Run <span className="font-mono text-base">{runId.slice(0, 8)}</span>
        </h1>
        <span className="text-sm text-slate-500">
          status: {status} · events: {eventCount}
        </span>
      </div>
      <div className="flex border-b border-slate-200 bg-slate-50">
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm ${
              tab === t
                ? "border-b-2 border-slate-900 font-medium text-slate-900"
                : "text-slate-500 hover:text-slate-700"
            }`}
          >
            {t}
          </button>
        ))}
      </div>
      <div className="flex-1 overflow-auto p-4">
        {/* tab 内容 9c/9d 实现；本阶段占位 */}
        <p className="text-sm text-slate-400">
          [{tab}] 视图占位 —— phase 9c/9d 填充。当前 run:{" "}
          {activeRunId ?? "(未加载)"}
        </p>
      </div>
    </div>
  );
}
