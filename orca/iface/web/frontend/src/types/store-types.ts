// types/store-types.ts —— 前端派生类型（非后端契约，手维护；区别于生成的 ./events.ts）。
//
// SPEC §3.1：前端 store = fold(tape) 派生。本文件的类型描述「派生结果形状」，不进 codegen
// （后端不感知；这些类型由前端 selector / reducer shape 决定）。
//
// 铁律 2：前端无独立状态真相——所有派生形状 = ``fold(WebEvent)`` 的输出。

// （events.ts 自动生成；此处不 re-export —— 任何代码用 EventType/WebEvent 一律从 events.ts import）

// ── workflow 级 status（对齐 orca/schema/state.py RunState.status，加前端 "idle"）──────
// 后端 RunStatus: queued|running|completed|failed|cancelled|blocked（projections 派生）。
// 前端加 "idle"（store 未 loadRun）+ "cancelled"/"blocked"（fold 自 workflow_cancelled /
// human_decision_requested 派生，SPEC §5.1 TopBar status icon）。
export type WorkflowStatus =
  | "idle"
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "blocked";

// ── node 级 status（对齐 orca/schema/state.py Status Literal）──────────────────────
// "done" vs workflow 级 "completed" 有意区分（与后端 Status Literal 一致）。
// "blocked" 来自 projections 派生（gate/interrupt），SPEC §4.3.1。
export type NodeStatus =
  | "pending"
  | "running"
  | "done"
  | "failed"
  | "skipped"
  | "blocked";

// ── 派生节点状态（store fold 产出，每节点 last-writer-wins 幂等）──────────────────────
export interface NodeState {
  status: NodeStatus;
  /** node_completed.data.output（最后写者胜）。 */
  output?: unknown;
  /** foreach/parallel 进度 "done/total"。 */
  progress?: string;
  /** node_started.timestamp（用于 AgentsRail ⏱Ns 派生，D5 elapsed snap 用）。 */
  startedAt?: number;
  /** node_completed.data.elapsed（D5 snap，完成停 tick）。 */
  elapsed?: number;
  /** 累计 input/output/reasoning tokens（agent_usage fold，仅该 node 范围）。 */
  inputTokens?: number;
  outputTokens?: number;
  reasoningTokens?: number;
}

// 注：reasoningTokens 在 NodeState 内是 per-node（agent_usage.data.reasoning_tokens 累加）；
// workflow 级 reasoningTokens 单独在 WorkflowState.reasoningTokens（同 fold 派生）。

// ── GateState（D4：human_decision_requested 派生）────────────────────────────────
export interface GateState {
  gate_id: string;
  prompt: string;
  options?: string[];
  source?: string;
  context?: Record<string, unknown>;
}

// ── 最近一次抢答/已答信息（三通道竞速，驱动 ResolvedToast 短暂显示）──────────────────
export interface LastResolved {
  by: string;
  answer: string;
}

// ── WS 客户端 → 后端消息（subscribe/unsubscribe/gate_response/resume）──────────────────
// 对齐 orca/iface/web/ws_handler.py _dispatch 接收的 msg 形状 + D6 resume 扩展。
export type WsClientMessage =
  | { type: "subscribe"; run_id: string }
  | { type: "unsubscribe" }
  | { type: "gate_response"; gate_id: string; answer: string }
  | { type: "resume"; run_id: string; since: number };

// ── RunMeta（懒加载列表项，元数据，SPEC §0.1 铁律 2）──────────────────────────────
// 后置 chunk 才用（RunsListPage 已删；保留供未来「runs 列表后置」复用）。
export interface RunMeta {
  run_id: string;
  workflow_name: string;
  status: WorkflowStatus;
  progress: string;
  cost: number;
  elapsed: number;
  error: string | null;
}

// ── Huge-mode server overview（SPEC web-attach §3 / M3/M4）──────────────────────────
// 服务端 fold 同一 tape 派生的 overview（**非第二真相源**——``load full`` 可全量拉回
// client-fold 校验）。仅 huge=true 时 /meta 返回此字段；前端 store 在 huge 模式设此 slice。
export interface OverviewAgent {
  name: string;
  status: string; // "pending" | "running" | "done" | "failed" | "skipped" | "blocked"
  elapsed?: number;
  tokens?: number;
}
export interface OverviewChart {
  label: string;
  title: string;
  chart_type: string;
}
export interface ServerOverview {
  agents: OverviewAgent[];
  charts: OverviewChart[];
  cost_usd: number;
  run_status: string;
}

// ── /api/runs/<id>/meta 完整响应（SPEC web-attach §3）──────────────────────────────
export interface RunMetaExtended {
  run_id: string;
  status: WorkflowStatus;
  source: "in-process" | "attached";
  event_count: number;
  byte_size: number;
  oldest_seq: number;
  newest_seq: number;
  writable: boolean;
  huge: boolean;
  overview?: ServerOverview;
}
