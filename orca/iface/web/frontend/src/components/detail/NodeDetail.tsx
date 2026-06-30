// components/detail/NodeDetail.tsx —— 选中节点详情（SPEC §4）。
//
// 显示选中 node 的 status / output / 该 node 相关事件（agent message/thinking/tool）。
// replay 模式只看 events[0..replayPosition]（快照视图）。

import { useMemo } from "react";
import { useWorkflowStore } from "@/stores/workflow-store";
import { formatLogLine } from "./LogStream";
import { ChartRenderer } from "@/components/chart/ChartRenderer";

export function NodeDetail() {
  const selectedNode = useWorkflowStore((s) => s.selectedNode);
  const nodes = useWorkflowStore((s) => s.nodes);
  const events = useWorkflowStore((s) => s.events);
  const replayMode = useWorkflowStore((s) => s.replayMode);
  const replayPosition = useWorkflowStore((s) => s.replayPosition);

  const nodeEvents = useMemo(() => {
    if (!selectedNode) return [];
    const end = replayMode ? replayPosition + 1 : events.length;
    return events
      .slice(0, end)
      .filter((e) => e.node === selectedNode);
  }, [events, selectedNode, replayMode, replayPosition]);

  if (!selectedNode) {
    return (
      <div className="p-4 text-sm text-slate-400" data-testid="detail-empty">
        点击 DAG 节点查看详情
      </div>
    );
  }

  const node = nodes[selectedNode];
  const status = node?.status ?? "pending";

  return (
    <div className="flex h-full flex-col" data-testid="node-detail">
      <div className="border-b border-slate-200 p-3">
        <h3 className="font-mono text-base font-semibold" data-testid="detail-name">
          {selectedNode}
        </h3>
        <p className="text-sm">
          status:{" "}
          <span data-testid="detail-status" className="font-mono">
            {status}
          </span>
          {node?.progress && (
            <span className="ml-2 text-slate-500" data-testid="detail-progress">
              progress: {node.progress}
            </span>
          )}
        </p>
        {node?.output !== undefined && (
          <details className="mt-2">
            <summary className="cursor-pointer text-xs text-slate-500">
              output
            </summary>
            <pre className="mt-1 max-h-40 overflow-auto rounded bg-slate-50 p-2 text-xs">
              {JSON.stringify(node.output, null, 2)}
            </pre>
          </details>
        )}
      </div>
      <div className="flex-1 overflow-auto p-3">
        <h4 className="mb-2 text-xs uppercase text-slate-500">
          事件流（{replayMode ? `replay @${replayPosition}` : "live"}）
        </h4>
        {nodeEvents.length === 0 ? (
          <p className="text-xs text-slate-400">该节点暂无事件</p>
        ) : (
          <ul className="space-y-0.5">
            {nodeEvents.map((e, i) => (
              <li
                key={`${e.seq}-${i}`}
                className="whitespace-pre-wrap font-mono text-xs text-slate-700"
                data-testid={`detail-event-${i}`}
              >
                {formatLogLine(e)}
              </li>
            ))}
          </ul>
        )}
        {/* phase 9d：该节点的图表（SPEC §2.6）。chart 是事件（铁律 4），从 store.events filter。 */}
        <div className="mt-4 border-t border-slate-200 pt-3">
          <h4 className="mb-2 text-xs uppercase text-slate-500">图表</h4>
          <ChartRenderer nodeId={selectedNode} />
        </div>
      </div>
    </div>
  );
}
