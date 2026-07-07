// components/pages/RunDetailPage.tsx —— 单 run 根：3 栏布局（SPEC §4）。
//
// web-shell-v2 §4 三栏：
//   - 左 AgentsRail（agents 列表 + DAG 浮层挂点）
//   - 中 tabs [会话 | 图表]（gate 模态浮于其上，§5.6）
//   - 右 LogStream（常驻最右，虚拟化 live）
// 顶 TopBar（status + elapsed + cost）。**无** Replay 控件（SPEC §3.1 / §8）。
//
// Chunk A：3 栏布局 + tab 切换骨架；会话/图表内容用占位组件（D2/D3 全渲染留后续 chunk）。
//
// 关键（铁律 1 + 5）：
//   - useRunEvents(runId)：mount → 懒加载 GET /events；unmount → unloadRun
//   - useWebSocket(runId)：mount → subscribe；重连发 resume（D6）

import { useCallback, useState } from "react";
import { useParams } from "react-router-dom";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { useRunEvents } from "@/hooks/use-run-events";
import { useWebSocket } from "@/hooks/use-websocket";
import { useStreamingText } from "@/hooks/use-streaming-text";
import { useWorkflowStore } from "@/stores/workflow-store";
import { TopBar } from "@/components/layout/TopBar";
import { AgentsRail } from "@/components/layout/AgentsRail";
import { ConversationView } from "@/components/views/ConversationView";
import { ChartsView } from "@/components/views/ChartsView";
import { LogStream } from "@/components/detail/LogStream";

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
              {tab === "conversation" && <ConversationView nodeId={selectedNode} />}
              {tab === "charts" && <ChartsView />}
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
