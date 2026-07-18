// components/layout/AgentsRail.tsx —— 左栏 agents 列表（SPEC §5.2 / §4 三栏布局左 / §0 D5 / §6 D9）。
//
// P3（web-presentation-refinement §P3）：视觉重做——
//   1. 底色 orca-bg-surface-2（与中间 tab 栏 RunDetailPage.tsx 一致）；agent 行白卡片
//      （orca-bg-surface + border + rounded + hover 浅灰）。
//   2. 去固定宽度 → w-full h-full（react-resizable-panels 全弹性，根治 GAP）。
//   3. 状态色条：左竖条 import NODE_STATUS_HEX（与 DAG 浮层同源 DRY）替代文字 icon。
//   4. 阶段分组 selectAgentGroups：Setup/Loop/Finalize（无 back-route → 单组平铺）。
//   5. 循环节点显示 R3（iteration = sessionCount，依赖 P2 nodesIndex）。
//   6. 子 agent 折叠：sessionCount > 1 → ▸ N subs，展开点子 session 切中栏会话
//      （setSelectedNode + setSelectedSession 联动，复用 P2）。
//
// **单一 timer**（SPEC §5.2）：本组件用 ``useElapsedNow()`` 订阅模块级 singleton tick，
// 不开自己的 setInterval。tick 启停由页根 ``useElapsedTickActive`` 控制（N agent = 1 timer）。
//
// D5 elapsed：``selectNodeElapsed(state, node, now)``——running 时 live tick，完成 snap。
// D9 stall：``selectStall`` —— 当前 node 无新事件 > 5s → 琥珀「思考中 Ns」。

import { Suspense, lazy, useState } from "react";
import { useWorkflowStore } from "@/stores/workflow-store";
import {
  selectAgentGroups,
  selectNodeElapsed,
  selectNodeSessions,
  selectStall,
  formatTokens,
  formatElapsed,
} from "@/selectors";
import { useElapsedNow } from "@/hooks/use-elapsed-tick";
import { NODE_STATUS_HEX } from "@/components/graph/constants";
import type { NodeStatus } from "@/types/store-types";

// D5/D2：xyflow 全家桶（~250KB）懒挂——只在用户点 [DAG] 按钮才下载。
const WorkflowGraph = lazy(() =>
  import("@/components/graph/WorkflowGraph").then((m) => ({
    default: m.WorkflowGraph,
  }))
);

/** "main" session 哨兵（与 workflow-store.MAIN_SESSION 同义；不跨层 import store 内部常量）。 */
const MAIN_SESSION = "main";

/** 未识别 status 兜底色（与 graph/nodes/NodeShell 同策略，DRY）。 */
const STATUS_FALLBACK = NODE_STATUS_HEX.pending;

function DagFallback() {
  return (
    <div
      className="flex h-full items-center justify-center text-sm orca-text-faint"
      data-testid="dag-fallback"
    >
      <span className="animate-pulse">加载 DAG…</span>
    </div>
  );
}

