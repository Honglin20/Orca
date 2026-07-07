// selectors.ts —— 纯函数 selector：state → view model（SPEC §3.1 / §0 D2 / D7）。
//
// 铁律：selector 是 **唯一** view 输入。组件不直接读 store.events 做派生（避免 N 处
// fold 漂移）。所有视图真相从这里出。
//
// D2 conversation 分组键 = node；retry/foreach 多 session_id 在同 node 内合并（细分隔符）。
// D7 seq 升序 fold：selectCharts(T)==selectCharts(sort(T))==selectCharts(reverse(T))。
//
// 这些 selector 输出不可变快照（每次调用新建结构），调用方应 useCallback/useMemo
// 避免每 render 重算。

import type { WebEvent } from "@/types/events";
import type { NodeState } from "@/types/store-types";
import type { WorkflowState } from "@/stores/workflow-store";

// ── selectAgents：DAG nodes → AgentsRail 行模型 ─────────────────────────────────
export interface AgentRow {
  node: string;
  status: NodeState["status"];
  progress?: string;
  elapsed?: number;
  startedAt?: number;
  inputTokens?: number;
  outputTokens?: number;
  reasoningTokens?: number;
}

export function selectAgents(state: WorkflowState): AgentRow[] {
  return Object.entries(state.nodes).map(([node, ns]: [string, NodeState]) => ({
    node,
    status: ns.status,
    progress: ns.progress,
    elapsed: ns.elapsed,
    startedAt: ns.startedAt,
    inputTokens: ns.inputTokens,
    outputTokens: ns.outputTokens,
    reasoningTokens: ns.reasoningTokens,
  }));
}

// ── selectConversation：events → per-node 对话模型（D2 按 node 分组）──────────────
// 输出按 seq 升序的事件分组（每 node 一个数组）。retry/foreach 多 session_id 在同 node
// 内合并（细分隔符是渲染层职责，本 selector 只输出按 (node, seq) 排序的事件流）。
//
// 「orphan tool_result」（无对应 call）在本 selector 不进 conversation——渲染层决定是否
// 进 LogStream warn。本 selector 输出**全部** events 供视图分类；orphan 判定是视图层关注点
// （DiffView/ToolCallMessage 内做）。
export interface ConversationGroup {
  node: string;
  events: WebEvent[];
}

/** 选择所有应进 conversation 的事件，按 node 分组，每组按 seq 升序。 */
export function selectConversation(
  state: WorkflowState,
  nodeId: string | null | undefined
): ConversationGroup {
  if (nodeId === undefined || nodeId === null) {
    return { node: "", events: [] };
  }
  // 仅取该 node 的 conversation-相关事件，按 seq 升序（state.events 已是 seq-sorted）。
  // SPEC §5.3：foreach_* / retry_* / interrupt_* / validator_* / wait_* 在 conversation 内
  // dim 渲染——故纳入 conversation 事件集（dim 是渲染层决定，本 selector 只输出事件流）。
  const convTypes = new Set<WebEvent["type"]>([
    "prompt_rendered",
    "agent_thinking",
    "agent_message",
    "agent_tool_call",
    "agent_tool_result",
    "agent_step_started",
    "dialog_started",
    "dialog_message",
    "dialog_ended",
    "node_started",
    "node_completed",
    "node_failed",
    "node_skipped",
    "retry_started",
    "retry_succeeded",
    "retry_exhausted",
    "interrupt_requested",
    "interrupt_resolved",
    "validator_started",
    "validator_passed",
    "validator_failed",
    "wait_started",
    "wait_completed",
    "foreach_started",
    "foreach_item_started",
    "foreach_item_completed",
    "foreach_completed",
    "custom",
    "workflow_failed",
    "unknown_event",
  ]);
  const events = state.events.filter(
    (e) => e.node === nodeId && convTypes.has(e.type)
  );
  return { node: nodeId, events };
}

