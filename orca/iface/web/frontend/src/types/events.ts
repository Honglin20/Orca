// types/events.ts —— 前端事件契约（**逐字对齐后端 orca/schema/event.py +
// orca/iface/web/{run_manager.py,routes/runs.py,ws_handler.py}**）。
//
// 铁律（SPEC §0.1 铁律 2 / §6）：前端不持有业务真相，store = 这些事件的 fold 派生物。
// 类型不匹配 = 静默断裂，所以**改后端先改这里**。

// ── EventType（对齐 orca/schema/event.py EventType Literal 联合体，21 个）──────────
// 注释里标注每个 type 的 data payload 字段（来自 schema/event.py 注释），handler 表据此 dispatch。
export type EventType =
  // ── workflow 生命周期 ──
  | "workflow_started" // data: {inputs, node_count, entry, workflow_name}
  | "workflow_completed" // data: {elapsed, outputs}
  | "workflow_failed" // data: {error_type, message, node}
  // ── node 生命周期 ──
  | "node_started"
  | "node_completed" // data: {elapsed, output}
  | "node_failed" // data: {error_type, message, phase}
  | "node_skipped" // data: {reason}
  // ── agent 流式（claude stream-json 翻译产出）──
  | "agent_message" // data: {text}
  | "agent_thinking" // data: {text}
  | "agent_tool_call" // data: {tool, args, tool_call_id}
  | "agent_tool_result" // data: {tool_call_id, result}
  | "agent_usage" // data: {input_tokens, output_tokens, cache_tokens, cost_usd}
  // ── 路由 ──
  | "route_taken" // data: {from, to}
  // ── 并发 ──
  | "foreach_started" // data: {item_count, max_concurrent}
  | "foreach_item_started" // data: {index, item_key}
  | "foreach_item_completed" // data: {index, output}
  | "foreach_completed" // data: {count, succeeded}
  // ── HMIL（gates extension 产出）──
  | "human_decision_requested" // data: {gate_id, prompt, options?, source, context}
  | "human_decision_resolved" // data: {gate_id, answer}
  // ── 自定义 ──
  | "custom" // data: {kind: "chart"|"table"|"image"|..., ...}
  // ── 错误 ──
  | "error"; // data: {error_type, message, phase?}

// ── Event（对齐 orca/schema/event.py Event 模型）──────────────────────────────
// 后端 model_dump() 出来的形状（顶层字段 + data dict）。WS 推送时 ws_handler 给每条
// 事件加 run_id 标签（让前端区分来源 run），REST /events 返回的则无 run_id。
export interface WorkflowEvent {
  seq: number; // 全局单调递增（不变量）
  type: EventType;
  timestamp: number; // epoch 秒
  node: string | null; // 哪个 node 产出；workflow 级为 null
  session_id: string | null; // 哪次 agent 调用；workflow/node 级生命周期可为 null
  data: Record<string, unknown>; // 各 type 特定 payload
  /** WS 推送时 ws_handler._pump 注入的来源 run 标签；REST /events 返回无此字段。 */
  run_id?: string;
}

// ── RunMeta（对齐 orca/iface/web/run_manager.py RunMeta + routes/runs.py _meta_to_dict）──
// 懒加载列表项：**只有元数据，不含事件**（SPEC §0.1 铁律 2）。
// progress 形如 "3/7"（done/total）。status 取后端 RunStatus Literal。
export type RunStatus = "queued" | "running" | "completed" | "failed";

// ── WorkflowStatus（前端派生：workflow 级 status + "idle"=未加载任何 run）────────
// 后端 RunStatus 逐字 + 前端自造 "idle"（store 未 loadRun 时）。区别于 node 级 Status。
// 注：node 级 Status Literal（"pending"|"running"|"done"|"failed"|"skipped"）见下方
// NodeStatus，对齐 orca/schema/state.py Status Literal（"done" vs workflow 的 "completed" 有意区分）。
export type WorkflowStatus = RunStatus | "idle";

export interface RunMeta {
  run_id: string;
  workflow_name: string;
  status: RunStatus;
  progress: string; // "done/total"，例如 "3/7"
  cost: number;
  elapsed: number;
  error: string | null;
}

// ── NodeState（派生：store fold 产出的每节点状态）──────────────────────────────
// 对齐 orca/schema/state.py Status Literal（"done" vs RunState.status 的 "completed" 有意区分）。
export type NodeStatus = "pending" | "running" | "done" | "failed" | "skipped";

export interface NodeState {
  status: NodeStatus;
  output?: unknown; // node_completed 的 data.output（最后写者胜，幂等）
  /** foreach/parallel 进度（"done/total"），phase 9c DAG widget 读。 */
  progress?: string;
}

// ── GateState（派生：当前 human gate，phase 9d 弹窗读）────────────────────────
// 来自 human_decision_requested 的 data payload（gate_id / prompt / options / source / context）。
export interface GateState {
  gate_id: string;
  prompt: string;
  options?: string[];
  source?: string;
  context?: Record<string, unknown>;
}

// ── WS 客户端 → 后端消息（subscribe/unsubscribe/gate_response）──────────────────
// 对齐 orca/iface/web/ws_handler.py _dispatch 接收的 msg 形状。
export type WsClientMessage =
  | { type: "subscribe"; run_id: string }
  | { type: "unsubscribe" }
  | { type: "gate_response"; gate_id: string; answer: string };