export function AgentsRail() {
  // 细粒度订阅（SPEC §1.6/§4 性能意图）：只订阅 selectors 实际依赖的字段（workflowDef +
  // nodes + events + nodesIndex），避免「无 selector 整体订阅」导致流式每帧 WS setState
  // 都触发本组件 re-render（即便 nodes/events 内容未变）。setSelectedNode / setSelectedSession
  // 是 stable action ref，订阅一次即可。
  // 重渲染真正必要的时机：workflowDef 变（拓扑首次到达 / P3 分组）/ nodes 变（status/elapsed/
  // tokens 派生）/ events 变（stall 派生）/ nodesIndex 变（P3 sessionCount 派生）/ selectedNode
  // 变（高亮）/ selectedSession 变（P3 子 session 高亮）/ now 变（tick → elapsed 推进）。
  const workflowDef = useWorkflowStore((s) => s.workflowDef);
  const nodes = useWorkflowStore((s) => s.nodes);
  const events = useWorkflowStore((s) => s.events);
  const nodesIndex = useWorkflowStore((s) => s.nodesIndex);
  const selectedNode = useWorkflowStore((s) => s.selectedNode);
  const selectedSession = useWorkflowStore((s) => s.selectedSession);
  const setSelectedNode = useWorkflowStore((s) => s.setSelectedNode);
  const setSelectedSession = useWorkflowStore((s) => s.setSelectedSession);
  const [showDag, setShowDag] = useState(false);
  // P3 方案 6：子 agent 折叠态（按 node id 记录展开；Set 重新分配触发 re-render）
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  // 构造 selectors 期望的最小 state shape（DRY：未来加字段时只改这里）。`as unknown as`
  // 是纯编译期类型断言（运行时无影响）：把只读字段子集与 WorkflowState 接口对齐。
  // selectors 契约上只读 workflowDef/nodes/events/nodesIndex 这几个字段；若未来 selector
  // 新读其它字段，需同步在此处补订阅——否则 zustand 不会在该字段变化时触发本组件 re-render
  // （字段会 silent undefined，不会 fail loud，故契约靠人工同步）。
  const state = {
    workflowDef,
    nodes,
    events,
    nodesIndex,
  } as unknown as Parameters<typeof selectAgentGroups>[0];
  const groups = selectAgentGroups(state);

  // 单一共享 tick —— N agent 共用一个 timer（SPEC §5.2）。
  const now = useElapsedNow();

  const toggleFold = (node: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(node)) next.delete(node);
      else next.add(node);
      return next;
    });

  return (
    <aside
      // P3 方案 1/2：orca-bg-surface-2（底色统一，与中间 tab 栏一致）+ w-full h-full
      // （去固定宽度 → 全弹性填满 Panel，react-resizable-panels 同款，根治 GAP）
      className="flex h-full w-full flex-col border-r orca-border orca-bg-surface-2"
      data-testid="agents-rail"
    >
      <div className="flex items-center justify-between p-3">
        <h2 className="text-sm font-semibold orca-text-muted">Agents</h2>
        <button
          type="button"
          onClick={() => setShowDag((v) => !v)}
          className="rounded border orca-border px-2 py-0.5 text-xs orca-text-muted hover:orca-bg-surface"
          data-testid="dag-toggle"
        >
          DAG
        </button>
      </div>
      {groups.length === 0 ? (
        <p className="px-3 text-xs orca-text-faint">暂无 agent</p>
      ) : (
        <div className="flex-1 space-y-3 overflow-y-auto p-2">
          {groups.map((g) => (
            <section key={g.group} data-testid={`agent-group-${g.group}`}>
              <h3 className="px-1 pb-1 text-[10px] font-semibold uppercase tracking-wide orca-text-faint">
                {g.group}
              </h3>
              <ul className="space-y-1">
                {g.agents.map((a) => {
                  const status: NodeStatus = a.status;
                  const color = NODE_STATUS_HEX[status] ?? STATUS_FALLBACK;
                  const elapsed = selectNodeElapsed(state, a.node, now);
                  const stall = selectStall(state, a.node, now);
                  const tokens = formatTokens(a.inputTokens, a.outputTokens);
                  const sessionCount = a.sessionCount ?? 0;
                  const isExpanded = expanded.has(a.node);
                  const showFold = sessionCount > 1;
                  // P3 方案 5：循环节点（Loop 组）显示 R{iteration}
                  const showIter = g.group === "Loop" && (a.iteration ?? 0) > 0;
                  return (
                    <li
                      key={a.node}
                      className="relative overflow-hidden rounded border orca-border orca-bg-surface"
                    >
                      {/* P3 方案 3：左竖状态色条（NODE_STATUS_HEX，与 DAG 浮层同源 DRY）替代文字 icon */}
                      <div
                        className="absolute inset-y-0 left-0 w-1"
                        style={{ backgroundColor: color }}
                        data-testid={`agent-bar-${a.node}`}
                        data-status={status}
                      />
                      <button
                        type="button"
                        onClick={() => setSelectedNode(a.node)}
                        data-testid={`agent-row-${a.node}`}
                        className={`flex w-full flex-col items-start gap-0.5 py-2 pl-3 pr-2 text-left hover:orca-bg-surface-2 ${
                          selectedNode === a.node ? "orca-bg-surface-2" : ""
                        }`}
                      >
                        <span className="flex w-full items-center gap-2 text-sm">
                          <span className="font-mono text-xs orca-text">
                            {a.node}
                          </span>
                          {showIter && (
                            <span
                              className="rounded orca-bg-surface-2 px-1 text-[10px] font-medium orca-text-muted"
                              data-testid={`agent-iter-${a.node}`}
                            >
                              R{a.iteration}
                            </span>
                          )}
                        </span>
                        <span
                          className="text-[10px] orca-text-faint"
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
                            className="text-[10px] text-orca-skipped"
                            data-testid={`agent-stall-${a.node}`}
                          >
                            {stall.thinking ? "💭" : "思考中"}{" "}
                            {Math.floor(stall.sinceMs / 1000)}s
                          </span>
                        )}
                        {tokens && (
                          <span className="text-[10px] orca-text-faint">
                            🔤 {tokens}
                          </span>
                        )}
                        {a.progress && (
                          <span className="text-[10px] orca-text-faint">
                            ⟳ {a.progress}
                          </span>
                        )}
                      </button>
                      {/* P3 方案 6：子 agent 折叠（sessionCount > 1）—— 复用 selectNodeSessions */}
                      {showFold && (
                        <button
                          type="button"
                          onClick={() => toggleFold(a.node)}
                          data-testid={`agent-fold-${a.node}`}
                          className="flex w-full items-center gap-1 px-3 pb-1.5 text-left text-[10px] orca-text-faint hover:orca-text-muted"
                        >
                          <span className="text-xs leading-none">
                            {isExpanded ? "▾" : "▸"}
                          </span>
                          <span>·</span>
                          <span>{sessionCount} subs</span>
                        </button>
                      )}
                      {isExpanded && showFold && (
                        <ul
                          className="border-t orca-border px-2 py-1"
                          data-testid={`agent-subs-${a.node}`}
                        >
                          {selectNodeSessions(state, a.node)
                            .filter((s) => s.sessionId !== MAIN_SESSION)
                            .map((s) => (
                              <li key={s.sessionId}>
                                <button
                                  type="button"
                                  // P2 联动：先 setSelectedNode（切中栏到该 node；
                                  // 其 P1-3 逻辑会把 selectedSession 设成第一个 sub），
                                  // 再 setSelectedSession 覆盖到目标 session。
                                  onClick={() => {
                                    setSelectedNode(a.node);
                                    setSelectedSession(s.sessionId);
                                  }}
                                  data-testid={`agent-sub-${a.node}-${s.sessionId}`}
                                  className={`flex w-full items-center justify-between gap-2 rounded px-2 py-1 text-left text-[10px] hover:orca-bg-surface-2 ${
                                    selectedNode === a.node &&
                                    selectedSession === s.sessionId
                                      ? "orca-bg-surface-2"
                                      : ""
                                  }`}
                                >
                                  <span className="font-mono orca-text-muted">
                                    {s.label}
                                  </span>
                                  <span className="orca-text-faint">
                                    {s.eventCount}
                                  </span>
                                </button>
                              </li>
                            ))}
                        </ul>
                      )}
                    </li>
                  );
                })}
              </ul>
            </section>
          ))}
        </div>
      )}
      {showDag && (
        <div
          className="fixed inset-0 z-50 bg-slate-900/40"
          // P0b 白名单（intentional inverse）：DAG overlay 是 dark backdrop + light panel
          // 的强对比浮层，slate-900/40 不属于 surface scale（与 ResolvedToast/LogStream
          // live badge 同类），P0 不替换；P3 暗色机制收口时统一处理。
          onClick={() => setShowDag(false)}
          data-testid="dag-overlay"
        >
          <div
            className="absolute inset-8 rounded orca-bg-surface p-2 shadow"
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