// ── selectCharts：custom(kind=chart) → ChartsView（D3 / D7）──────────────────────
export interface ChartEntry {
  seq: number;
  node: string | null;
  /** 分组键 = data.label ?? "misc"。 */
  group: string;
  /** 组内身份 = data.title ?? chart_type+seq（同 identity upsert）。 */
  identity: string;
  /** 原始 chart payload（ChartPayload shape 由 chart/types.ts 定义）。 */
  payload: unknown;
  /** 原始事件 seq，用于 D7 sort 后的稳定身份消歧。 */
}

/** 提取单条 custom 事件的 chart payload（data.kind==="chart"）。非 chart → null。 */
function extractChartPayload(e: WebEvent): {
  chart: Record<string, unknown>;
} | null {
  const d = e.data;
  if (!d || d.kind !== "chart") return null;
  const chart = d.chart;
  if (!chart || typeof chart !== "object") return null;
  return { chart: chart as Record<string, unknown> };
}

/**
 * 选择所有 chart 事件 → 按 group 分组 + identity 去重（D7 seq 升序 fold）。
 *
 * 同 identity upsert：后到（更大 seq）覆盖前到。state.events 已 seq-sorted，故遍历即得
 * D7 序无关结果（selectCharts(T)==selectCharts(sort(T))==selectCharts(reverse(T))）。
 */
export function selectCharts(state: WorkflowState): {
  groups: { group: string; entries: ChartEntry[] }[];
} {
  // identity → entry（同 identity upsert，后到胜）
  const byIdentity = new Map<string, ChartEntry>();
  for (const e of state.events) {
    if (e.type !== "custom") continue;
    const extracted = extractChartPayload(e);
    if (!extracted) continue;
    const chart = extracted.chart;
    const label = typeof chart.label === "string" ? chart.label : "misc";
    const chartType = typeof chart.chart_type === "string" ? chart.chart_type : "chart";
    const title = typeof chart.title === "string" ? chart.title : "";
    const identity = title || `${chartType}#${e.seq}`;
    // D7 upsert：直接覆盖（后到 seq 更大胜；seq 升序遍历 → 最后写入 = max seq）
    byIdentity.set(identity, {
      seq: e.seq,
      node: e.node,
      group: label,
      identity,
      payload: chart,
    });
  }
  // 按 group 分组，保持首次插入顺序
  const groupMap = new Map<string, ChartEntry[]>();
  for (const entry of byIdentity.values()) {
    const arr = groupMap.get(entry.group);
    if (arr) arr.push(entry);
    else groupMap.set(entry.group, [entry]);
  }
  return { groups: Array.from(groupMap.entries()).map(([group, entries]) => ({ group, entries })) };
}

// ── selectLog：events → LogStream 行模型（每事件一行 ≤80 字符）───────────────────
export interface LogLine {
  seq: number;
  type: WebEvent["type"];
  text: string; // 单行摘要 ≤80 字符
  isError: boolean;
}

/** 单行摘要：每个 EventType 均有 readable 摘要，无 no-op fallback（SPEC §5.5 / §9 AC3）。 */
export function summarizeEvent(e: WebEvent): string {
  const d = e.data ?? {};
  const node = e.node ?? "-";
  const sess = e.session_id ? e.session_id.slice(0, 6) : "------";
  const detail = eventDetail(e.type, d);
  return `${node} [${sess}] ${detail}`.slice(0, 80);
}

