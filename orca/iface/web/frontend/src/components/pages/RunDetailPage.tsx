// components/pages/RunDetailPage.tsx —— `/runs/:runId` 详情（SPEC §6.2 + phase 9c 填充）。
//
// 关键（铁律 1 + 5）：
//   - useRunEvents(runId)：mount → 懒加载 GET /events；unmount → unloadRun（清派生态）
//   - useWebSocket(runId)：mount → 全量重拉 + subscribe；unmount → 关 WS（无 leak）
//
// phase 9c 布局（SPEC §1-§4）：
//   - 主区：WorkflowGraph（DAG 可视化）
//   - 右侧：NodeDetail（选中节点详情）
//   - 底部 tab：Log（流式日志，虚拟滚动）/ Output / Yaml
//   - replayMode 时底部多一行 ReplayBar
//   - run 完成（completed/failed）→ Header 出现「⏮ Replay」按钮（SPEC §2.5）

import { useState } from "react";
import { useParams } from "react-router-dom";
import { useRunEvents } from "@/hooks/use-run-events";
import { useWebSocket } from "@/hooks/use-websocket";
import { useWorkflowStore } from "@/stores/workflow-store";
import { WorkflowGraph } from "@/components/graph/WorkflowGraph";
import { NodeDetail } from "@/components/detail/NodeDetail";
import { LogStream } from "@/components/detail/LogStream";
import { ReplayBar } from "@/components/layout/ReplayBar";
import { ChartRenderer } from "@/components/chart/ChartRenderer";

type Tab = "dag" | "log" | "output" | "yaml";

const TABS: Tab[] = ["dag", "log", "output", "yaml"];

export function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  // 懒加载（铁律 1）+ WS 按需订阅（铁律 5）
  useRunEvents(runId);
  useWebSocket(runId);

  const [tab, setTab] = useState<Tab>("dag");
  const status = useWorkflowStore((s) => s.status);
  const eventCount = useWorkflowStore((s) => s.events.length);
  const replayMode = useWorkflowStore((s) => s.replayMode);
  const workflowName = useWorkflowStore((s) => s.workflowName);
  const enterReplay = useWorkflowStore((s) => s.enterReplay);
  const exitReplay = useWorkflowStore((s) => s.exitReplay);

  if (!runId) {
    return <p className="p-4 text-sm text-slate-500">缺少 runId</p>;
  }

  const canReplay =
    !replayMode && (status === "completed" || status === "failed") && eventCount > 0;

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-slate-200 p-4">
        <h1 className="text-lg font-semibold">
          Run <span className="font-mono text-base">{runId.slice(0, 8)}</span>
          {workflowName && (
            <span className="ml-2 text-sm font-normal text-slate-500">
              {workflowName}
            </span>
          )}
        </h1>
        <div className="flex items-center gap-3">
          <span className="text-sm text-slate-500">
            status: {status} · events: {eventCount}
          </span>
          {canReplay && (
            <button
              type="button"
              onClick={enterReplay}
              className="rounded border border-indigo-300 bg-indigo-50 px-3 py-1 text-sm text-indigo-700 hover:bg-indigo-100"
              data-testid="enter-replay-btn"
            >
              ⏮ Replay
            </button>
          )}
          {replayMode && (
            <button
              type="button"
              onClick={exitReplay}
              className="rounded border border-slate-300 bg-white px-3 py-1 text-sm hover:bg-slate-100"
              data-testid="exit-replay-btn"
            >
              ⏹ Live
            </button>
          )}
        </div>
      </div>

      <div className="flex flex-1 overflow-hidden">
        {/* 主区：DAG / Output / Yaml */}
        <div className="flex flex-1 flex-col overflow-hidden">
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
                data-testid={`tab-${t}`}
              >
                {t}
              </button>
            ))}
          </div>
          <div className="flex-1 overflow-auto">
            {tab === "dag" && <WorkflowGraph />}
            {tab === "log" && <LogStream />}
            {tab === "output" && (
              // phase 9d：Output 视图 = 所有节点的图表（nodeId undefined 取全部，SPEC §2.6 Output Panel）。
              <ChartRenderer />
            )}
            {tab === "yaml" && (
              <div className="p-4 text-sm text-slate-500">
                Yaml 视图 —— phase 9d 实现。
              </div>
            )}
          </div>
        </div>

        {/* 右侧：节点详情 */}
        <aside className="w-80 border-l border-slate-200 overflow-auto">
          <NodeDetail />
        </aside>
      </div>

      {/* replay 模式：底部 ReplayBar */}
      {replayMode && <ReplayBar />}
    </div>
  );
}
