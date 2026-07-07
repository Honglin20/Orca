// components/layout/AgentsRail.tsx —— 左栏 agents 列表（SPEC §5.2 / §4 三栏布局左）。
//
// 用 selectAgents 选择所有节点 → 显示 status icon / 名 / elapsed / token 小字。
// 点击切中栏会话（D2 按 node 分组）。DAG 切换按钮（浮层）挂载点（§5.7，内容后置）。
//
// Chunk A：占位实现（无 DAG 浮层按钮交互；elapsed live tick 留给后续 chunk 的 useElapsedTick）。

import { useState } from "react";
import { useWorkflowStore } from "@/stores/workflow-store";
import { selectAgents } from "@/selectors";
import type { NodeStatus } from "@/types/store-types";
import { WorkflowGraph } from "@/components/graph/WorkflowGraph";

const STATUS_ICON: Record<NodeStatus, string> = {
  pending: "○",
  running: "●",
  done: "✓",
  failed: "✗",
  skipped: "⊘",
  blocked: "⏸",
};

export function AgentsRail() {
  const state = useWorkflowStore();
  const agents = selectAgents(state);
  const selectedNode = useWorkflowStore((s) => s.selectedNode);
  const setSelectedNode = useWorkflowStore((s) => s.setSelectedNode);
  const [showDag, setShowDag] = useState(false);

  return (
    <aside
      className="flex w-56 flex-col border-r border-slate-200 bg-white"
      data-testid="agents-rail"
    >
      <div className="flex items-center justify-between p-3">
        <h2 className="text-sm font-semibold text-slate-700">Agents</h2>
        <button
          type="button"
          onClick={() => setShowDag((v) => !v)}
          className="rounded border border-slate-300 px-2 py-0.5 text-xs text-slate-600 hover:bg-slate-100"
          data-testid="dag-toggle"
        >
          DAG
        </button>
      </div>
      {agents.length === 0 ? (
        <p className="px-3 text-xs text-slate-400">暂无 agent</p>
      ) : (
        <ul className="flex-1 overflow-y-auto">
          {agents.map((a) => (
            <li key={a.node}>
              <button
                type="button"
                onClick={() => setSelectedNode(a.node)}
                data-testid={`agent-row-${a.node}`}
                className={`flex w-full flex-col items-start gap-0.5 px-3 py-2 text-left hover:bg-slate-100 ${
                  selectedNode === a.node ? "bg-slate-100" : ""
                }`}
              >
                <span className="flex w-full items-center gap-2 text-sm">
                  <span
                    className={
                      a.status === "failed"
                        ? "text-red-600"
                        : a.status === "running"
                          ? "text-emerald-600"
                          : "text-slate-600"
                    }
                  >
                    {STATUS_ICON[a.status] ?? "?"}
                  </span>
                  <span className="font-mono text-xs text-slate-800">
                    {a.node}
                  </span>
                </span>
                <span className="text-[10px] text-slate-400">
                  {a.elapsed !== undefined
                    ? `⏱${a.elapsed.toFixed(1)}s`
                    : a.startedAt
                      ? "running"
                      : ""}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {showDag && (
        <div
          className="fixed inset-0 z-50 bg-slate-900/40"
          onClick={() => setShowDag(false)}
          data-testid="dag-overlay"
        >
          <div
            className="absolute inset-8 rounded bg-white p-2 shadow"
            onClick={(e) => e.stopPropagation()}
          >
            <WorkflowGraph />
          </div>
        </div>
      )}
    </aside>
  );
}
