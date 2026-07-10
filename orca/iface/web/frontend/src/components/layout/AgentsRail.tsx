// components/layout/AgentsRail.tsx —— 左栏 agents 列表（SPEC §5.2 / §4 三栏布局左 / §0 D5 / §6 D9）。
//
// 用 selectAgents 选择所有节点 → 显示 status icon / 名 / elapsed / token 小字 / stall。
// 点击切中栏会话（D2 按 node 分组）。DAG 切换按钮（§5.7）→ 懒挂全屏浮层。
//
// **单一 timer**（SPEC §5.2）：本组件用 ``useElapsedNow()`` 订阅模块级 singleton tick，
// 不开自己的 setInterval。tick 启停由页根 ``useElapsedTickActive`` 控制（N agent = 1 timer）。
//
// D5 elapsed：``selectNodeElapsed(state, node, now)``——running 时 live tick，完成 snap。
// D9 stall：``selectStall`` —— 当前 node 无新事件 > 5s → 琥珀「思考中 Ns」。

import { Suspense, lazy, useState } from "react";
import { useWorkflowStore } from "@/stores/workflow-store";
import {
  selectAgents,
  selectNodeElapsed,
  selectStall,
  formatTokens,
  formatElapsed,
} from "@/selectors";
import { useElapsedNow } from "@/hooks/use-elapsed-tick";
import type { NodeStatus } from "@/types/store-types";

// D5/D2：xyflow 全家桶（~250KB）懒挂——只在用户点 [DAG] 按钮才下载。
const WorkflowGraph = lazy(() =>
  import("@/components/graph/WorkflowGraph").then((m) => ({
    default: m.WorkflowGraph,
  }))
);

const STATUS_ICON: Record<NodeStatus, string> = {
  pending: "○",
  running: "●",
  done: "✓",
  failed: "✗",
  skipped: "⊘",
  blocked: "⏸",
};

function statusColor(status: NodeStatus): string {
  if (status === "failed") return "text-red-600";
  if (status === "running") return "text-emerald-600";
  if (status === "done") return "text-emerald-600";
  if (status === "blocked") return "text-amber-600";
  return "text-slate-600";
}

/** AgentsRail 内部用 ``formatElapsed(seconds, "seconds")``（紧凑秒级精度）。 */

function DagFallback() {
  return (
    <div
      className="flex h-full items-center justify-center text-sm text-slate-400"
      data-testid="dag-fallback"
    >
      <span className="animate-pulse">加载 DAG…</span>
    </div>
  );
}

export function AgentsRail() {
  // 细粒度订阅（SPEC §1.6/§4 性能意图）：只订阅 selectors 实际依赖的字段（workflowDef +
  // nodes + events），避免「无 selector 整体订阅」导致流式每帧 WS setState 都触发本组件
  // re-render（即便 nodes/events 内容未变）。setSelectedNode 是 stable action ref，订阅一次即可。
  // 重渲染真正必要的时机：workflowDef 变（拓扑首次到达）/ nodes 变（status/elapsed/tokens 派生）
  // / events 变（stall 派生）/ selectedNode 变（高亮）/ now 变（tick → elapsed 推进）。
  const workflowDef = useWorkflowStore((s) => s.workflowDef);
  const nodes = useWorkflowStore((s) => s.nodes);
  const events = useWorkflowStore((s) => s.events);
  const selectedNode = useWorkflowStore((s) => s.selectedNode);
  const setSelectedNode = useWorkflowStore((s) => s.setSelectedNode);
  const [showDag, setShowDag] = useState(false);

  // 构造 selectors 期望的最小 state shape（DRY：未来加字段时只改这里）。`as unknown as`
  // 类型断言把只读字段子集与 WorkflowState 接口对齐——selectors 只读这三个字段，访问其它
  // 字段会运行时报错（fail loud）。
  const state = { workflowDef, nodes, events } as unknown as Parameters<
    typeof selectAgents
  >[0];
  const agents = selectAgents(state);

  // 单一共享 tick —— N agent 共用一个 timer（SPEC §5.2）。
  const now = useElapsedNow();

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
          {agents.map((a) => {
            const elapsed = selectNodeElapsed(state, a.node, now);
            const stall = selectStall(state, a.node, now);
            const tokens = formatTokens(a.inputTokens, a.outputTokens);
            return (
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
                    <span className={statusColor(a.status)}>
                      {STATUS_ICON[a.status]}
                    </span>
                    <span className="font-mono text-xs text-slate-800">
                      {a.node}
                    </span>
                  </span>
                  <span
                    className="text-[10px] text-slate-400"
                    data-testid={`agent-elapsed-${a.node}`}
                  >
                    {elapsed !== null
                      ? `⏱${formatElapsed(elapsed, "seconds")}`
                      : a.status === "running"
                        ? "running"
                        : ""}
                  </span>
                  {stall && (
                    <span
                      className="text-[10px] text-amber-600"
                      data-testid={`agent-stall-${a.node}`}
                    >
                      {stall.thinking ? "💭" : "思考中"}{" "}
                      {Math.floor(stall.sinceMs / 1000)}s
                    </span>
                  )}
                  {tokens && (
                    <span className="text-[10px] text-slate-400">
                      🔤 {tokens}
                    </span>
                  )}
                  {a.progress && (
                    <span className="text-[10px] text-slate-400">
                      ⟳ {a.progress}
                    </span>
                  )}
                </button>
              </li>
            );
          })}
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
            {/* D2：懒挂——首次打开才下载 xyflow chunk */}
            <Suspense fallback={<DagFallback />}>
              <WorkflowGraph />
            </Suspense>
          </div>
        </div>
      )}
    </aside>
  );
}
