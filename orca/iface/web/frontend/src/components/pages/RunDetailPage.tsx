// components/pages/RunDetailPage.tsx —— 单 run 根：3 栏布局（SPEC §4）。
//
// web-shell-v2 §4 三栏：
//   - 左 AgentsRail（agents 列表 + DAG 浮层挂点）
//   - 中 tabs [会话 | 图表]（gate 模态浮于其上，§5.6）
//   - 右 LogStream（常驻最右，虚拟化 live）
// 顶 TopBar（status + elapsed + cost）。**无** Replay 控件（SPEC §3.1 / §8）。
//
// **D5 bundle split**：ConversationView（含 react-markdown 全家桶 ~2MB）/ ChartsView
// （recharts ~400KB）/ WorkflowGraph（xyflow ~250KB）各自 ``React.lazy`` 拆独立 chunk。
// 首屏（TopBar + AgentsRail + LogStream）只剩 ~200KB——conversation/charts/DAG 首次切
// 到才拉对应 chunk。Suspense fallback 给极简骨架（不污染首屏 chunk）。
//
// 关键（铁律 1 + 5）：
//   - useRunEvents(runId)：mount → 懒加载 GET /events；unmount → unloadRun
//   - useWebSocket(runId)：mount → subscribe；重连发 resume（D6）+ resume 失败 fallback

import { Suspense, lazy, useCallback, useState } from "react";
import { useParams } from "react-router-dom";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { useRunEvents } from "@/hooks/use-run-events";
import { useWebSocket } from "@/hooks/use-websocket";
import { useStreamingText } from "@/hooks/use-streaming-text";
import { useElapsedTickActive } from "@/hooks/use-elapsed-tick";
import { useWorkflowStore } from "@/stores/workflow-store";
import { TopBar } from "@/components/layout/TopBar";
import { AgentsRail } from "@/components/layout/AgentsRail";
import { LogStream } from "@/components/detail/LogStream";

// D5：重依赖 view 懒挂——独立 chunk，首屏不加载。
const ConversationView = lazy(() =>
  import("@/components/views/ConversationView").then((m) => ({
    default: m.ConversationView,
  }))
);
const ChartsView = lazy(() =>
  import("@/components/views/ChartsView").then((m) => ({ default: m.ChartsView }))
);

type Tab = "conversation" | "charts";

export function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  useRunEvents(runId);
  // Streaming hook mounted at page root（单 hook，N agent 不开 N timer）。
  // resume 失败 fallback 时 dropBuffer（SPEC §3.3 / D6：丢弃 _textBuf）。
  const streaming = useStreamingText();
  const onResumeFallback = useCallback(() => {
    streaming.dropBuffer();
  }, [streaming]);
  useWebSocket(runId, { onResumeFallback });

  // SPEC §5.2 / §0 D5 单一 elapsed tick 在页根：running 时启 tick，完成 / 终态停。
  // 所有 TopBar / AgentsRail 共用模块级 singleton timer（N agent = 1 timer）。
  const status = useWorkflowStore((s) => s.status);
  useElapsedTickActive(status === "running");

  const [tab, setTab] = useState<Tab>("conversation");
  const selectedNode = useWorkflowStore((s) => s.selectedNode);

  if (!runId) {
    return <p className="p-4 text-sm text-slate-500">缺少 runId</p>;
  }

  return (
    <div className="flex h-full flex-col">
      <TopBar runId={runId} />
      <PanelGroup direction="horizontal" className="flex-1">
        <Panel defaultSize={18} minSize={12} maxSize={30}>
          <AgentsRail />
        </Panel>
        <PanelResizeHandle className="w-px bg-slate-200" />
        <Panel defaultSize={56} minSize={30}>
          <div className="flex h-full flex-col">
            <div className="flex border-b border-slate-200 bg-slate-50">
              {(
                [
                  ["conversation", "会话"],
                  ["charts", "图表"],
                ] as const
              ).map(([t, label]) => (
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
                  {label}
                </button>
              ))}
            </div>
            <div className="flex-1 overflow-auto">
              <Suspense fallback={<TabFallback label="加载会话…" />}>
                {tab === "conversation" && (
                  <ConversationView
                    nodeId={selectedNode}
                    onChartClick={() => setTab("charts")}
                  />
                )}
                {tab === "charts" && <ChartsView />}
              </Suspense>
            </div>
          </div>
        </Panel>
        <PanelResizeHandle className="w-px bg-slate-200" />
        <Panel defaultSize={26} minSize={15}>
          <div
            className="flex h-full flex-col border-l border-slate-200"
            data-testid="log-panel"
          >
            <div className="border-b border-slate-200 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Log
            </div>
            <div className="flex-1 overflow-hidden">
              <LogStream />
            </div>
          </div>
        </Panel>
      </PanelGroup>
    </div>
  );
}

function TabFallback({ label }: { label: string }) {
  return (
    <div
      className="flex h-full items-center justify-center text-sm text-slate-400"
      data-testid="tab-fallback"
    >
      <span className="animate-pulse">{label}</span>
    </div>
  );
}