function eventDetail(
  type: WebEvent["type"],
  d: Record<string, unknown>
): string {
  switch (type) {
    case "workflow_started":
      return `workflow ${str(d.workflow_name)} started`;
    case "workflow_completed":
      return `workflow completed (${num(d.elapsed)}s)`;
    case "workflow_failed":
      return `workflow FAILED: ${str(d.message)}`;
    case "workflow_cancelled":
      return `workflow cancelled (${str(d.reason)})`;
    case "workflow_resumed":
      return `workflow resumed (replayed ${num(d.replayed_events)})`;
    case "node_started":
      return `node started`;
    case "node_completed":
      return `node completed (${num(d.elapsed)}s)`;
    case "node_failed":
      return `node FAILED: ${str(d.message)}`;
    case "node_skipped":
      return `node skipped (${str(d.reason)})`;
    case "agent_message":
      return `msg: ${str(d.text).slice(0, 60)}`;
    case "agent_thinking":
      return `thinking: ${str(d.text).slice(0, 60)}`;
    case "agent_tool_call":
      return `tool_call: ${str(d.tool)}`;
    case "agent_tool_result":
      return `tool_result: ${str(d.tool_call_id)}`;
    case "agent_usage":
      return `usage: in=${num(d.input_tokens)} out=${num(d.output_tokens)} rt=${num(d.reasoning_tokens ?? 0)} $${num(d.cost_usd)}`;
    case "agent_step_started":
      return `step: ${str(d.step_reason)}`;
    case "route_taken":
      return `route: ${str(d.from)} → ${str(d.to)}`;
    case "foreach_started":
      return `foreach: ${num(d.item_count)} items`;
    case "foreach_item_started":
      return `foreach item[${num(d.index)}]`;
    case "foreach_item_completed":
      return `foreach item[${num(d.index)}] done`;
    case "foreach_completed":
      return `foreach done (${num(d.count)})`;
    case "human_decision_requested":
      return `GATE: ${str(d.prompt)}`;
    case "human_decision_resolved":
      return `gate resolved: ${str(d.answer)}`;
    case "interrupt_requested":
      return `interrupt requested (${str(d.source)})`;
    case "interrupt_resolved":
      return `interrupt resolved: ${str(d.action)}`;
    case "prompt_rendered":
      return `prompt rendered`;
    case "retry_started":
      return `retry ${num(d.attempt)}/${num(d.max_attempts)} (${str(d.kind)})`;
    case "retry_succeeded":
      return `retry succeeded (total ${num(d.attempt_total)})`;
    case "retry_exhausted":
      return `retry exhausted (${num(d.attempts)})`;
    case "wait_started":
      return `wait ${num(d.duration_seconds)}s (${str(d.reason)})`;
    case "wait_completed":
      return `wait done (${num(d.elapsed_seconds)}s)`;
    case "validator_started":
      return `validator started`;
    case "validator_passed":
      return `validator passed`;
    case "validator_failed":
      return `validator FAILED`;
    case "dialog_started":
      return `dialog started (${str(d.node)})`;
    case "dialog_message":
      return `dialog[${str(d.role)}]: ${str(d.text).slice(0, 50)}`;
    case "dialog_ended":
      return `dialog ended (${num(d.total_turns)} turns)`;
    case "custom":
      return `custom[${str(d.kind)}]`;
    case "error":
      return `ERROR: ${str(d.message)}`;
    case "unknown_event":
      return `? unknown (${str(d.source)})`;
    default: {
      // 穷尽性检查：TS 编译期保证所有 EventType 都有分支。运行时若到这里是 events.ts
      // 与本 switch drift——codegen drift guard 应已拦。fail loud：返回可读标识，不静默。
      const _exhaustive: never = type;
      return `? unmapped ${String(_exhaustive)}`;
    }
  }
}

function str(v: unknown): string {
  if (v === undefined || v === null) return "";
  return String(v);
}

function num(v: unknown): number {
  const n = Number(v ?? 0);
  return Number.isFinite(n) ? n : 0;
}

const ERROR_TYPES = new Set<WebEvent["type"]>([
  "workflow_failed",
  "node_failed",
  "workflow_cancelled",
  "error",
  "validator_failed",
  "retry_exhausted",
]);

export function selectLog(state: WorkflowState): LogLine[] {
  return state.events.map((e: WebEvent) => ({
    seq: e.seq,
    type: e.type,
    text: summarizeEvent(e),
    isError: ERROR_TYPES.has(e.type),
  }));
}
